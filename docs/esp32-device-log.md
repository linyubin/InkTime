# ESP32 设备日志（黑盒）与上传

> 起因：2026-08-30 舵机突然转到机械零点但屏幕无变化，事后无任何记录可查。
> 设计决策与被否决方案见 [ADR-0001](adr/0001-esp32-flash-device-journal.md)。

## 它解决什么问题

设备绝大部分时间在深度睡眠，出事（掉电/崩溃/异常转动）时无人目睹、串口没接，
重启后现场全丢。黑盒日志把"发生了什么"的骨架即时写进设备 Flash，并在每次
入睡前推送到树莓派集中保存，跨重启可回查。

## 事件类型（14 种）

| 类型 | 时机 | 关键字段 |
|------|------|----------|
| `BOOT` | 每次开机第一条 | `reset=`（POWERON/BROWNOUT/PANIC/…WDT/SW_REBOOT/DEEPSLEEP_WAKE）、`wake=`（TIMER/POWERON）、`heap=` |
| `SLEEP` | 入睡前最后一条 | `next_min=` 下次唤醒分钟数、`heap=` |
| `WIFI` | connectWiFi 结果 | `ok ip=` / `fail ssid= timeout_ms=` |
| `AP_PORTAL` | 进配网模式 | `ssid= ok=` |
| `TIME_SYNC` | 授时成功（**锚点**）/失败 | `src=lan\|ntp anchor=<epoch>` |
| `PHOTO_JSON` | 朝向 sidecar 拉取 | `idx= ori=` / `fail idx= code=` |
| `PHOTO_BIN` | 照片 bin 拉取 | `ok/fail idx= ori= ms=`（耗时） |
| `RENDER` | 刷屏 | `ok` / `no_epd` |
| `SERVO_CMD` | 收到转动指令 | `target= speed=` / `home` |
| `SERVO_SKIP` | 同朝向跳过 | `ori= last=`（解释"为什么没转/停在原地"） |
| `SERVO_DONE` | 转动结果 | `ok/timeout ori=` |
| `SERVO_SKIP_UNCAL` | 未标定跳过 | — |
| `HTTP_HIT` | 设备 Web 端点被调用 | `ep=/fetch\|/servo_test\|/servo_save\|/servo_sync …` |
| `LOG_UP` | 日志上传本身 | `ok n= cur=` / `fail code=` |

## 存储与协议

- **设备侧**：LittleFS 双段滚动 `/devlog_a.log` + `/devlog_b.log`（各 32KB，合计
  ~64KB ≈ 500 条，离线 2~4 周不丢）。行格式 JSONL：
  `{"ls":行号,"b":开机序号,"ms":相对毫秒,"t":"类型","d":"明细"}`。
  每条即时落盘，崩溃前的最后一条也留得住；写满一段删另一段。
- **上传**：`goDeepSleepMinutes()`（所有休眠路径的唯一咽喉点）里，趁 WiFi 在线把
  `ls > 上传游标` 的增量按旧→新分批（每批 ≤6KB）POST 到
  `POST /api/device_log/<key>`；收到 200 才推进游标（存 NVS）。失败保留重传。
- **服务器侧**（server.py）：每行补 `rcv`（接收时刻）与 `ip`，追加
  `logs/device/<key>.log`；>5MB 轮转 `.log.1`；按 `ls` 去重（设备游标丢失重传时
  不会产生重复行）。
- **绝对时间**：设备深睡后 millis 清零，事件只有相对毫秒。`TIME_SYNC anchor=<epoch>`
  作为真实时间锚点，WebUI（和后续分析）用 `锚点 + (ms - 锚点ms)` 推算绝对时间。

## 怎么查

- **网页**：WebUI（8766 端口）`http://<树莓派>/devlog` —— 倒序 500 条，BOOT 行
  高亮，`reset=BROWNOUT` 等异常行标红。
- **文件**：`logs/device/andyhome0203.log`（一行一条 JSON，直接 grep/jq）。

## 烧录注意（重要）

- 分区方案用 **No OTA (Large APP)**（APP ~2MB + SPIFFS ~2MB）。Default 方案的
  APP 上限只有 1.2MB，固件已超（1.33MB）编译不过；两者都带 SPIFFS 分区，
  LittleFS 设备日志照常工作。若将来需要 OTA，可选 Minimal SPIFFS (Large APPS
  with OTA)（APP ~1.9MB）。
- 标准方案的 NVS 偏移一致，正常烧录不丢 WiFi 配置/舵机标定；但保险起见烧录前
  先在设备 Web 标定页抄下当前值。
- 保持 **Erase All Flash Before Sketch Upload: Disabled**；若手动 Erase 会连 NVS
  带标定一起擦掉。
- AP 配网模式（WiFi 连不上）不入睡，日志只留在设备本地，下次联网成功后补传。

## 事件覆盖范围（代码位置）

| 挂钩点 | 事件 |
|--------|------|
| `device_log.cpp journalBegin()`（setup 3.5 步） | BOOT |
| `connectWiFi()` | WIFI |
| `startConfigPortal()` | AP_PORTAL |
| `fetchServerTime()` / `syncTime()` | TIME_SYNC |
| `fetchPhotoOrientation()` | PHOTO_JSON |
| `fetchPhotoBinToFramebuffer()` | PHOTO_BIN |
| `displayFramebuffer()` | RENDER |
| `applyServoForOrientation()` | SERVO_CMD / SERVO_SKIP / SERVO_DONE / SERVO_SKIP_UNCAL |
| `handleFetch/handleServoTest/handleServoSave/handleServoSync` | HTTP_HIT |
| `goDeepSleepMinutes()` | SLEEP + 上传 |
| `journalUpload()` | LOG_UP |
