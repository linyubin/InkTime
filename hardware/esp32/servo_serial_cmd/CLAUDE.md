# CLAUDE.md — 给 AI 整合项目时的指南

本文件供 AI 助手在把这个舵机模块整合到其他 ESP32 项目时参考。
配套的 `README.md` 是给人类用户看的，本文件聚焦**架构、约束、踩过的坑、集成步骤**。

## 项目定位

这是一个**舵机控制模块**，设计成可被整合进更大的 ESP32 项目（如电子纸显示 + 定时器 + 舵机的 IoT 设备）。
核心是 `config` / `motion` / `webui` 三个模块 + 主文件的胶水代码（WiFi、WebServer、串口、休眠）。

## 文件职责（务必先读懂这个分层）

```
servo_serial_cmd.ino   ← 胶水层：WiFi、WebServer 路由、串口命令、深度休眠
config.h/.cpp          ← 数据层：Config 结构 + NVS 读写（唯一持久化入口）
motion.h/.cpp          ← 业务层：非阻塞运动规划器（位置/速度模式 + offset 套用）
webui.h/.cpp           ← 表现层：HTML/CSS/JS 字符串 + JSON 序列化/解析
```

**依赖方向**（重要，整合时不要打破）：
- `motion` 依赖 `config`（读 `g_cfg`），不依赖 `webui`
- `webui` 依赖 `config`（读写 `g_cfg`），不依赖 `motion`
- 主文件依赖三者，是唯一的组装点

整合到新项目时：**保留 config + motion 两层**（这是纯逻辑、可移植），按需重写 `webui` 或主文件的 WiFi/WebServer 部分。

## 全局状态：`g_cfg`（唯一配置入口）

`config.h` 定义 `extern Config g_cfg`，所有配置（模式、参数、WiFi ssid/pass）都在这一个结构体里。

**修改配置的唯一正确路径**：`parseControl(jsonString, errMsg)`（在 `webui.cpp`）。
它会做限幅、校验、并自动 `configSave()` 写 NVS。
- 网页路由 `/api/control` 走它
- 串口命令 `p/v/range/off/stop` 也构造 JSON 走它

**不要绕过 `parseControl` 直接改 `g_cfg`**——会漏掉限幅和持久化。

NVS 命名空间：`"servo"`（config.cpp 里 `NVS_NAMESPACE`）。整合时如果新项目也用 NVS，换不同命名空间避免冲突。

## motion 模块的非阻塞契约（最容易踩坑的地方）

### 1. `tick()` 必须 20ms 调一次，且绝不能用 `delay()`

```cpp
void loop() {
  server.handleClient();
  motion.tick();   // ← 内部用 millis() 算 dt，靠频繁调用推进
}
```

如果新项目的 `loop()` 里有长耗时操作（如刷电子纸、`delay(1000)`），会导致 tick 间隔变大，但**不会丢步**——`dt` 是实时算的，会自动补偿。但响应会变慢。

### 2. `Motion::disable()` 是停止舵机的唯一正确方法（不是 `myservo.detach()`！）

这是本模块**最隐蔽、踩了最多坑的设计**：

```cpp
// ❌ 错误：单靠 detach 无效
myservo.detach();
// 因为 loop 里 tick() 会调 _servo->write()，
// 而 ESP32Servo 库的 write() 在未 attach 时会"自动重连"舵机！
// 结果 detach 下一帧就被复活，舵机继续吃电保持。

// ✅ 正确：用 motion.disable()
motion.disable();
// 它会：1) detach 舵机  2) 设 _enabled=false
// tick() 开头 if(!_enabled) return，彻底斩断重连路径
```

整合时如果要做"舵机休眠/不工作时不吃电"，**必须调 `motion.disable()`**，对应的恢复用 `motion.enable()`（内部会重新 attach + applyOutput）。

### 3. offset 套在输出层，逻辑层不感知

```cpp
// motion.cpp applyOutput()
_outAngle = _curAngle + _cfg->offset;   // 输出层套 offset
_servo->write(_outAngle);
```

所有运动规划（位置/速度模式）只操作 `_curAngle`（逻辑角度），offset 在最后一层加。
这意味着：**改 offset 不需要重置运动状态，下一个 tick 自动套用**。

### 4. offset 语义：≥0，表示浮标 0° 对应舵机几度

