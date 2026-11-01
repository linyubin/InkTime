# ESP32-L 舵机 Web 控制台

基于 ESP32-L 开发板的舵机（SG90 / 360° 连续旋转舵机）Web 控制台。
支持网页 + 串口双通道控制，内置 AP 配网、参数断电保存、深度休眠功耗优化。

> **⚠️ 与 InkTime 主项目的整合关系**
>
> 这个目录是一个**独立的通用舵机控制台**——开发阶段的标定/功耗测试工具。
> 它的 `config.{h,cpp}` + `motion.{h,cpp}` 两层是按"可移植"设计的（详见 `CLAUDE.md`）。
>
> InkTime 的"横屏相框自动旋转"功能已**精简整合**到主显示固件
> [`esp32/ink-display-wifi-epd/`](../ink-display-wifi-epd/)：
> - 单 ESP32-L 同时驱动墨水屏 + 舵机（舵机信号脚 **IO25**，避开 EPD 的 SPI 占用）。
> - 整合方式参考了本目录 `CLAUDE.md` 的"场景 B 最小集成"，但**进一步精简**：
>   只保留"转到绝对角度、转完 detach 撒手"这一最小职责（自写 `servo_rotate.{h,cpp}` 约 50 行），
>   不搬 `webui.cpp` 那套通用控制台（velocity/range/往复/串口命令在主项目都用不到）。
> - 标定在主固件的 debug 页做（`/servo?test=show`），不依赖本目录的 Web 控制台。
>
> **本目录保留为独立项目**，用于：
> - 单独标定/验证舵机硬件（不接墨水屏时）
> - 功耗实测（`deepsleep`/`wifioff`/`detach` 等诊断命令）
> - 作为整合到其他 ESP32 项目的参考样板
>
> 完整设计方案见项目根目录 `docs/plans/2026-07-25-frame-rotation-design.md`。

## 功能

- **两种工作模式**
  - **位置控制**：设目标角度 `target` 和移动速度 `speed(°/s)`，平滑走到目标停下。
  - **速度控制**：设角速度 `velocity(°/s, 可负)`。`range=360` 时真连续旋转；`range<360` 时端点间定速往复。
- **范围设定** `range`（默认 360）：影响 target 限幅和速度模式是否往复。
- **零位偏移校正** `offset`（°，≥0）：浮标 0° 对应舵机物理角度。最终输出 = 逻辑角度 + offset。
- **WiFi 配置**：支持 AP 配网（首次）+ 网页/串口双通道改 WiFi（写 NVS，保存后自动重启）。
- **参数断电保存**：所有参数用 NVS(Preferences) 存储，开机自动恢复。
- **深度休眠支持**：内置完整的休眠前清理流程 + 功耗测试命令。

## 硬件接线

| 舵机线 | 接 ESP32-L 排针 | 说明 |
|---|---|---|
| 🟠 橙色（信号） | **IO13** | P7 IOL 上的 `T4 IO13` |
| 🔴 红色（电源） | **5V** | P9 POWER 的 Pin 1 或 2 |
| 🟤 棕色（GND） | **GND** | P9 POWER 的 Pin 3 或 4（必须共地） |

> 可选：舵机电源受控开关（P-MOSFET）。实测仅省 ~2.7mA，性价比低，默认不启用。需要时取消 `servo_serial_cmd.ino` 里 `#define SERVO_POWER_PIN 27` 的注释。

## 安装

### 1. 安装依赖库（Arduino IDE → 工具 → 管理库）

- **ESP32Servo**（Kevin Harrington, John K. Bennett）
- **ArduinoJson** v7+（Benoit Blanchon）

> 自带库（无需安装）：`WiFi`、`WebServer`、`DNSServer`、`Preferences`，以及 `esp_wifi.h` / `esp_bt.h`（Arduino-ESP32 3.x）。

### 2. 烧录

1. **文件 → 打开**，进入 `servo_serial_cmd/` 文件夹，打开 `servo_serial_cmd.ino`（IDE 顶部应显示 7 个标签页）。
2. **工具** 菜单：开发板选 `ESP32 Dev Module`，端口选对应 COM 口，Upload Speed `921600`。
3. 点 **上传**。若未自动进入下载模式，按住 BOOT + 按一下 RST + 松开 RST + 松开 BOOT。

## 首次使用（WiFi 配网）

烧录后**无需改代码**，按以下步骤配网：

1. 上电，串口（115200）显示进入 **AP 配网模式**，热点名 `ESP32_Servo_Setup`。
2. 手机连这个热点（无密码）。
3. 手机自动弹出配网页（未弹则浏览器访问 `192.168.4.1`）。
4. 填 WiFi 名/密码，点保存。ESP32 自动重启连接。
5. 串口打印新 IP（如 `192.168.1.xxx`），手机切回 WiFi 后浏览器打开该 IP = 控制台。

切换 WiFi 同理：控制台底部"WiFi 设置"卡片，或串口 `wifi <ssid> <pass>`。

## 串口命令（115200，换行符设 NL/CR）

