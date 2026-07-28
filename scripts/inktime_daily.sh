#!/usr/bin/env bash
set -euo pipefail

# =========================================================
# InkTime 每日编排脚本（BLE 推送 + ESP32 拉取，二选一或并存）
#
# 流程：
#   Step 1  render_daily_photo.py        选片 + 渲染 + 生成 photo_*.bin（恒跑）
#   Step 2  push_to_epd_ble.py           若 ENABLE_BLE_PUSH：BLE 推送到 nRF5 设备
#   Step 3  server.py (临时)             若 ENABLE_ESP32_SERVE：起 server 供 ESP32 主动拉取，
#                                        检测到拉取（或超时 ESP32_SERVE_WAIT_MIN 分钟）后关闭
#
# 所有步骤写入 logs/render.log；server.py 自身另写 logs/transfer.log（每次 HTTP 拉取的明细）。
#
# Cron 示例（每天 07:50 运行，早于 ESP32 默认刷新点 08:00）：
#   50 7 * * * /path/to/inktime/scripts/inktime_daily.sh >> /path/to/inktime/logs/cron.log 2>&1
# =========================================================

# ── 配置 ──────────────────────────────────────────────
# 默认指向本脚本上一级（仓库根），可用环境变量覆盖
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${PROJECT_DIR:-$(cd "${SCRIPT_DIR}/.." && pwd)}"

VENV_DIR="${PROJECT_DIR}/venv"
PYTHON_BIN="${PYTHON_BIN:-${VENV_DIR}/bin/python3}"
[ -x "${PYTHON_BIN}" ] || PYTHON_BIN="$(command -v python3 || command -v python)"

LOG_DIR="${LOG_DIR:-${PROJECT_DIR}/logs}"
TMP_DIR="${TMP_DIR:-${PROJECT_DIR}/tmp}"
LOCK_DIR="${PROJECT_DIR}/tmp/inktime_daily.lockdir"

# BLE 推送超时（秒）：扫描 10s + 传输 ~120s，留余量 3 分钟
PUSH_TIMEOUT=180
# 检测到 ESP32 拉取后的宽限（秒），防止设备立刻重试时 server 已关
FETCH_GRACE_SEC=30
# 轮询哨兵的间隔（秒）
POLL_INTERVAL=5

# ── 初始化 ────────────────────────────────────────────
mkdir -p "${LOG_DIR}" "${TMP_DIR}" "${PROJECT_DIR}/tmp"
cd "${PROJECT_DIR}"

log() { echo "[$(date '+%F %T')] $*" >> "${LOG_DIR}/render.log"; }

SERVER_PID=""

# ── 防重入锁 ─────────────────────────────────────────
if ! mkdir "${LOCK_DIR}" 2>/dev/null; then
    log "另一个编排进程正在运行，跳过本次。"
    exit 0
fi

