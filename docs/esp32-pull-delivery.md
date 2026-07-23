# ESP32 拉取式投递适配 · 设计与运维文档

> 建立日期：2026-07-23
> 关联固件：`esp32/ink-display-wifi-epd/`（ESP32-L，7.3″ 四色 GDEY073D46）
> 关联脚本：`scripts/inktime_daily.sh`、`server.py`、`render_daily_photo.py`

---

## 1. 背景

InkTime 原本只支持 **BLE 主动推送**（树莓派 cron → `render_daily_photo.py` → `push_to_epd_ble.py`，推给 nRF5 墨水屏）。
新增的 ESP32-L 墨水屏是**主动拉取**模型：设备定时从 deep sleep 唤醒，HTTP GET 树莓派上的 `.bin` 渲染后继续睡。

两条投递路径的传输方式完全相反，需要：
1. 让树莓派知道自己走哪条路（配置开关）；
2. 为 ESP32 拉取提供临时 server 编排 + 传输日志 + 拉取检测；
3. 固件端提供手动调试入口。

**关键前提**（经核对代码确认）：ESP32-L 固件**直接消费** `render_daily_photo.py` 产出的 canonical `.bin`（480×800，1 字节/像素，`0黑/1白/2红/3黄`，384000 字节），设备上逐字节映射 `0→BLACK0…3→YELLOW0`。因此 **render 算法无需改动**；BLE 的格式差异早已在 `push_to_epd_ble.py` 的 `inktime_to_fourcolor_packed` 中处理。`convert_img4.py` 仅用于生成开机背景 `ap_bg.h`，与每日照片无关。

---

## 2. 决策历史（grilling 访谈记录）

| 编号 | 议题 | 决策 |
|---|---|---|
| Q1 | render 是否要为 ESP32 改 BIN 算法？ | **A：不改**。先看 ESP32 实际显示效果，之后若需优化（2bpp 96KB 降带宽 / 换 Floyd-Steinberg 抖动）再议。 |
| Q2 | 配置开关驱动什么？ | 让树莓派清楚自己向哪条路径投递，并**为未来功能（舵机等）留扩展**。 |
| Q3 | BLE / ESP32 能否并存？ | **可并存**（一台部署可同时推 BLE + 供 ESP32）。 |
| Q4 | 开关命名 | 由单值 enum 改为**独立布尔**（兼容 Q3 的并存需求）：`ENABLE_BLE_PUSH` / `ENABLE_ESP32_SERVE`。 |
| Q5 | 拉取检测机制 | server 写哨兵 `tmp/last_fetch.json` + 脚本轮询 + 超时兜底；同时写**详细传输日志**。 |
| Q6 | ESP32 漏拉怎么办？ | **列为待办**：未来做「树莓派确认机制 + 远程唤醒」。本轮只打日志地基。 |
| Q7 | 日志粒度 | 尽量详细，含 HTTP 访问日志（时间/IP/idx/字节数/耗时/UA）。 |
| Q8 | 防重入锁 | 保留 lockdir。 |
| Q9 | debug 入口位置 | 固件自己的 WebServer，新增 `/fetch` `/log` `/debug`。 |
| Q10 | debug 可达性 | webui 加**手动 debug 模式开关**，开启后保持 server 唤醒。 |
| Q11 | 日志输出方式 | `/fetch` 网页返回日志 + `/log` 下载；关 debug 时提示下载。 |
| Q12 | debug 退出条件 | 手动关闭 debug 才 DeepSleep；未操作 **30 分钟**自动休眠。 |
| Q13 | idx 选择 | 调试可指定固定 idx；**正常流程固定拉 idx=0**（评分最高）。 |

ESP32 存储澄清：5 张 `.bin` 住在树莓派 `output/`，ESP32 同一时刻只持有一张 96KB 画布（下载即渲染即释放），**无存储压力**。

---

## 3. 系统架构与数据流

