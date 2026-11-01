# InkTime V2


InkTime 是一个「拉回你相册里的记忆」的墨水屏电子相框项目。

它不会随机展示照片，也不是简单的按时间轴播放，而是：

- 用 AI 理解每一张照片在"“"拍什么"
- 给照片按照"值得回忆度"、"美观度"打分
- 写一句灵光一现的旁白文案
- 每天从"历史上的今天"里选出**最值得被再次看到的照片**
- 推送到 ESP32 墨水屏上展示

---
## 项目整体结构

InkTime 分为三部分：

1. **照片分析（Python）**  
   扫描相册 → 调用视觉模型 → 分类 / 评分 / 写文案 → 存入数据库


2. **图片渲染（Python）**  
   从数据库里选出「历史上的今天」高分照片 → 渲染成 ESP32 可直接显示的 `.bin`


3. **下载与展示（ESP32）**  
   ESP32 定时从服务器拉取 `.bin` → 刷新墨水屏 → 深度休眠直至下次唤醒

### 两套 ESP32 固件（按需选择）

`hardware/esp32/` 下并存两套独立固件，**硬件平台、投递方式、功能范围都不同**，部署前请先确认你用的是哪套（硬件拓扑、元器件清单、引脚定义详见 [`hardware/README.md`](hardware/README.md)）：

| | `ink-display-7C-photo`（原作者） | `ink-display-wifi-epd`（本 fork 主力维护） |
|---|---|---|
| 主控 | ESP32-S3-N8R8（**需 PSRAM** ≥384K） | ESP32-L（**无 PSRAM** 亦可） |
| 屏幕 | 7.3″ 四色 GDEY073D46（GxEPD2 库） | 7.3″ 四色 EL073TS3（厂商 EPD_SPI 库） |
| 投递方式 | 服务器 BLE 推送 / 同方案拉取 | **HTTP 拉取**（设备主动 GET `.bin`） |
| 相框旋转 | 无 | **有**（舵机横竖屏旋转 + 现场标定） |
| Web 调试 | 无 | 有（hub / `/fetch` / `/servo` 标定 / `/debug`） |
| 引脚 | 见下文「7C-photo 引脚」 | 见下文「wifi-epd 引脚」 |

> 本 README 后续章节若涉及具体引脚、选型、标定，默认以 **`ink-display-wifi-epd`**（本 fork 主力）为准；原作者 `7C-photo` 方案的细节保留在「ESP32 硬件部分」原章节内并明确标注。
> 拉取式投递的完整设计见 [`docs/esp32-pull-delivery.md`](docs/esp32-pull-delivery.md)。

---
## 仓库目录结构（2026-11 重构后）

服务端全部 Python 代码已收拢进 `inktime/` 包，按功能模块划分；渲染资产收拢进 `assets/`。仓库根只保留少量入口脚本与配置：

```text
inktime/                      # 仓库根
├── inktime/                  # 服务端 Python 主包
│   ├── core/                 # 基础设施：config 路径解析(common)、SQLite schema(db_schema)
│   ├── analysis/             # 照片分析流水线：analyze_photos.py(入口)、photo_scan_safety.py
│   ├── render/               # 渲染流水线：render_daily_photo.py(入口)、render_calib_cards.py(入口)、
│   │                         #   render_layouts / layout_strategy / style_registry / style_runtime、
│   │                         #   holidays / holiday_ai / holiday_render（节日模式）
│   ├── server/               # Web 服务：server.py(入口)、webui.py(Blueprint)、admin_security、
│   │                         #   admin_settings、batch_storage、*_security、device_*（设备协议/日志/注册）、
│   │                         #   templates/、static/（Flask 模板与前端资源）
│   └── devices/              # push_to_epd_ble.py（BLE 推送 nRF5 设备，入口）
├── assets/                   # 渲染资产：font/（中文字体）、layouts/（版式 JSON，版式编辑器可写）、
│                             #   seal_glyphs/（小篆印章字形）
├── server.py 等 5 个入口      # 兼容 shim：转发到 inktime 包内实现（见下「入口命令」）
├── config.py                 # 用户私有配置（从 config-example.py 复制，gitignore）
├── config-example.py         # 配置模板      ├── holidays.json / holidays-example.json
├── scripts/                  # 运维编排（inktime_daily.sh / inktime_check.sh，cron 用）+ 维护与校验脚本
├── tests/                    # 单元测试（python -m unittest discover -s tests）
├── docs/                     # 设计文档 / ADR / 验证报告
├── hardware/                 # 硬件层：esp32/（相框固件×4 + PCB + 驱动库 + 供电记录）、EPD-nRF5/（BLE 屏）
└── （运行时数据，均 gitignore）photos.db、output/、logs/、tmp/、data/、.cache/
```