cleanup() {
    if [ -n "${SERVER_PID}" ] && kill -0 "${SERVER_PID}" 2>/dev/null; then
        log "cleanup: 收尾时 server 仍在运行，强制关闭 (PID=${SERVER_PID})"
        kill "${SERVER_PID}" 2>/dev/null || true
    fi
    rmdir "${LOCK_DIR}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# ── 环境检查 ─────────────────────────────────────────
if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
    log "ERROR: 未找到 Python：${PYTHON_BIN}"
    log "       本地物理机请先：python3 -m venv venv && venv/bin/pip install -r requirements.txt"
    exit 1
fi

if [[ ! -f "config.py" ]]; then
    log "ERROR: 未找到 config.py，请参考 config-example.py 创建"
    exit 1
fi

# ── 读取投递能力开关与参数 ────────────────────────────
# 一次性从 config.py 读出：BLE开关 / ESP32开关 / 等待分钟 / 下载密钥 / 端口
CFG_LINE="$("${PYTHON_BIN}" -c "
import config as c
def g(name, default):
    v = getattr(c, name, default)
    return v
print(int(bool(g('ENABLE_BLE_PUSH', False))),
      int(bool(g('ENABLE_ESP32_SERVE', False))),
      int(g('ESP32_SERVE_WAIT_MIN', 15) or 15),
      str(g('DOWNLOAD_KEY', '')),
      int(g('FLASK_PORT', 8765) or 8765))
" 2>>"${LOG_DIR}/render.log")" || {
    log "ERROR: 读取 config.py 失败"
    exit 1
}

read -r DO_BLE DO_ESP32 ESP_WAIT_MIN DOWNLOAD_KEY FLASK_PORT <<<"${CFG_LINE}"
if [[ -z "${DOWNLOAD_KEY}" ]]; then
    log "ERROR: config.py 缺少 DOWNLOAD_KEY"
    exit 1
fi

log "========================================================"
log "InkTime 每日编排启动 | BLE=${DO_BLE} ESP32=${DO_ESP32} | 等待=${ESP_WAIT_MIN}min key=${DOWNLOAD_KEY} port=${FLASK_PORT}"

# ── Step 1：每日选片与渲染（恒跑）─────────────────────
log "Step 1: 开始每日选片与渲染"
if "${PYTHON_BIN}" render_daily_photo.py >> "${LOG_DIR}/render.log" 2>&1; then
    log "Step 1: 渲染完成 ✅"
else
    log "Step 1: 渲染失败 ❌（exit code: $?）"
    exit 1
fi

# ── Step 2：BLE 推送（若开启）─────────────────────────
if [[ "${DO_BLE}" == "1" ]]; then
    if [[ -f "${PROJECT_DIR}/push_to_epd_ble.py" ]]; then
        log "Step 2: 开始 BLE 推送到墨水屏"
        if timeout "${PUSH_TIMEOUT}" "${PYTHON_BIN}" push_to_epd_ble.py \
               >> "${LOG_DIR}/render.log" 2>&1; then
            log "Step 2: BLE 推送完成 ✅"
        else
            EC=$?
            if [[ "${EC}" -eq 124 ]]; then
                log "Step 2: BLE 推送超时 ⚠️（超过 ${PUSH_TIMEOUT}s，屏幕未更新）"
            else
                log "Step 2: BLE 推送失败 ⚠️（exit code: ${EC}，屏幕未更新）"
            fi
            # 推送失败不中断（屏幕维持上次内容，不影响后续 ESP32 步骤）
        fi
    else
        log "Step 2: push_to_epd_ble.py 不存在，跳过 BLE 推送。"
    fi
else
    log "Step 2: ENABLE_BLE_PUSH=False，跳过 BLE 推送。"
fi

# ── Step 3：ESP32 拉取式 server（若开启）──────────────
if [[ "${DO_ESP32}" == "1" ]]; then
    log "Step 3: 启动 server.py 供 ESP32 拉取"

    # 清掉上一次的拉取哨兵，避免误判
    rm -f "${TMP_DIR}/last_fetch.json"

    "${PYTHON_BIN}" server.py >> "${LOG_DIR}/render.log" 2>&1 &
    SERVER_PID=$!
    log "Step 3: server.py 已启动 (PID=${SERVER_PID}, 端口 ${FLASK_PORT})，等待 ESP32 拉取（最长 ${ESP_WAIT_MIN} 分钟）"

    DEADLINE=$(( $(date +%s) + ESP_WAIT_MIN * 60 ))
    FETCHED=0
    while [ "$(date +%s)" -lt "${DEADLINE}" ]; do
        if [[ -f "${TMP_DIR}/last_fetch.json" ]]; then
            FETCHED=1
            break
        fi
        if ! kill -0 "${SERVER_PID}" 2>/dev/null; then
            log "Step 3: server.py 进程已自行退出"
            break
        fi
        sleep "${POLL_INTERVAL}"
    done

    if [[ "${FETCHED}" == "1" ]]; then
        SENTINEL="$(cat "${TMP_DIR}/last_fetch.json" 2>/dev/null || echo '(读取失败)')"
        log "Step 3: 检测到 ESP32 拉取 ✅ → ${SENTINEL}"
        log "Step 3: 宽限 ${FETCH_GRACE_SEC}s 后优雅关闭 server..."
        sleep "${FETCH_GRACE_SEC}"
        # 优先走 /shutdown 优雅退出；失败兜底 kill
        curl -fsS -X POST "http://127.0.0.1:${FLASK_PORT}/static/inktime/${DOWNLOAD_KEY}/shutdown" \
             -o /dev/null --max-time 5 >> "${LOG_DIR}/render.log" 2>&1 || true
        sleep 1
        if kill -0 "${SERVER_PID}" 2>/dev/null; then
            log "Step 3: /shutdown 未生效，强制 kill"
            kill "${SERVER_PID}" 2>/dev/null || true
            wait "${SERVER_PID}" 2>/dev/null || true
        fi
        log "Step 3: server 已关闭 ✅"
    else
        log "Step 3: 超时未检测到 ESP32 拉取 ⚠️，关闭 server（今日设备可能未唤醒/拉取失败）"
        kill "${SERVER_PID}" 2>/dev/null || true
        wait "${SERVER_PID}" 2>/dev/null || true
    fi
    SERVER_PID=""
else
    log "Step 3: ENABLE_ESP32_SERVE=False，跳过 ESP32 server。"
fi

log "========================================================"
