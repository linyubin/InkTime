#!/bin/bash
set -eo pipefail

echo "[InkTime] 正在初始化容器启动环境..."

# 1. 映射挂载配置
# 在 Unraid 下，我们推荐将数据固化至 /config (例如宿主的 /mnt/user/appdata/inktime)
# 创建 config 目录下的日志存放文件夹
mkdir -p /config/logs

# 判断是否存在配置，不存在则初始化模板
if [ ! -f "/config/config.py" ]; then
    echo "[InkTime] 未找到 /config/config.py，从默认模板载入..."
    cp /app/config-example.py /config/config.py
fi

# 由于主程序的路径逻辑是读取运行目录下的 config.py，建立软连接实现隔离
rm -f /app/config.py
ln -s /config/config.py /app/config.py

# 2. 根据环境变量动态写入 Crontab 定时调度配置
# 默认为每天的 07:30，如果 Unraid 模板面板填入了其它语法（例如 "0 2 * * *"），则启用其它语法
SCHEDULE=${CRON_SCHEDULE:-"30 7 * * *"}
echo "[InkTime] 注册墨水屏每日更新任务 (Cron): ${SCHEDULE}"

# 将指令写入标准 crontab 中，且输出打印到 /config 以便长期除错
# 通过行内参数赋予 Docker 全局的容器运行上下文
echo "${SCHEDULE} cd /app && PROJECT_DIR=/app PYTHON_BIN=python LOG_DIR=/config/logs bash scripts/inktime_daily.sh >> /config/logs/cron_render.log 2>&1" > /etc/cron.d/inktime-cron
chmod 0644 /etc/cron.d/inktime-cron
crontab /etc/cron.d/inktime-cron

# 3. 移交运行权限给 Supervisor (守护进程树管理大师)
echo "[InkTime] 正在启动主服务调度树 (Supervisor)..."
exec /usr/bin/supervisord -c /app/docker/supervisord.conf
