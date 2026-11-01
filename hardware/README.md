# InkTime 硬件层（hardware/）

本目录收纳全部硬件相关内容：各硬件的固件、PCB 设计文件、驱动库、供电与元器件记录。
软件/服务端见仓库根 [README.md](../README.md) 与 `inktime/` 包。

## 一、系统拓扑

```text
┌─────────────────────────── 服务端（树莓派，inktime/ 包）───────────────────────────┐
│  NAS 照片库(CIFS) → analyze_photos 分析 → photos.db → render_daily_photo 渲染      │
│  server.py 常驻（HTTP :8765）：/static/inktime/<key>/… 供拉取 + /time 授时          │
└──────────────┬────────────────────────────────────────────┬───────────────────────┘
               │ ① HTTP 拉取（设备主动 GET，每日定时唤醒）      │ ② BLE 推送
               ▼                                            ▼
   ┌─────────────────────────────┐              ┌──────────────────────────┐
   │ ESP32 墨水屏相框（主力硬件）   │              │ nRF5 BLE 墨水屏           │
   │ 固件 ink-display-wifi-epd    │              │ 固件 EPD-nRF5（上游项目）  │
   │ 舵机横竖屏旋转 + 设备日志/注册 │              │ push_to_epd_ble.py 推送   │
   └──────────────┬──────────────┘              └────────────┬─────────────┘
                  ▼                                          ▼
        7.3″ 四色墨水屏 EL073TS3                     4.2″ / 7.5″ 黑白/三色/四色屏
```

两套投递通道按 `config.py` 的 `ENABLE_ESP32_SERVE` / `ENABLE_BLE_PUSH` 开关并存或单选。

## 二、目录与固件一览

| 路径 | 状态 | 主控 | 屏幕 | 屏幕驱动 | 投递方式 |
|---|---|---|---|---|---|
| `esp32/ink-display-wifi-epd/` | ✅ **主力维护** | ESP32-L（普通 ESP32，**无 PSRAM** 亦可） | 7.3″ 四色 EL073TS3（49-pin） | 厂商 `EPD_SPI`（固件内自带 `EPD.cpp/.h`） | HTTP 拉取 + 相框舵机旋转 + 设备 Web 调试 |
| `esp32/ink-display-7C-photo/` | 历史（原作者方案） | ESP32-S3-N8R8（**需 PSRAM** ≥384K） | 7.3″ 四色 GDEY073D46 | GxEPD2（`GxEPD2_730c_GDEY073D46`） | 服务器 BLE 推送 / 拉取 |
| `esp32/ink-display-133C-photo/` | 历史 | ESP32-S3（需 PSRAM） | 13.3″ | GxEPD2 | 同 7C-photo |
| `esp32/servo_serial_cmd/` | 舵机调试工具 | ESP32 | — | — | 串口/WebUI 控制舵机（见其自带 `README.md`） |
| `esp32/pcb/` | PCB 设计文件 | — | — | — | 7C-photo 自研主板：原理图 / BOM / Gerber / 立创工程 |
| `esp32/libraries/ESP32epdx/` | vendored 驱动库 | — | — | — | ESP32epdx Paint/PIC 风格驱动参考实现 |
| `esp32/esp32-power-hardware.md` | 供电与元器件记录 | — | — | — | 实物整机供电架构、理想二极管改造 |
| `EPD-nRF5/` | nRF5 固件（上游 [tsl0922/EPD-nRF5] 原样保留） | nRF51822 / 51802 / 52810 / 52811 | 4.2″ / 7.5″ 黑白/三色/四色 | UC81xx / SSD16xx 系 | BLE 接收（Web Bluetooth 控制 + OTA） |

> Arduino IDE 要求 sketch 文件夹名与 `.ino` 同名——本目录各固件文件夹名均保持与 `.ino` 一致，可直接打开编译。

## 三、元器件清单（按硬件）

### 1）ESP32 相框 · 实物主力（GoodDisplay ESP32-L 套件）

