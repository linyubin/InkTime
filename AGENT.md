# AGENT.md — InkTime 智能体协作指南

本文件为 AI 编码智能体（Claude Code / Gemini CLI 等）提供本仓库的工作指引。

## 项目概览

InkTime 是一个集成 AI 视觉能力的墨水屏电子相框项目：分析本地照片库，用 VLM（视觉大模型）识别照片内容、按"值得回忆度/美观度"打分并撰写旁白，每天从"历史上的今天"中选出最值得再次看到的照片，渲染成墨水屏专用 `.bin` 推送到相框展示；舵机按照片朝向自动翻转相框。

### 核心流程

1. **照片分析（Python，`inktime/analysis/`）**：扫描 `IMAGE_DIR`，提取 EXIF/GPS，调用 VLM（OpenAI 兼容接口：LM Studio / Ollama / 云端 API）评分与写文案，结果存入 SQLite。
2. **图片渲染（Python，`inktime/render/`）**：按日期筛选高分照片，套用版式（layouts）与节日模板，渲染成墨水屏 `.bin`。
3. **展示端（硬件，`hardware/`）**：ESP32 相框定时唤醒，HTTP 拉取 `.bin` 刷屏后深度休眠；另有 nRF5 BLE 屏走 BLE 推送通道。

## 仓库结构

```text
inktime/                  # 仓库根
├── inktime/              # 服务端 Python 主包（改代码都在这里）
│   ├── core/             # common.py（config 路径解析/日期解析）、db_schema.py（SQLite 迁移）
│   ├── analysis/         # analyze_photos.py（VLM 提示词在此）、photo_scan_safety.py
│   ├── render/           # render_daily_photo.py、render_layouts.py、layout_strategy.py、
│   │                     #   style_registry.py、style_runtime.py、holidays.py、
│   │                     #   holiday_ai.py、holiday_render.py、render_calib_cards.py
│   ├── server/           # server.py（统一 Flask 入口）、webui.py（Blueprint）、admin_*、
│   │                     #   *_security、device_*（设备协议/日志/注册）、batch_storage.py、
│   │                     #   templates/、static/
│   └── devices/          # push_to_epd_ble.py（BLE 推送 nRF5 屏）
├── assets/               # 渲染资产：font/（中文字体）、layouts/（版式 JSON，编辑器可写）、seal_glyphs/
├── hardware/             # 硬件层（拓扑/元器件/引脚见其 README.md）
│   ├── esp32/            # 相框固件×4（ink-display-wifi-epd 主力、7C-photo/133C-photo 历史、
│   │                     #   servo_serial_cmd 调试工具）+ pcb/ + libraries/ + 供电记录
│   └── EPD-nRF5/         # nRF5 BLE 墨水屏固件（上游 tsl0922/EPD-nRF5 原样保留）
├── scripts/              # 运维编排（inktime_daily.sh / inktime_check.sh，cron 用）+ 维护/校验脚本
├── tests/                # 单元测试
├── docs/                 # 设计文档 / ADR / 验证报告
├── config.py             # 用户私有配置（从 config-example.py 复制，gitignore）
└── server.py 等 5 个根目录文件   # 兼容 shim（runpy 转发到 inktime 包），见下
```

根目录的 `server.py` / `analyze_photos.py` / `render_daily_photo.py` / `render_calib_cards.py` / `push_to_epd_ble.py` 是 `runpy` 兼容 shim，保证 systemd 服务与 cron 脚本无需改动；新代码一律进 `inktime/` 包内对应模块。

## 常用命令

```bash
# 环境
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 入口（两种写法等价，shim 转发到包内模块）
python3 analyze_photos.py          # = python3 -m inktime.analysis.analyze_photos
python3 render_daily_photo.py      # = python3 -m inktime.render.render_daily_photo
python3 server.py                  # = python3 -m inktime.server.server
python3 render_calib_cards.py      # = python3 -m inktime.render.render_calib_cards
python3 push_to_epd_ble.py         # = python3 -m inktime.devices.push_to_epd_ble

# 测试
python -m unittest discover -s tests -p 'test_*.py'
```

- 旧独立管理界面 `webui.py`（8766 端口）已并入 `server.py`（Blueprint），仅保留 `python -m inktime.server.webui` 兼容旧启动方式。
- 服务由 systemd 常驻：`inktime-server.service`（`ExecStart=... python3 server.py`，日志 `logs/server.log`）。
- cron 编排：`scripts/inktime_daily.sh`（每日 04:00 渲染 + 按开关 BLE 推送/服务健康检查）、`scripts/inktime_check.sh`（06:00 投递结果判定，失败重试 + 飞书告警）。

## 配置（config.py）

所有路径、API 密钥、阈值参数都放 `config.py`（从 `config-example.py` 复制），**严禁硬编码**。必配项：

- `IMAGE_DIR`：照片库路径（NAS CIFS 挂载等；跨平台映射用 `PATH_MAP`）
- `API_URL` / `MODEL_NAME`：VLM 接口（OpenAI 兼容，`common.resolve_chat_endpoint` 自动补全端点）
- `DOWNLOAD_KEY`：ESP32 下载路径口令
- `ENABLE_BLE_PUSH` / `ENABLE_ESP32_SERVE`：投递通道开关（可并存）

