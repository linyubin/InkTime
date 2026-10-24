#!/bin/bash
# =========================================================
# InkTime 每日推送结果检查脚本
# 每天 06:00 由 cron 触发，检查 04:50 主流程（inktime_daily.sh）的投递结果。
#
# 架构变化（server 常驻后）：
#   server.py 现由 systemd 常驻，inktime_daily.sh 不再起/停 server，
#   也不再写 "Step 3: 检测到 ESP32 拉取 ✅" 标记。因此 ESP32 投递是否成功，
#   改为查 transfer.log 当天是否有设备的 .bin 拉取记录（最可靠的证据）。
#
# 判定逻辑（双投递通道，任一启用通道成功即视为今日投递成功）：
#   - 读取 config.py 拿到本轮启用了哪些通道（ENABLE_BLE_PUSH / ENABLE_ESP32_SERVE）
#   - BLE   成功: render.log 本轮含 "Step 2: BLE 推送完成 ✅"
#   - ESP32 成功: transfer.log 当天有设备的 .bin 拉取记录（200）
#   - 任一启用通道成功 → 判定 OK，退出
#   - 否则视为失败 → 重跑 inktime_daily.sh 一次 → 仍失败则发飞书告警，
#     告警附当天完整日志（render 关键行 + transfer 拉取记录 + server 设备请求）
#
# 注意：重试调用的是新编排脚本 inktime_daily.sh（自动读 config 投递开关），
#       不再调用已退役的 daily_render.sh（后者无条件 BLE 推送，会与 config 冲突）。
# =========================================================

set -uo pipefail

LOG_FILE="/home/andy/inktime/logs/render.log"
TRANSFER_LOG="/home/andy/inktime/logs/transfer.log"
SERVER_LOG="/home/andy/inktime/logs/server.log"
INKTIME_SCRIPT="/home/andy/inktime/scripts/inktime_daily.sh"
NAZA_SENDER="/mnt/nas/Hermes_ws/scripts/naza_send_text.py"  # 原 openclaw CLI 已退役
FEISHU_TARGET="chat:oc_86e8cdcfb2d5ae745ac8ef6340173675"  # NAZA与Andy的DM（旧openclaw的open_id在NAZA应用下无效）
RETRY_LOG="/tmp/inktime_retry.log"
PYTHON_BIN="/home/andy/inktime/venv/bin/python3"
[ -x "${PYTHON_BIN}" ] || PYTHON_BIN="$(command -v python3 || command -v python)"

# ── 读取本轮启用的投递通道 ──────────────────────────────
read -r DO_BLE DO_ESP32 <<<"$("${PYTHON_BIN}" -c "
import sys
sys.path.insert(0, '/home/andy/inktime')
import config as c
print(int(bool(getattr(c, 'ENABLE_BLE_PUSH', False))),
      int(bool(getattr(c, 'ENABLE_ESP32_SERVE', False))))
" 2>/dev/null)" || {
    # config 读取失败，保守按「两个都启用」处理，避免漏判
    DO_BLE=1; DO_ESP32=1
}

# ── 抽取「本轮」编排记录（最后一次「InkTime 每日编排启动」之后的内容）──
extract_round_log() {
    local start_line
    start_line=$(grep -n "InkTime 每日编排启动" "${LOG_FILE}" 2>/dev/null | tail -1 | cut -d: -f1)
    if [ -z "${start_line}" ]; then
        return 0
    fi
    tail -n +"${start_line}" "${LOG_FILE}"
}

# ── 判定单轮投递是否成功（任一启用通道命中即可）─────────
judge_round() {
    local round_log="$1"
    local today=$2
    local ble_ok=0 esp_ok=0
    # BLE 成功：看 render.log 本轮标记
    echo "${round_log}" | grep -q "Step 2: BLE 推送完成 ✅" && ble_ok=1
    # ESP32 成功：看 transfer.log 当天是否有设备的 .bin 拉取（200）
    # （server 常驻后不再有 "Step 3: 检测到拉取" 标记，改查实际拉取记录）
    if [[ -f "${TRANSFER_LOG}" ]]; then
        grep -q "${today}.*\.bin.*200" "${TRANSFER_LOG}" 2>/dev/null && esp_ok=1
    fi
    # 任一启用通道成功
    { [ "${DO_BLE}" = "1" ]  && [ "${ble_ok}" = "1" ]; } && return 0
    { [ "${DO_ESP32}" = "1" ] && [ "${esp_ok}" = "1" ]; } && return 0
    return 1
}