**入口命令（根目录 5 个 shim，`python3 xxx.py` 用法保持不变）**：

| 命令 | 包内真实实现 |
|---|---|
| `python3 server.py` | `python3 -m inktime.server.server` |
| `python3 analyze_photos.py` | `python3 -m inktime.analysis.analyze_photos` |
| `python3 render_daily_photo.py` | `python3 -m inktime.render.render_daily_photo` |
| `python3 render_calib_cards.py` | `python3 -m inktime.render.render_calib_cards` |
| `python3 push_to_epd_ble.py` | `python3 -m inktime.devices.push_to_epd_ble` |

> shim 只是 `runpy` 转发，systemd 服务与 cron 脚本无需任何改动。历史代码备份（重构前的 `*.bak_*`）归档在 `backups/`。

---
## 环境准备

### 1）Python
推荐 Python 3.10+。

建议使用虚拟环境：

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2）安装 exiftool （可选）
InkTime 可以在不装 exiftool的情况下运行，但不一定能完整地获取 EXIF 中的 GPS 信息。

建议使用 exiftool 获取 GPS 信息：  

MacOS(Homebrew): ```brew install exiftool```  
Linux: ```sudo apt-get install -y libimage-exiftool-perl```

### 3) 配置 config.py
```
cp config-example.py config.py  
vi config.py
```
必须配置以下字段：  
照片库路径 ```IMAGE_DIR```  
VLM 模型接口 ```API_URL``` ```MODEL_NAME```  
InkTime 使用 OpenAI 接口（LM Studio / 其它兼容服务均可）。

为防止照片隐私泄露，建议修改```DOWNLOAD_KEY```，为 ESP32 下载路径加一个随机前缀作为密钥。   
同时，请同步修改```hardware/esp32/ink-display-7C-photo/ink-display-7C-photo.ino```固件中的```DAILY_PHOTO_PATH_PREFIX```字段（`ink-display-wifi-epd` 固件在配网页填写服务器地址即可）。   
注意，这不是“加密”，只是一个简单的验证路径口令。公网部署建议加 HTTPS/反代鉴权，或只允许内网访问。

## 分析照片
分析照片前，请先确保：
- LM Studio（或你的云端 VLM 服务）已启动
- config.py 已正确配置

执行：

```python3 analyze_photos.py```

视觉大模型会读取并理解相册目录中的所有文件，为每张照片生成：

- 画面描述
- 照片类型
- 值得回忆度 / 画面美观度评分
- 一句话文案

图片数据会保存在```photos.db```中（SQLite数据库）。

请自行修改```inktime/analysis/analyze_photos.py```中的提示词，以调整模型的评价标准和文案风格。

程序可以断点续跑，已处理过的照片信息不会重复分析。你可以分几天分析完你的整个相册。

*请根据你拥有的算力选择合适的模型，作者使用的 qwen3-vl-30b 已经能取得相当不错的文案。*

## 为 ESP32 渲染"历史上的今天"照片
执行：

```python3 render_daily_photo.py```

## 生成相框旋转标定卡（首次部署必做，仅 `ink-display-wifi-epd` 固件）

