#!/usr/bin/env bash
set -euo pipefail

# =========================================================
# InkTime 每日渲染 + BLE 推送脚本
#
# 流程：
#   1. 运行 render_daily_photo.py：选片、渲染、生成 photo_0.bin
#   2. 运行 push_to_epd_ble.py：格式转换，通过 NAS 蓝牙推送到墨水屏
#
# Cron 示例（每天 07:30 运行）：
#   30 7 * * * /path/to/inktime/scripts/daily_render.sh >> /path/to/inktime/logs/cron.log 2>&1
# =========================================================

# ── 配置 ──────────────────────────────────────────────
# 修改为你在 NAS 上的实际项目目录
PROJECT_DIR="/path/to/inktime"

VENV_DIR="${PROJECT_DIR}/venv"
PYTHON_BIN="${VENV_DIR}/bin/python"
LOG_DIR="${PROJECT_DIR}/logs"
LOCK_DIR="${PROJECT_DIR}/tmp/inktime_render.lockdir"

# BLE 推送超时（秒）：扫描最多10s + 传输最多约120s，留余量共3分钟
PUSH_TIMEOUT=180

# ── 初始化 ────────────────────────────────────────────
mkdir -p "${LOG_DIR}" "${PROJECT_DIR}/tmp"
cd "${PROJECT_DIR}"

log() { echo "[$(date '+%F %T')] $*" >> "${LOG_DIR}/render.log"; }

# ── 防重入锁 ─────────────────────────────────────────
if ! mkdir "${LOCK_DIR}" 2>/dev/null; then
    log "另一个渲染进程正在运行，跳过本次。"
    exit 0
fi

cleanup() { rmdir "${LOCK_DIR}" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

# ── 环境检查 ─────────────────────────────────────────
if [[ ! -x "${PYTHON_BIN}" ]]; then
    log "ERROR: 未找到 Python：${PYTHON_BIN}"
    log "       请先创建虚拟环境：python3 -m venv venv && venv/bin/pip install -r requirements.txt"
    exit 1
fi

if [[ ! -f "config.py" ]]; then
    log "ERROR: 未找到 config.py，请参考 config-example.py 创建配置文件"
    exit 1
fi

# ── Step 1：每日选片与渲染 ────────────────────────────
log "========================================================"
log "Step 1: 开始每日选片与渲染"
if "${PYTHON_BIN}" render_daily_photo.py >> "${LOG_DIR}/render.log" 2>&1; then
    log "Step 1: 渲染完成 ✅"
else
    log "Step 1: 渲染失败 ❌（exit code: $?）"
    exit 1
fi

# ── Step 2：BLE 推送到墨水屏 ─────────────────────────
if [[ ! -f "${PROJECT_DIR}/push_to_epd_ble.py" ]]; then
    log "Step 2: push_to_epd_ble.py 不存在，跳过 BLE 推送。"
    log "========================================================"
    exit 0
fi

log "Step 2: 开始 BLE 推送到墨水屏"
if timeout "${PUSH_TIMEOUT}" "${PYTHON_BIN}" push_to_epd_ble.py \
       >> "${LOG_DIR}/render.log" 2>&1; then
    log "Step 2: BLE 推送完成 ✅"
else
    EXIT_CODE=$?
    if [[ "${EXIT_CODE}" -eq 124 ]]; then
        log "Step 2: BLE 推送超时 ⚠️（超过 ${PUSH_TIMEOUT}s，本次屏幕未更新）"
    else
        log "Step 2: BLE 推送失败 ⚠️（exit code: ${EXIT_CODE}，本次屏幕未更新）"
    fi
    # 推送失败不中断脚本（屏幕维持上次内容，不影响明天再试）
fi

log "========================================================"