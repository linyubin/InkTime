#!/usr/bin/env bash
set -euo pipefail

# =========================================================
# InkTime 每日编排脚本（渲染 + BLE 推送 + server 健康检查）
#
# 流程：
#   Step 1  render_daily_photo.py        选片 + 渲染 + 生成 photo_*.bin（恒跑）
#   Step 2  push_to_epd_ble.py           若 ENABLE_BLE_PUSH：BLE 推送到 nRF5 设备
#   Step 3  systemctl 健康检查           若 ENABLE_ESP32_SERVE：检查 inktime-server 服务存活，
#                                        异常则尝试重启。server.py 由 systemd 常驻管理
#                                        （开机自启 + 崩溃重启），不再由本脚本临时起/停。
#
# 所有步骤写入 logs/render.log；常驻 server 另写 logs/server.log（含 transfer.log 拉取明细）。
#
# Cron 示例（每天 04:50 运行，纯渲染时间；ESP32 靠服务器授时按自身 refresh_hour 唤醒拉取）：
#   50 4 * * * /path/to/inktime/scripts/inktime_daily.sh >> /path/to/inktime/logs/cron.log 2>&1
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

# ── 初始化 ────────────────────────────────────────────
mkdir -p "${LOG_DIR}" "${TMP_DIR}" "${PROJECT_DIR}/tmp"
cd "${PROJECT_DIR}"

log() { echo "[$(date '+%F %T')] $*" >> "${LOG_DIR}/render.log"; }

# ── 防重入锁 ─────────────────────────────────────────
if ! mkdir "${LOCK_DIR}" 2>/dev/null; then
    log "另一个编排进程正在运行，跳过本次。"
    exit 0
fi

cleanup() {
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
# 一次性从 config.py 读出：BLE开关 / ESP32开关 / 下载密钥
# （server 端口与等待时长已交由 systemd 服务管理，不再读取）
CFG_LINE="$("${PYTHON_BIN}" -c "
import config as c
print(int(bool(getattr(c, 'ENABLE_BLE_PUSH', False))),
      int(bool(getattr(c, 'ENABLE_ESP32_SERVE', False))),
      str(getattr(c, 'DOWNLOAD_KEY', '')))
" 2>>"${LOG_DIR}/render.log")" || {
    log "ERROR: 读取 config.py 失败"
    exit 1
}

read -r DO_BLE DO_ESP32 DOWNLOAD_KEY <<<"${CFG_LINE}"
if [[ -z "${DOWNLOAD_KEY}" ]]; then
    log "ERROR: config.py 缺少 DOWNLOAD_KEY"
    exit 1
fi

log "========================================================"
log "InkTime 每日编排启动 | BLE=${DO_BLE} ESP32=${DO_ESP32} | key=${DOWNLOAD_KEY}"

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
    # server.py 现由 systemd 服务 inktime-server 常驻管理（开机自启 + 崩溃重启），
    # 提供 ESP32 拉照片接口 + /time 服务器授时。本脚本不再起/停 server，
    # 仅做健康检查：确认服务存活，异常则尝试重启。
    log "Step 3: 检查 inktime-server 服务健康"
    if sudo systemctl is-active --quiet inktime-server; then
        log "Step 3: inktime-server 服务运行中 ✅"
    else
        log "Step 3: inktime-server 服务未运行 ⚠️，尝试重启..."
        if sudo systemctl restart inktime-server 2>/dev/null; then
            sleep 2
            if sudo systemctl is-active --quiet inktime-server; then
                log "Step 3: 重启成功 ✅"
            else
                log "Step 3: 重启后仍异常 ❌（ESP32 将无法拉取/授时）"
            fi
        else
            log "Step 3: 重启失败 ❌（请检查 systemctl status inktime-server）"
        fi
    fi
else
    log "Step 3: ENABLE_ESP32_SERVE=False，跳过。"
fi

log "========================================================"