> 仅当使用 `hardware/esp32/ink-display-wifi-epd/` 固件（带相框旋转 / 舵机功能）时需要。
> 老的 `ink-display-7C-photo` / `ink-display-133C-photo` 固件无此功能，可跳过本节。

该固件支持通过舵机在竖屏（portrait）/ 横屏（landscape）之间旋转相框，安装时需要两张「朝向无歧义」的标定卡来现场标定舵机的绝对角度、转速与画面方向反转开关。标定卡上印有向上箭头、TOP/上、L/左、R/右及左上红三角 / 右下黄方块等不对称标记，便于一眼判断画面是否正放、颠倒或镜像。

标定卡是一次性部署产物（不随每日 `render_daily_photo.py` 重跑）。**首次部署、换机重装或想更新标定卡图案时，需手动执行：**

```bash
python3 render_calib_cards.py
```

产物写入 `output/`：

- `calib_p.bin`（竖屏 480×800，384000 字节）
- `calib_l.bin`（横屏 800×480，384000 字节）

生成后由 `server.py` 通过以下端点下发，供 ESP32 调试页拉取：

```
GET /static/inktime/<DOWNLOAD_KEY>/calib_p.bin
GET /static/inktime/<DOWNLOAD_KEY>/calib_l.bin
```

> ⚠️ 若不执行此步骤，ESP32 标定页拉取时会返回 `HTTP 404`，日志提示 `[标定] HTTP 失败 code=404`。

## 启动 ESP32 下载服务器和 WebUI
执行：

```python3 server.py```

`server.py` 是统一入口（`config.py` 的 `FLASK_PORT`，默认 8765），首页分发以下子页：

| 路径 | 功能 |
| --- | --- |
| `/` | InkTime Server 首页（各子页导航） |
| `/select` | 选片推送：今日选中批次 + 候选池预览、打分明细、手动勾选推送墨水屏 |
| `/review` | 照片库：按回忆度/美观度/日期浏览评分与文案，单张墨水屏模拟 |
| `/manager` | 相册管理：DataTables 多维筛选（原 8766 端口 `webui.py` 已并入本服务） |
| `/style-editor` | 版式编辑器：元素级拖放编辑 P/L 系版式，真机对照 |
| `/devlog` | 设备日志：ESP32 黑盒事件回查 |
| `/files/` | 输出文件浏览（`photo_*.bin` / `latest.*` / `preview_*.png`） |

在浏览器中访问：

```http://127.0.0.1:8765/```

程序跑通后，建议在```config.py```中关闭`ENABLE_REVIEW_WEBUI`，仅保留 ESP32 下载接口。

## 服务器部署与定时任务示例（可选）

创建 systemd 服务：

```sudo vi /etc/systemd/system/inktime-server.service```

示例（请自行修改项目路径）：