| 部件 | 型号/说明 |
|---|---|
| 主板 | GoodDisplay **ESP32-L**（ESP32-WROOM-32 模组、CH340G 串口、GT30L32S4W 字库芯片） |
| 屏幕驱动转接板 | GoodDisplay **DESPI-C02**（主板 P5 排针连接，板上电荷泵产生 VGH/VGL） |
| 墨水屏 | 7.3″ 四色 EL073TS3（49-pin） |
| 舵机 | 普通 PWM 舵机（如 SG90），**独立 5V 供电**（勿从 ESP32 3.3V 取电） |
| 供电 | LM66200 双通道理想二极管（双电源 ORing）+ 5V 锂电池组 4000mAh + USB 5V 适配器 |

整机供电架构、电源轨图、理想二极管改造记录见 **[`esp32/esp32-power-hardware.md`](esp32/esp32-power-hardware.md)**。

### 2）ESP32 相框 · 7C-photo 自研板（历史方案）

| 部件 | 型号/说明 |
|---|---|
| 主控 | ESP32-S3-N8R8 模组（需至少 384K PSRAM），任意带 PSRAM 的成品板亦可 |
| 屏幕 | 7.3″ 四色 EL073TS3（49-pin），GxEPD2 驱动 |
| 转接板 | B 站"记得带马扎"七色 EPD 转接板（49-pin）；多数 24-pin SPI 转接板亦可兼容 |
| 设计文件 | `esp32/pcb/`：`Schematic.pdf`、`BOM.csv`、`Gerber/`、`InkTime_JLC_EDA.zip`、`PickAndPlace.xlsx` |

原理图中 H1–H6 为测试焊盘：H1 UART、H2 USB、H3 BOOT（烧录时短接 GND）、H4 接 EPD 转接板、H5 3.7V 电池、H6 5V 输入。
按键：SW1 RESET（重启即拉取刷新）、SW2 WiFi 重置（与 SW1 同按清 NVS）、SW3/SW4 备用。

### 3）nRF5 屏（EPD-nRF5）

- 主控：nRF51822 / nRF51802 / nRF52810 / nRF52811（低功耗蓝牙）。
- 屏幕：常见 4.2″ / 7.5″ 黑白、三色、四色屏（UC81xx / SSD16xx 驱动系列）。
- 唤醒方式支持 NFC / 无线充电，支持蓝牙 OTA。
- 编译：Keil uVision（`EPD-nRF5/Keil/`）或 ARM GCC（`Makefile`）；Web 控制端 `EPD-nRF5/html/index.html`（Web Bluetooth）。

## 四、引脚定义

### ink-display-wifi-epd（主力，定义在 `ink-display-wifi-epd.ino` 顶部）

| 信号 | GPIO | | 其它 | GPIO |
|---|---|---|---|---|
| `PIN_EPD_BUSY` | 13 | | `PIN_FACTORY_RESET` | 0（BOOT 键长按清 NVS） |
| `PIN_EPD_RST` | 12 | | `LED_BUILTIN` | 2 |
| `PIN_EPD_DC` | 14 | | 舵机 | 见 `servo_rotate.h` |
| `PIN_EPD_CS` | 27 | | | |
| `PIN_EPD_SCLK` | 18 | | | |
| `PIN_EPD_DIN` | 23 | | | |

### 7C-photo（历史方案）

| 信号 | GPIO |
|---|---|
| `PIN_EPD_BUSY` | 14 |
| `PIN_EPD_RST` | 13 |
| `PIN_EPD_DC` | 12 |
| `PIN_EPD_CS` | 11 |
| `PIN_EPD_SCLK` | 10 |
| `PIN_EPD_DIN` | 9 |

> 两套方案引脚差异较大（BUSY 7C=14 / wifi-epd=13，CS 7C=11 / wifi-epd=27 等），烧录前务必核对所用固件对应的引脚表。

## 五、编译烧录与标定

编译烧录、首次配网、Web 调试页、舵机标定流程与首次部署自检清单见仓库根 [README.md](../README.md) 的「编译与烧录」「首次配置」「相框标定流程」「首次部署自检清单」各节。

固件侧 WiFi 凭据格式有回归测试守护：`tests/test_firmware_wifi_credentials.py`（源码级检查，不编译固件）。

[tsl0922/EPD-nRF5]: https://github.com/tsl0922/EPD-nRF5