不是简单叠加，而是**坐标系平移**：
- `offset=11` 意味着浮标 0° → 舵机物理 11°，浮标 90° → 舵机 101°
- 取值 ≥0（`parseControl` 里 `if(o<0) o=0`），不设上限
- 输出层不夹 `[0,180]`——因为用户的舵机可能是 360° 连续旋转，量程可能 >180。超量程由用户负责。

**如果你整合的是普通 180° 舵机**，且希望严格限幅，可以在 `applyOutput` 加回 `if(out>180) out=180;`。但要确认目标舵机量程。

## WiFi 配网的启动逻辑（主文件 setup）

```
configLoad()
  ├─ 有 ssid → connectSTA() [15秒超时]
  │     ├─ 成功 → 正常模式（开 web server，返回控制台）
  │     └─ 失败 → startAP() 配网模式
  └─ 无 ssid（首次）→ startAP() 配网模式
```

AP 模式特点：
- 热点名 `ESP32_Servo_Setup`（无密码）
- 启动 DNS captive portal（`DNSServer`），手机连上后自动弹出配网页
- 主页 `/` 根据 `g_apMode` 返回配网页或控制台

**整合到已有 WiFi 项目的处理**：如果新项目有自己的 WiFi 管理，**删掉主文件的 `connectSTA/startAP/handleSetWifi`**，只保留舵机相关代码。`g_apMode` 标志可一并删掉，`handleRoot` 直接返回 `getIndexHtml()`。

## 集成到其他项目的步骤

### 场景 A：新项目要舵机 + 网页控制（保留全部功能）

1. 复制 `config.h/.cpp`、`motion.h/.cpp`、`webui.h/.cpp` 到新项目文件夹。
2. 把 `servo_serial_cmd.ino` 的内容**合并进新项目的主 `.ino**（或拆成单独的 `.ino` 文件放同目录，Arduino 会自动合并）。
3. 检查引脚冲突：舵机用 IO13，可选电源控制用 IO27。若新项目占用了，改 `SERVO_PIN` 和 `#define SERVO_POWER_PIN`。
4. 检查 WiFi 管理冲突（见上节）。
5. 检查 NVS 命名空间冲突。

### 场景 B：新项目只要舵机控制，自己处理 UI/网络（最小集成）

只需复制 **`config.h/.cpp` + `motion.h/.cpp`**（4 个文件），用法：

```cpp
#include "config.h"
#include "motion.h"

Servo myservo;
Motion motion;

void setup() {
  configLoad();                          // 读上次参数
  ESP32PWM::allocateTimer(0);
  myservo.setPeriodHertz(50);
  myservo.attach(13, 500, 2400);
  motion.begin(&myservo, 13);
}

// 控制示例：直接改 g_cfg（注意：这样不写 NVS，不校验）
void gotoAngle(float target, float speed) {
  g_cfg.mode = MODE_POSITION;
  g_cfg.target = target;
  g_cfg.speed = speed;
  motion.applyConfigChange();            // 通知规划器重置状态
}

void loop() {
  motion.tick();   // 必须频繁调用
  // ... 你自己的代码，但别用长 delay
}
```

如果要持久化，自己调 `configSave()`（或保留 webui.cpp 用 `parseControl`）。

## 深度休眠整合（最重要的集成点）

主文件里有完整的休眠函数 `enterDeepSleepSeconds(uint32_t seconds)`，**这是给整合参考的样板**。
新项目整合时，把它的步骤移植到你自己的休眠函数里：

```cpp
void yourDeepSleep(uint32_t seconds) {
  prepareServoForDeepSleep();          // 1. 舵机 detach（省 ~9mA）
  // 你自己的：关 EPD、关传感器等
  WiFi.disconnect(true, true);          // 2. 关 WiFi（省 ~60mA，最大头）
  WiFi.mode(WIFI_OFF);
  esp_wifi_stop();
  esp_bt_controller_disable();          // 3. 关蓝牙
  setCpuFrequencyMhz(80);               // 4. 降频
  esp_sleep_enable_timer_wakeup((uint64_t)seconds * 1000000ULL);
  esp_deep_sleep_start();               // 5. 真正进休眠
}
```

**必须的 include**：`<esp_wifi.h>`、`<esp_bt.h>`、`<driver/rtc_io.h>`（否则 `esp_wifi_stop` 等编译报错）。

## 实测功耗数据（锂电池 5V 供电，TEST1 整板电流）