```
[Unit]
Description=InkTime Server
After=network.target

[Service]
Type=simple
# 改成你的项目路径
WorkingDirectory=/path/to/InkTime
ExecStart=/path/to/InkTime/venv/bin/python server.py
Restart=always
RestartSec=3
User=inktime
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```
sudo systemctl daemon-reload
sudo systemctl enable inktime-server
sudo systemctl start inktime-server
```

使用 crontab 每天凌晨自动选片、渲染：

```
chmod +x scripts/inktime_daily.sh
sudo -u inktime crontab -e
50 4 * * * /path/to/InkTime/scripts/inktime_daily.sh
```

在 ```logs/render.log```可查看日志。

---

# ESP32 墨水屏硬件部分

> 硬件系统拓扑、元器件清单（主控/屏幕/转接板/舵机/供电）、两套方案的完整引脚定义与 PCB 焊接说明，统一维护在 **[`hardware/README.md`](hardware/README.md)**；实物整机的供电架构与理想二极管改造记录见 [`hardware/esp32/esp32-power-hardware.md`](hardware/esp32/esp32-power-hardware.md)。本节只保留固件烧录与使用相关内容。

## 硬件方案速览

| | `ink-display-7C-photo`（原作者） | `ink-display-wifi-epd`（本 fork 主力维护） |
|---|---|---|
| 固件路径 | `hardware/esp32/ink-display-7C-photo/` | `hardware/esp32/ink-display-wifi-epd/` |
| 主控 | ESP32-S3-N8R8（**需 PSRAM** ≥384K） | ESP32-L（**无 PSRAM** 亦可） |
| 屏幕 | 7.3″ 四色 GDEY073D46（GxEPD2 库） | 7.3″ 四色 EL073TS3（厂商 EPD_SPI 库） |
| 投递方式 | 服务器 BLE 推送 / 同方案拉取 | **HTTP 拉取**（设备主动 GET `.bin`） |
| 相框旋转 | 无 | **有**（舵机横竖屏旋转 + 现场标定） |
| Web 调试 | 无 | 有（hub / `/fetch` / `/servo` 标定 / `/debug`） |

> 另有 nRF5 BLE 墨水屏（`hardware/EPD-nRF5/`，上游 tsl0922/EPD-nRF5 项目），由服务端 `push_to_epd_ble.py` 通过 BLE 推送，见 `ENABLE_BLE_PUSH` 配置项。

## 编译与烧录

建议使用 Arduino IDE。**两套固件的编译方式不同，请按你选用的方案操作。**

### 方案 A：ink-display-wifi-epd（本 fork 主力）
1. 安装 ESP32 Arduino Core。
2. 选择开发板：**ESP32**（普通 ESP32，**无需 PSRAM**；若用 ESP32-S3 也可，但不强制）。
3. 安装依赖库：屏幕驱动用厂商 **`EPD_SPI`** 库（随屏提供，放入 Arduino `libraries/`）；舵机用 `ESP32Servo`。
4. 打开并编译/烧录 `hardware/esp32/ink-display-wifi-epd/ink-display-wifi-epd.ino`。
5. 烧录建议用 UART 串口（BOOT 接 GND 上电进下载模式）。

### 方案 B：ink-display-7C-photo（原作者）
1. 安装 ESP32 Arduino Core。
2. 选择开发板：ESP32-S3（**必须开启 PSRAM**）。
3. 安装依赖库：
   - `GxEPD2`
4. 打开并编译/烧录 `hardware/esp32/ink-display-7C-photo/ink-display-7C-photo.ino`。

### 自定义字体(可选)
如需使用自定义中文字体，可放入 ```assets/font/``` 中，并在```config.py```中声明（相对路径以仓库根为基准）。

## 首次配置

设备启动时，会尝试从 NVS 读取已保存的 Wi-Fi 配置；若未配置或 Wi-Fi 连接失败，会自动进入 AP 配置模式：

- 设备会开启 AP 热点：`InkTime-xxxx`
- 默认密码：`12345678`
- 连接 AP ，用浏览器访问配置页面：`http://192.168.4.1/`
- 配置 Wi-Fi、服务器地址、定时更新时间并保存，设备会自动重启并进入正常工作流程。

## Web 调试页面（ink-display-wifi-epd）

设备配网后，在手动唤醒 / Debug 期间会在局域网 IP 上开放一个 Web Hub（端口见固件，默认 80）。从首页可进入各功能页：

| 页面 | 路径 | 用途 |
|---|---|---|
| Hub 首页 | `/` | 导航 + 设备状态 |
| 网络配置 | `/network` | WiFi / 服务器 / 刷新时间 / **竖屏画面旋转 180°** |
| 屏幕测试 | `/screen` | 4 色测试图案，验证屏幕与颜色 |
| 简单舵机测试 | `/servo_test` | 填角度+速度纯转动验证（不刷屏、不存） |
| **相框标定** | `/servo` | 横竖屏角度 + 画面方向，端到端测试 + 保存 |
| 图片拉取 | `/fetch?idx=N` | 手动拉取并渲染指定 idx 的照片 |
| 日志 / Debug | `/log` `/debug` | 下载拉取日志 / 开关 Debug 模式 |