```
┌─────────────── 树莓派（生成 + 托管）───────────────┐
│  cron 07:50 → scripts/inktime_daily.sh             │
│    Step1 render_daily_photo.py → output/photo_0..4.bin │
│    Step2 (ENABLE_BLE_PUSH) push_to_epd_ble.py      │
│    Step3 (ENABLE_ESP32_SERVE) server.py 后台起      │
│            轮询 tmp/last_fetch.json                │
│            ├─ 检测到 → 宽限30s → POST /shutdown     │
│            └─ 超时 ESP32_SERVE_WAIT_MIN → kill      │
│  日志：logs/render.log（脚本）+ logs/transfer.log（HTTP）│
└─────────────────────────────────────────────────────┘
                       ▲ HTTP GET（ESP32 主动拉）
┌─────────────── ESP32-L（每次唤醒跑一遍 setup）─────┐
│  定时唤醒：连WiFi→NTP→下载 idx=0→渲染→deep sleep   │
│  手动唤醒(BOOT)：屏显IP→web server→                │
│     /debug?state=on → /fetch?idx=N → /log →        │
│     /debug?state=confirm_off → deep sleep          │
│  （debug 下 30 分钟无操作自动休眠）                 │
└─────────────────────────────────────────────────────┘
```

---

## 4. 配置项说明（`config.py`）

```python
ENABLE_BLE_PUSH    = True   # BLE 推送到 nRF5 设备（push_to_epd_ble.py）
ENABLE_ESP32_SERVE = True   # HTTP 供 ESP32 主动拉取（server.py，由脚本临时起停）
ESP32_SERVE_WAIT_MIN = 15   # render 后 server 等待 ESP32 拉取的最长在线分钟数
DOWNLOAD_KEY = "andyhome0203"   # 必须与固件 DAILY_PHOTO_PATH_PREFIX 中的 key 一致
FLASK_PORT = 8765
```

固件侧（`ink-display-wifi-epd.ino`）需手动同步的常量：
- `DAILY_PHOTO_PATH_PREFIX = "/static/inktime/andyhome0203/photo_"`（key 段）
- `DAILY_PHOTO_COUNT = 5`（须 = config 的 `DAILY_PHOTO_QUANTITY`）

---

## 5. 树莓派端操作步骤

### 5.1 首次部署
```bash
cd /path/to/InkTime
python3 -m venv venv && venv/bin/pip install -r requirements.txt
cp config-example.py config.py   # 编辑：IMAGE_DIR / DOWNLOAD_KEY / ENABLE_* 等
```

### 5.2 配 cron（每天 07:50，早于 ESP32 默认刷新点 08:00）
```cron
50 7 * * * /path/to/InkTime/scripts/inktime_daily.sh >> /path/to/InkTime/logs/cron.log 2>&1
```

### 5.3 手动跑一次验证
```bash
bash scripts/inktime_daily.sh
tail -f logs/render.log      # 脚本日志
tail -f logs/transfer.log    # 每次 HTTP 拉取明细（ESP32 真正取走数据时才有）
```

### 5.4 关键产物
- `output/photo_0..4.bin` + `latest.bin`：渲染产物。
- `logs/render.log`：编排脚本日志（render/推送/server 起停/是否拉取）。
- `logs/transfer.log`：每次 `/static/inktime/...` 请求的明细（时间/IP/idx/状态/字节/耗时/UA）。
- `photos.db` 表 `transfer_history`：同上，可 SQL 查询。
- `tmp/last_fetch.json`：最近一次拉取哨兵（脚本据此判断「已取走」）。

---

## 6. ESP32 端操作步骤

### 6.1 烧录
Arduino IDE → 开发板「ESP32-L Module」→ 打开 `esp32/ink-display-wifi-epd/ink-display-wifi-epd.ino` → 编译上传。

### 6.2 正常运行（定时刷新）
配网后每天到 `refresh_hour`（默认 8 点）自动唤醒：连 WiFi → NTP → 下载 `idx=0` → 渲染 → 深睡。**不起 web server，功耗不变。**

### 6.3 手动调试流程
1. 按一下 **BOOT 键**（GPIO0）手动唤醒 → 屏幕显示当前 IP/SSID/MAC。
2. 浏览器打开 `http://<设备IP>/`。
3. 点 `/debug?state=on` 开启 debug（设备保持唤醒）。
4. 点 `/fetch?idx=N`（N=0..4）手动拉取并渲染，页面返回完整日志（URL/HTTP码/字节数/耗时/成功失败）。
5. 需要留存日志时点 `/log` 下载 `inktime_fetch.log`。
6. 调试完点 `/debug?state=off` → 提示下载日志 → `/debug?state=confirm_off` 确认 → 设备进入 Deep Sleep。
7. 若忘了关：debug 下 **30 分钟无操作自动休眠**（常量 `DEBUG_IDLE_MS`）。