| 状态 | 电流 | 优化手段 |
|---|---|---|
| 运行态（WiFi省电+舵机保持） | ~120~140 mA | - |
| WiFi 关闭 | ~70 mA | `wifioff` |
| **深度休眠**（含舵机） | **~18.7 mA** | `enterDeepSleepSeconds` 全流程 |
| 深度休眠 + 舵机 detach | ~18.7 mA | 同上（detach 已含） |
| 深度休眠 + 拔舵机5V | ~16.0 mA | 硬件断电 |

**16mA 是软件优化的下限**，来自板载硬件：
- CH340G ~8~12mA（3.3V 供电，锂电池下仍工作）
- ASM1117 LDO ~3~5mA（静态电流）
- 电源指示灯 ~1~3mA
- Flash + 字库 ~1~2mA

软件能做的优化（已全部实施）：
- ✅ WiFi 省电模式 `WIFI_PS_MIN_MODEM`
- ✅ 休眠前关 WiFi + 蓝牙
- ✅ 休眠前 CPU 降到 80MHz
- ✅ 舵机 detach（不是 `myservo.detach()`，是 `motion.disable()`！）

## 踩过的坑（整合时务必注意）

### 1. Arduino 多文件规则
同一文件夹的所有 `.ino/.h/.cpp` 会被合并成一个 sketch。**不要把多个独立程序放同目录**（会重定义报错）。每个程序要有自己的同名文件夹（如 `servo_serial_cmd/servo_serial_cmd.ino`）。

### 2. ESP32Servo 库版本差异
- `attach` API 在 v2.x 和 v3.x 不同（v3 用 `allocateTimer` + `setPeriodHertz` + `attach`）
- `JsonDocument`（v7）vs `DynamicJsonDocument`（v6）
- 本代码基于 **ESP32Servo 3.x + ArduinoJson 7+ + Arduino-ESP32 3.x**
- WiFi 省电枚举：新版叫 `WIFI_PS_MIN_MODEM`（带 MODM 后缀），老版叫 `WIFI_PS_MIN_MODE`

### 3. `esp_wifi_set_ps` / `esp_wifi_stop` 要加头文件
`<esp_wifi.h>` 不会随 `<WiFi.h>` 自动包含，必须显式 include。

### 4. detach 不掉电的迷思
单靠 `myservo.detach()` 不能让舵机停止——因为 `tick` 里 `write()` 会自动重连。必须用 `motion.disable()`。详见上文 motion 模块第 2 点。

### 5. 网页轮询会覆盖用户输入
前端 500ms 轮询 `/api/state` 刷新输入框时，用户正在编辑的字段会被服务器旧值覆盖（导致"输负数不生效""保存总是旧值"）。
解法：用 `editingXxx` 标志，输入框 `onfocus` 置 true，应用/保存时置 false，轮询时跳过正在编辑的字段。

### 6. offset 不能为负（本项目的设计选择）
曾设计成可负，但产生歧义（负 offset 在逻辑 0° 时无法生效，会被量程下限夹住）。
最终改为：offset ≥0，语义为"浮标 0° 对应舵机物理角度"，纯坐标平移。

### 7. 引脚选择避坑
不要用 strapping pin（IO0/2/12/15）当信号脚——会影响启动。推荐 IO13/14/25/26/27/32/33。本代码舵机用 IO13，可选电源控制用 IO27（曾误用 IO12，已修正）。

## 测试用的串口命令（开发/调试时）

功耗/舵机行为相关的诊断命令（在 `handleSerialCmd`）：
- `detach` / `attach` — 测舵机 detach 省电效果
- `raw` — 底层 ledcDetach + pinMode(INPUT)，诊断 PWM 是否真停
- `wifioff` — 测 WiFi 功耗占比
- `deepsleep N` — 完整深度休眠 N 秒
- `sleep` — 跑休眠前清理函数但不真休眠

这些是**开发诊断用**，整合到生产项目时可以删掉对应的 `else if` 分支。

## 修改建议

如果 AI 整合时要做大改动：
1. **先读 `motion.h` 的注释**——它说明了设计意图（非阻塞、offset 套在输出层、_enabled 阻止重连）。
2. **改 `g_cfg` 走 `parseControl`**——除非有特殊需求，别绕过。
3. **保留 `motion.disable/enable`**——这是停止舵机的唯一正确方法。
4. **休眠流程参考 `enterDeepSleepSeconds`**——顺序很重要（先关外设再降频再进休眠）。
5. **加新参数**：往 `Config` 结构加字段 → `configLoad/Save` 加读写 → `parseControl` 加解析 → `buildStateJson` 加序列化 → 网页加输入框。五处都要改，漏一处就不同步。