> Debug 模式下设备保持唤醒不进入 Deep Sleep，方便反复调试；调试完务必 `/debug?state=off` 退出，否则 30 分钟无操作才自动休眠。

## 相框标定流程（ink-display-wifi-epd）

首次使用舵机旋转功能前，必须现场标定竖屏 / 横屏的舵机绝对角度，以及画面方向开关。在 Web Hub → **相框标定**（`/servo`）：

1. 填入竖屏角度（°）、横屏角度（°）、转动速度（°/s）。
2. 点 **「测试竖屏」/「测试横屏」**：固件会拉取对应标定卡 → 转舵机 → 刷屏显示标定卡。**测试用表单当前值，不写入，可反复迭代。**
3. 观察标定卡是否正放：中央箭头应**朝上**、`TOP/上` 在顶部、`L/左`/`R/右` 在两侧、左上角红三角、右下角黄方块。
4. 若画面方向不对，用两个翻转开关修正后再测：
   - **竖屏画面旋转 180°**：竖屏画面上下颠倒时勾选。
   - **横屏画面方向反转**：横屏画面方向不对时勾选。
5. 全部满意后点 **「💾 保存标定」**，参数写入 NVS（`servo_calibrated` 置 true）。

> 标定卡由服务端的 `render_calib_cards.py` 预生成（见前文「生成相框旋转标定卡」一节）。未生成时标定页会报 `HTTP 404`。

## 首次部署自检清单（ink-display-wifi-epd）

这套固件有多条渲染路径（竖屏/横屏、照片/标定卡、翻转开关），首次部署或换机后请按顺序走一遍，确保各路径都通——这些问题大多是**隐藏的**，平时只跑竖屏照片不会暴露：

- [ ] **服务端**：`server.py` 已启动，`output/` 下有当日 `photo_0.bin` + `latest.bin`（各 384000 字节）。
- [ ] **标定卡已生成**：跑过 `python3 render_calib_cards.py`，`output/calib_p.bin`、`calib_l.bin` 存在。否则 `/servo` 标定会 `404`。
- [ ] **`/screen` 测试图案**：4 色横带显示正常 → 屏幕接线、颜色映射 OK。
- [ ] **`/fetch?idx=0` 拉取照片**：成功刷屏，无 `[拉取] 尺寸不匹配` / `链路中断` 报错（偶发抖断固件会自动重试，最多 3 次）。
- [ ] **`/servo` 测试竖屏**：标定卡正放（箭头朝上），舵机转到竖屏角度。
- [ ] **`/servo` 测试横屏**：标定卡横向铺满、箭头朝上（方向不对就勾「横屏画面方向反转」再测）。
- [ ] **翻转开关生效**：勾选「竖屏画面旋转 180°」再测竖屏，画面应翻转；取消则恢复（若勾不勾都一样，说明固件未含 `computePaintRotate` 修复，需更新）。
- [ ] **保存标定**：点「💾 保存标定」，重启后舵机仍按标定角度转动。
- [ ] **`/debug?state=off`**：调试完退出 Debug，让设备恢复定时 Deep Sleep。

## 刷新与休眠

- 设备每天会在配置的更新时间，从服务器拉取一次当日生成的图片，并刷新墨水屏。
- 成功刷新后，会进入 Deep Sleep，直到下一次被唤醒。
- 若下载超时（默认 60s），也会进入长休眠，避免异常耗电。
- 在任意时候，按下 RESET 键，会强制重启并马上拉取、刷新一次图片。
- 长休眠待机电流 ＜ 1mA，如使用 2 节 18650 电池，5000mAh 约可实现半年续航。

## 相关项目
- ESP32 固件依赖 GxEPD2 © ZinggJM（GPL-3.0）：https://github.com/ZinggJM/GxEPD2  
  如对外分发编译后的固件，请同时遵守 GPL-3.0。


- 项目中的离线中文城市名索引，基于 GeoNames 数据制作：  
GeoNames © GeoNames contributors, CC BY 4.0  
https://www.geonames.org/
