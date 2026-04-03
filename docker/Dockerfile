# /docker/Dockerfile
FROM python:3.10-slim

# 安装基础系统依赖，包括定时任务工具(cron)，守护进程(supervisor)，照片GPS解析(exiftool)，及蓝牙底层(bluez, dbus)
RUN apt-get update && apt-get install -y --no-install-recommends \
    exiftool \
    cron \
    supervisor \
    bluez \
    dbus \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# 设定工作目录
WORKDIR /app

# 将依赖预先考入以优化 Docker 镜像构建缓存
COPY requirements.txt /app/

# 安装项目所需的 Python 依赖
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# 把整个项目源码拷贝至 /app
COPY . /app/

# 为入口脚本提供执行权限
RUN chmod +x /app/docker/entrypoint.sh 

# 设定国内常见默认时区（保证 cron 时间准确无误）
ENV TZ=Asia/Shanghai

# 唯一容器入口点
ENTRYPOINT ["/app/docker/entrypoint.sh"]
