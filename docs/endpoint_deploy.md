# 端点部署指南（Unraid NAS / 树莓派）

本文档介绍如何在 Unraid NAS 或树莓派上部署 InkTime，并通过蓝牙适配器每日自动将照片推送到 EPD-nRF5 墨水屏。

## 整体流程

```
每日 cron (07:30)
  └→ daily_render.sh
       ├─ render_daily_photo.py   选片 → 抖动 → photo_0.bin
       └─ push_to_epd_ble.py      格式转换 → BLE → nRF5 → 墨水屏刷新
```

---

## 平台差异速览

| 项目 | Unraid NAS | 树莓派 |
|------|-----------|--------|
| 蓝牙适配器 | 需外接 USB 蓝牙 | 3B+/4/5 内置，开箱可用 |
| 照片路径 | `/mnt/user/照片` | `/home/pi/Photos` 或挂载 NAS |
| 定时任务 | User Scripts 插件 或 cron | 标准 `crontab` |
| Python 版本 | 通常已满足 3.10+ | 旧版系统可能需升级 |
| exiftool | 按需安装 | 需手动 `apt install` |

---

## 1. 前置条件

### 1.1 确认蓝牙可用

```bash
# 检查蓝牙适配器状态
hciconfig -a
# 期望输出中含：hci0 ... UP RUNNING

# 如提示 DOWN，需开启
sudo hciconfig hci0 up
```

**树莓派额外步骤**：确认 `bluetoothd` 服务运行：

```bash
sudo systemctl status bluetooth
sudo systemctl enable bluetooth --now
```

### 1.2 安装系统依赖

**Unraid**：通常无需额外操作，`exiftool` 可通过 Nerd Pack 插件安装。

**树莓派**：

```bash
sudo apt update && sudo apt install -y libimage-exiftool-perl bluetooth bluez
```

### 1.3 找到墨水屏设备 MAC 地址

开启墨水屏设备电源（nRF5 固件启动后会自动广播，设备名如 `NRF_EPD_AABB`）：

```bash
sudo bluetoothctl
> power on
> scan on
# 等待 10~20 秒，观察出现的设备列表
# 记录 NRF_EPD_XXXX 对应的 MAC 地址
> scan off
> exit
```

---

## 2. Python 环境配置

```bash
cd /path/to/inktime

# 创建虚拟环境
python3 -m venv venv
source venv/bin/activate

# 安装所有依赖（含 bleak）
pip install -r requirements.txt
```

> **树莓派注意**：项目需要 Python 3.10+。树莓派 OS Lite 默认可能为 3.9，建议使用 `pyenv` 安装或从源码编译较新版本。

### 2.1 Linux BLE 权限配置

Linux 下 bleak 通过 D-Bus 访问蓝牙，**二选一**：

**方案 A（推荐）：赋予 Python 网络能力**

```bash
sudo setcap 'cap_net_raw,cap_net_admin+eip' $(readlink -f venv/bin/python3)
```

**方案 B：将用户加入 bluetooth 组**

```bash
sudo usermod -aG bluetooth $USER
# 重新登录后生效
```

---

## 3. 配置文件

复制并编辑配置：

```bash
cp config-example.py config.py
```

关键配置项：

```python
# 照片库路径
IMAGE_DIR = "/mnt/user/照片"          # Unraid NAS 示例
# IMAGE_DIR = "/home/pi/Photos"        # 树莓派本地示例
# IMAGE_DIR = "/media/pi/my_nas/照片"  # 树莓派挂载 NAS 示例

# BIN 文件输出目录
BIN_OUTPUT_DIR = "./output"

# ── EPD-nRF5 BLE 推送（重要）──
EPD_DEVICE_MAC = "AA:BB:CC:DD:EE:FF"   # 填入上一步找到的 MAC
EPD_BLE_CHUNK_SIZE = 238               # 传输不稳时改为 128
```

> **树莓派注意**：Pi 的 BLE/WiFi 共用同一芯片，干扰相对较大，若传输不稳可优先将 `EPD_BLE_CHUNK_SIZE` 调低至 `128`。

---

## 4. 修改 daily_render.sh

编辑 `scripts/daily_render.sh`，将 `PROJECT_DIR` 改为实际路径：

```bash
# Unraid 示例
PROJECT_DIR="/mnt/user/appdata/inktime"

# 树莓派示例
PROJECT_DIR="/home/pi/inktime"
```

赋予执行权限：

```bash
chmod +x /path/to/inktime/scripts/daily_render.sh
```

---

## 5. 手动测试完整链路

```bash
# Step 1：测试格式转换逻辑
python scripts/test_bin_convert.py
# 期望：✅ 所有测试通过

# Step 2：手动渲染今日照片
python render_daily_photo.py
# 期望：output/photo_0.bin 生成，384000 字节

# Step 3：格式转换+推送（含真实文件测试）
python scripts/test_bin_convert.py
# 期望：✅ 测试6（真实文件）也通过

# Step 4：BLE 推送到设备
python push_to_epd_ble.py
# 期望日志：
#   [INFO] 加载照片 #0：.../photo_0.bin（384,000 字节）
#   [INFO] 格式转换：384000B → 96000B
#   [INFO] 已连接！开始初始化屏幕...
#   [传输] 100% (96000/96000 字节)
#   [INFO] ✅ 刷新指令已发送！

# Step 5：运行完整脚本
bash scripts/daily_render.sh

# 查看日志
tail -f logs/render.log
```

---

## 6. 设置定时任务

### Unraid 方式（推荐）：User Scripts 插件

1. 安装 Unraid 社区应用 **User Scripts** 插件
2. 新建脚本，粘贴内容：
   ```bash
   #!/bin/bash
   /path/to/inktime/scripts/daily_render.sh
   ```
3. 调度设置：`Custom Cron` → `30 7 * * *`（每天 07:30）
4. 点击 `Run in Background` 测试一次

### 标准 cron 方式（树莓派 / 通用 Linux）

```bash
crontab -e
# 添加以下行：
30 7 * * * /path/to/inktime/scripts/daily_render.sh >> /path/to/inktime/logs/cron.log 2>&1
```

---

## 7. 常见问题排查

| 现象 | 原因 | 解决 |
|------|------|------|
| `bleak not found` | 未安装依赖 | `pip install bleak` |
| 扫描不到设备 | 设备未广播 | 断电重启 nRF5 设备，等待 LED 闪烁 |
| `Permission denied` BLE | 权限不足 | 执行 `sudo setcap` 或加入 bluetooth 组 |
| 传输中断 / 数据异常 | MTU 过大 | 将 `EPD_BLE_CHUNK_SIZE` 改为 `128` |
| 屏幕不刷新 | REFRESH 未发送 | 检查日志中"刷新指令已发送"是否出现 |
| 颜色显示错误 | 格式转换问题 | 运行 `test_bin_convert.py` 重新验证 |
| 超时退出 | 距离远 / 干扰强 | 调大 `PUSH_TIMEOUT`，或将蓝牙天线靠近设备 |
| `bleak` 不兼容 | 树莓派 5 蓝牙架构变化 | 确认 `bleak >= 0.21` |
| `exiftool` 找不到 | 未安装 | `sudo apt install libimage-exiftool-perl` |
| Python 版本不满足 | 树莓派 OS 默认 < 3.10 | 使用 `pyenv` 安装 3.10+ |