| 命令 | 作用 |
|---|---|
| `?` | 查看当前状态（模式/位置/range/offset/WiFi） |
| `stop` | 停止（切 IDLE） |
| `p 90 60` | 位置模式：转到 90°，速度 60°/s |
| `p 45` | 位置模式：转到 45°，速度用上次的 |
| `v 60` | 速度模式：60°/s 持续转 |
| `v -60` | 速度模式：反转 60°/s |
| `range 360` | 设可转范围 |
| `off 2.5` | 设零位偏移 2.5°（立即保存到 Flash） |
| `wifi mi_0203 abc` | 设 WiFi 并重启连接 |
| `scan` | 重试连 WiFi |
| `detach` | 停止舵机 PWM（测功耗用，舵盘失去保持力） |
| `attach` | 恢复舵机 PWM |
| `raw` | 用底层 API 彻底切断 IO13（诊断用） |
| `wifioff` | 关闭 WiFi（测运行态功耗用） |
| `deepsleep 30` | 深度休眠 30 秒（测休眠功耗用，到点自动唤醒） |
| `sleep` | 跑一遍休眠前清理函数（不真休眠，测舵机反应） |

## 网页 API（HTTP）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | AP 模式返回配网页，STA 模式返回控制台 |
| GET | `/api/state` | 返回当前状态 JSON |
| POST | `/api/control` | 控制指令（模式/参数），见下 |
| POST | `/api/wifi` | 设置 WiFi，保存后自动重启 |
| POST | `/api/sleep` | 进入深度休眠，body `{"seconds":30}` |

### `GET /api/state` 返回示例

```json
{
  "mode": 1,
  "position": 45.2,
  "output": 56.2,
  "range": 360,
  "offset": 11,
  "target": 90,
  "speed": 60,
  "velocity": 0,
  "running": true,
  "ip": "192.168.1.50",
  "ssid": "mi_0203"
}
```

- `position`：逻辑角度（用户视角）
- `output`：实际写到舵机的角度 = position + offset

### `POST /api/control` 请求体（字段全部可选）

```json
{
  "mode": 1,
  "action": "start",
  "target": 90,
  "speed": 60,
  "velocity": 0,
  "range": 360,
  "offset": 11,
  "save": false
}
```

字段说明：
- `mode`：0=IDLE, 1=POSITION, 2=VELOCITY
- `target`：位置模式目标角度（限幅到 `[0, range]`）
- `speed`：位置模式移动速度 °/s（1~360）
- `velocity`：速度模式角速度 °/s（-360~360，负=反转）
- `range`：可转范围（1~360）
- `offset`：零位偏移（≥0）
- `save`：true 时强制写 NVS（默认有变更就写）

## 文件结构

```
servo_serial_cmd/
├── servo_serial_cmd.ino   # 主文件：WiFi + WebServer 路由 + 串口命令 + 深度休眠
├── config.h / config.cpp  # 参数定义 + NVS 读写（含 WiFi ssid/pass）
├── motion.h / motion.cpp  # 非阻塞运动规划器 + offset 套用
└── webui.h / webui.cpp    # 网页 HTML/CSS/JS + JSON 序列化/解析
```

> Arduino 规则：同一文件夹下所有 `.ino/.h/.cpp` 会被合并成一个 sketch。所以这 4 组文件必须在同一文件夹，且打开时用 **文件 → 打开** 选 `.ino`。

## 关键设计点

- **非阻塞运动规划**：`motion.tick()` 用 `millis()` 算 dt，绝不用 `delay()`，否则网页卡死。tick 间隔 20ms（和舵机 50Hz 一致）。
- **offset 套在输出层**：`applyOutput()` 里 `out = curAngle + offset`，逻辑层全程不感知 offset，干净。
- **速度模式自动区分行为**：`range>=360` 真连续旋转（角度回绕）；`range<360` 端点往复。
- **detach 必须配合 `_enabled` 标志**：ESP32Servo 库的 `write()` 在未 attach 时会自动重连，所以单靠 `myservo.detach()` 无效。`Motion::disable()` 会设 `_enabled=false`，`tick()` 开头 `if(!_enabled) return` 才能真正阻止重连。
- **WiFi 省电模式**：STA 连接时调 `esp_wifi_set_ps(WIFI_PS_MIN_MODEM)`，运行态电流从 ~130mA 降到 ~90mA。

## 实测功耗（5V 锂电池供电，TEST1 测整板电流）

| 状态 | 电流 |
|---|---|
| 运行态（web 控制时，WiFi 省电 + 舵机保持） | ~120~140 mA |
| WiFi 关闭后（舵机保持 + CPU） | ~70 mA |
| 深度休眠（含舵机） | **~18.7 mA** |
| 深度休眠 + 拔舵机 5V | ~16.0 mA |

> 16mA 的下限来自板载硬件（CH340 ~8~12mA、ASM1117 LDO ~3~5mA、电源 LED ~1~3mA、Flash/字库漏电 ~1~2mA），软件无法继续优化。2220mAh 电池约 **5~6 天续航**。要突破需硬件改造（拆 LED、换低静态 LDO）或换低功耗开发板。

## 已知限制

- **offset 上限未约束**：若 `offset + range > 舵机物理量程`，超出部分舵机会顶死。用户需自行确保合理。
- **SG90 物理分辨率约 1~2°**：小于 3° 的命令舵机可能不响应（非 bug）。
- **5GHz WiFi 不支持**：ESP32 只支持 2.4GHz，双频合一路由器需分开 SSID。

## 开发板与硬件参考

- 主板：ESP32-L（Good Display，型号 ESP32-L(C02)）
- 舵机：SG90（9g 微型，橙/红/棕三线）
- 电流测试点：板载 `TEST1`（电源开关拨 OFF 后，万用表电流档搭两针）