# ── 收集当天日志全文（供失败告警）────────────────────────
TODAY="$(date +%Y-%m-%d)"
collect_day_logs() {
    local out=""
    # 1. render.log 当天所有编排记录（去掉渲染过程的 DEBUG/SELECT 噪音，保留关键行）
    out+="═══ render.log 当天编排记录 ═══"$'\n'
    local first_today
    first_today=$(grep -n "InkTime 每日编排启动" "${LOG_FILE}" 2>/dev/null \
                  | awk -F: -v d="${TODAY}" '$0 ~ d {print $1; exit}')
    if [[ -n "${first_today}" ]]; then
        tail -n +"${first_today}" "${LOG_FILE}" \
            | awk '/InkTime 每日编排|Step [0-9]|ERROR|失败|超时|异常|⚠|❌|✅|健康|服务|Traceback|RuntimeError/{print}' \
            | tail -40
    else
        out+="(当天无编排记录)"$'\n'
    fi
    out+=$'\n'
    # 2. transfer.log 当天拉取记录（设备 + 本机测试）
    out+="═══ transfer.log 当天拉取记录 ═══"$'\n'
    if [[ -f "${TRANSFER_LOG}" ]]; then
        grep "${TODAY}" "${TRANSFER_LOG}" 2>/dev/null | tail -20 || out+="(当天无拉取记录)"$'\n'
    else
        out+="(transfer.log 不存在)"$'\n'
    fi
    out+=$'\n'
    # 3. server.log 当天设备（非127.0.0.1）的请求
    #    server.log 用 Flask 默认日期格式 DD/Mon/YYYY（如 29/Jul/2026），需转换
    out+="═══ server.log 当天设备请求 ═══"$'\n'
    if [[ -f "${SERVER_LOG}" ]]; then
        local flask_date
        flask_date="$(date +'%d/%b/%Y')"  # 如 29/Jul/2026
        grep "${flask_date}" "${SERVER_LOG}" 2>/dev/null \
            | grep -v "127.0.0.1" | tail -20 || out+="(当天无设备请求)"$'\n'
    else
        out+="(server.log 不存在)"$'\n'
    fi
    printf '%s' "${out}"
}

# ── 第一次判定（主流程结果）──────────────────────────────
TODAY="$(date +%Y-%m-%d)"
ROUND_LOG="$(extract_round_log)"
if judge_round "${ROUND_LOG}" "${TODAY}"; then
    exit 0
fi

# ── 失败：重跑主流程一次 ─────────────────────────────────
# inktime_daily.sh 内部有防重入锁 + 完整的渲染链路，重试会重新渲染。
# 注：server 已常驻，重试不再起/停 server；主要价值是重新选片渲染 + 再给设备一次机会。
bash "${INKTIME_SCRIPT}" > "${RETRY_LOG}" 2>&1
# 重新判定（重试后 transfer.log 可能有新记录）
ROUND_LOG2="$(extract_round_log)"
if judge_round "${ROUND_LOG2}" "${TODAY}"; then
    exit 0
fi

# ── 重试仍失败：收集当天日志，发飞书告警 ─────────────────
DAY_LOGS="$(collect_day_logs)"

python3 "${NAZA_SENDER}" \
    --channel feishu \
    --target "${FEISHU_TARGET}" \
    --message "⚠️ InkTime 每日投递失败（重试后仍失败）

启用通道: BLE=${DO_BLE} ESP32=${DO_ESP32}
判定依据: ESP32 成功 = transfer.log 当天有设备 .bin 拉取记录

${DAY_LOGS}

完整日志: ${LOG_FILE}
重试日志: ${RETRY_LOG}" 2>/dev/null || true