> 注：`/fetch` 触发的四色刷新会阻塞 web 约 10–20s，期间浏览器等待属正常。

---

## 7. 代码改动清单

| 文件 | 改动 |
|---|---|
| `config.py` / `config-example.py` | 新增 `ENABLE_BLE_PUSH` / `ENABLE_ESP32_SERVE` / `ESP32_SERVE_WAIT_MIN` |
| `server.py` | 新增 `tmp/` `logs/` 目录与 `TRANSFER_LOG`/`LAST_FETCH_SENTINEL` 常量；`transfer_history` 表（`ensure_transfer_history`）；`before/after_request` 钩子记录 `/static/inktime/` 请求；`esp_photo`/`esp_latest` 写拉取哨兵；新增 `POST /static/inktime/<key>/shutdown` |
| `scripts/inktime_daily.sh`（新建） | render → 按开关 BLE 推送 / 起临时 server → 轮询哨兵检测拉取或超时 → 关 server；lockdir 防重入；全量日志 |
| `esp32/ink-display-wifi-epd/ink-display-wifi-epd.ino` | `downloadAndRenderDailyPhoto` 加 `forcedIdx`/`logBuf` 参数 + `pushLog`；正常固定 idx=0；新增 `handleFetch`/`handleLog`/`handleDebug`；手动唤醒 web 窗口改为 debug 感知循环（初始 3min / debug 30min 自动休眠 / 两步关闭提示下载日志） |
| `CLAUDE.md` | 修正脚本名为 `inktime_daily.sh`；补充投递开关、`logs/transfer.log`、`tmp/last_fetch.json` |
| `render_daily_photo.py` | **未改动**（Q1=A） |

---

## 8. 验证清单

**树莓派侧（本机可验）**
- [ ] `python3 server.py` 起服务；`curl -o /dev/null -w "%{http_code}" http://127.0.0.1:8765/static/inktime/andyhome0203/photo_0.bin` 返回 200。
- [ ] `logs/transfer.log` 与 `transfer_history` 表各多一行；`tmp/last_fetch.json` 生成。
- [ ] `curl -X POST .../shutdown` 后 server 进程退出。
- [ ] `bash scripts/inktime_daily.sh`：`render.log` 出现 render → server 启动 → 拉取检测（curl 模拟）→ server 关闭；不模拟拉取时走超时分支关闭。

**ESP32 侧（需烧录硬件）**
- [ ] 定时唤醒：串口 `idx=0`，正常下载渲染，不起 web，进 deep sleep。
- [ ] 手动唤醒：`/debug?state=on` → `/fetch?idx=2` 返回日志且屏幕刷新 → `/log` 下载 → `/debug?state=confirm_off` 后进 deep sleep。
- [ ] debug 下调小 `DEBUG_IDLE_MS` 验证 30 分钟自动休眠。

---

## 9. 待办（TODO）

| 优先级 | 项 | 说明 |
|---|---|---|
| 中 | **远程唤醒 + 命令协议** | ESP32 deep sleep 下 WiFi 断电，**无法被网络包唤醒**。可行方案：提高定时唤醒频率（如每 15 分钟）轮询 server 的「待执行命令」；或外部 GPIO 触发 RTC 唤醒。本轮的 `transfer_history` 日志 + `/shutdown` 是该机制的**地基**。 |
| 中 | **漏拉重试/确认** | 树莓派据 `logs/transfer.log` 判断「今天 ESP32 没拉走」，配合远程唤醒触发重试。 |
| 低 | **ESP32 下载优化（2bpp）** | 若 Q1 后续选 B：render 出 2bpp 96KB 变体 + 固件改下载/解包 + 调和调色板顺序（render 0黑… vs Paint 0白…）。下载量降 4 倍。 |
| 低 | **多设备/多屏** | 当前 `ENABLE_*` 是全局开关；未来多台 ESP32 可扩展为设备列表 + 各自刷新时间。 |
| 低 | **舵机等功能** | `ENABLE_SERVO_CONTROL` 预留位，待硬件就绪。 |