相对路径一律以仓库根为基准（`inktime.core.common.ROOT_DIR`）。

## 架构要点

### 照片分析（inktime/analysis/analyze_photos.py）

1. 扫描 `IMAGE_DIR`（NAS 断链 fail-closed：`photo_scan_safety.py`，扫描不完整绝不执行破坏性动作）
2. EXIF 提取 GPS（`exiftool`，缺失时回退文件名日期解析）
3. VLM 逐张分析：内容描述、类型、回忆度/美观度评分（1-100）、一句话文案 → 存 `photos.db` 的 `photo_scores` 表
4. 可断点续跑，已分析照片跳过；VLM 提示词在本文件的 `generate_side_caption` / `analyze_image_content` 函数中

### 每日渲染（inktime/render/render_daily_photo.py）

1. 查询 `photos.db` 匹配"今日月日"的照片，加权选片（得分/新鲜度/路径/类别偏好、连拍去重、脸框否决）
2. 文字层走 12 版式管线（`layout_strategy.py` + `render_layouts.py`；`TEXT_LAYOUT_STRATEGY=False` 回退旧底部卡片）
3. 节日模式（`holidays.py` 匹配 `holidays.json`，公历+农历+除夕）：`holiday_render.py` 叠加庆祝模板；`holiday_ai.py` 可重排序/改文案/写祝福
4. 输出 `output/photo_0..N.bin` + sidecar json + preview PNG；`.bin` 格式：竖屏 480×800 = 384,000 字节，1 字节/像素，`0=黑 1=白 2=红 3=黄`，行主序（所有固件通用）

### BLE 推送（inktime/devices/push_to_epd_ble.py）

1. BLE 连接 EPD-nRF5 设备（自动扫描或 `EPD_DEVICE_MAC`）
2. 协议 Service UUID `62750001-…`，`CMD_WRITE_IMG`(0x30) 发送数据、`CMD_REFRESH`(0x05) 刷屏
3. 需 Linux BlueZ 栈（或 Windows bleak）

### 服务端（inktime/server/server.py）

统一 Flask 入口（`FLASK_PORT` 默认 8765）：ESP32 下载端点（`/{DOWNLOAD_KEY}/photo_N.bin`、`latest.bin`）、`/review` 照片库、`/select` 选片推送、`/manager` 相册管理、`/style-editor` 版式编辑器、`/devlog` 设备日志、`/device` 设备控制台。管理写操作有 Basic Auth + 签名 CSRF（凭据 `data/admin.env`，env `INKTIME_ADMIN_USERNAME/PASSWORD` 可覆盖）。

## 硬件与固件（hardware/）

两套 ESP32 相框固件硬件平台/驱动/投递方式均不同，**引脚不可混用**；系统拓扑、元器件清单、引脚表、PCB/供电记录统一见 **[`hardware/README.md`](hardware/README.md)**：

- `hardware/esp32/ink-display-wifi-epd/`（主力）：ESP32-L + 厂商 `EPD_SPI` 库（固件内自带 `EPD.cpp/.h`，2bit/像素四色）+ 舵机旋转；HTTP 拉取投递；手动唤醒开 3 分钟 Web 配置窗口。编译依赖 `EPD_SPI`、`ESP32Servo`，无需 PSRAM。
- `hardware/esp32/ink-display-7C-photo/`、`ink-display-133C-photo/`（历史方案）：ESP32-S3（必须 PSRAM）+ GxEPD2。
- `hardware/esp32/servo_serial_cmd/`：舵机串口标定工具（独立 sketch，有自己的 README）。
- `hardware/EPD-nRF5/`：上游固件原样保留（nRF51822/52810/52811，UC81xx/SSD16xx 驱动，Keil/GCC 编译，Web Bluetooth 控制，支持 BLE OTA）。

WiFi 凭据等固件行为有源码级回归测试：`tests/test_firmware_wifi_credentials.py`（不编译固件）。

## 开发约定

- **包结构**：服务端代码全部在 `inktime/` 包内（core/analysis/render/server/devices）；根目录 5 个 shim 仅供兼容，勿在其中加逻辑。
- **配置驱动**：路径/密钥/阈值一律进 `config.py`；代码里读配置用 `getattr(cfg, "KEY", default)` 并保证 `config-example.py` 同步定义（有测试守护：`tests/test_config_example_completeness.py`）。
- **数据库**：`photos.db`（SQLite）存所有照片元数据；schema 迁移走 `inktime/core/db_schema.py` 显式增量迁移（v0→v2），禁止隐式 DDL。
- **硬件适配**：换屏需改固件屏幕驱动构造 + `inktime/render/render_daily_photo.py` 渲染尺寸。
- **运行时数据**（gitignore，勿提交）：`photos.db`、`output/`、`logs/`、`tmp/`、`data/`、`.cache/`、`holidays.json`。

## 测试

```bash
python -m unittest discover -s tests -p 'test_*.py'
node --test tests/test_admin_csrf_js.cjs   # 浏览器 CSRF 适配器（需 node）
```

实验/校验脚本在 `scripts/`：`test_ollama.py`（VLM 连通性）、`test_exiftool.py`（EXIF）、`test_bin_convert.py`（bin 转换）、`validate_*.py`（版式/节日模板校验）、`debug_*.py`（调试工具）。
