/*
 * ESP32-L 舵机 Web 控制台（支持 AP 配网 + 串口/网页改 WiFi）
 *
 * 启动逻辑：
 *   1. 读 NVS 里的 WiFi 配置
 *   2. 有配置 → 尝试 STA 连 15 秒；连不上 → 进 AP 配网模式
 *   3. 无配置（首次）→ 直接进 AP 配网模式
 *   AP 模式下：手机连热点 "ESP32_Servo_Setup"，访问 192.168.4.1 填 WiFi
 *
 * 改 WiFi 的两种方式（都写 NVS，保存后自动重启）：
 *   - 网页：控制台里"WiFi 设置"卡片，或配网页面 /api/wifi
 *   - 串口：wifi <ssid> <pass>
 *
 * 接线：舵机 橙/红/棕 -> IO13 / 5V / GND
 *
 * 文件结构（同文件夹会被 Arduino 合并成一个 sketch）：
 *   servo_serial_cmd.ino  ← 本文件：WiFi + WebServer + loop
 *   config.h/.cpp         ← 参数定义 + NVS 读写（含 WiFi ssid/pass）
 *   motion.h/.cpp         ← 非阻塞运动规划器
 *   webui.h/.cpp          ← 网页 HTML + JSON
 *
 * 依赖库（库管理器安装）：
 *   - ESP32Servo
 *   - ArduinoJson  (v7+)
 */

#include <WiFi.h>
#include <WebServer.h>
#include <ESP32Servo.h>
#include <ArduinoJson.h>
#include <DNSServer.h>          // captive portal 用：让任意域名都解析到自己
#include <esp_wifi.h>           // esp_wifi_stop() 等底层 WiFi 控制 API
#include <esp_bt.h>             // esp_bt_controller_disable() 蓝牙关闭
#include <driver/rtc_io.h>      // rtc_gpio 强制设脚位状态（休眠时保持）

#include "config.h"
#include "motion.h"
#include "webui.h"

// ---- AP 配网模式的参数 ----
const char* AP_SSID = "ESP32_Servo_Setup";   // 配网热点名（无密码，方便连）
const byte  DNS_PORT = 53;
IPAddress   AP_IP(192, 168, 4, 1);

const int SERVO_PIN = 13;

// 舵机电源控制脚（可选：接 P-MOSFET high-side 开关）
// 实测：detach 已能省 9.2mA，MOSFET 在此基础上只能再省 2.7mA，性价比低。
// 除非舵机有持续漏电问题，否则保持注释（不启用），靠 detach 就够了。
// 如果要启用，取消下一行注释，并在 IO27 接 MOSFET 开关电路。
// #define SERVO_POWER_PIN 27

Servo      myservo;
WebServer  server(80);
DNSServer  dnsServer;          // captive portal：手机连上热点会自动弹出配网页
Motion     motion;

// 舵机上电/断电控制
void servoPowerOn() {
#ifdef SERVO_POWER_PIN
  digitalWrite(SERVO_POWER_PIN, HIGH);
  delay(50);   // 等电源稳定 + 舵机内部电容充电
#endif
}
void servoPowerOff() {
#ifdef SERVO_POWER_PIN
  digitalWrite(SERVO_POWER_PIN, LOW);
#endif
}

// 进入深度休眠前的舵机清理（在 esp_deep_sleep_start() 之前调用）
// 1. 停止 PWM 输出（motion.disable 会 detach 并阻止 tick 自动重连）
// 2. 信号线设为低，避免浮空导致舵机内部电路误触发
// 3. 断开舵机电源（需要 MOSFET 开关硬件，没装则这步无效）
void prepareServoForDeepSleep() {
  motion.disable();                      // 停止 PWM 输出 + 阻止 tick 重连
  pinMode(SERVO_PIN, OUTPUT);
  digitalWrite(SERVO_PIN, LOW);          // 信号线确定拉低
  servoPowerOff();                       // 舵机断电（0 mA，需 MOSFET 硬件）
  delay(20);
}

// === 完整的深度休眠流程（带定时唤醒）===
// 用于：1) 功耗测试  2) 将来融合到主项目的参考实现
// 秒数：休眠时长，到点自动唤醒复位重启
void enterDeepSleepSeconds(uint32_t seconds) {
  if (seconds < 1) seconds = 1;
  Serial.printf("[SLEEP] 即将深度休眠 %d 秒，期间测 TEST1 电流\n", (int)seconds);

  // 1. 舵机断信号 + 断电（detach 省 ~9mA，已验证）
  prepareServoForDeepSleep();

  // 2. 关闭 web server
  server.close();

  // 3. 关闭 WiFi（省 ~60mA，最大头）
  WiFi.disconnect(true, true);
  WiFi.mode(WIFI_OFF);
  esp_wifi_stop();

  // 4. 关蓝牙（即使没用也关掉，省漏电）
#if defined(CONFIG_BT_ENABLED)
  esp_bt_controller_disable();
#endif

  // 5. 降低 CPU 频率（省几 mA，休眠前最后做）
  setCpuFrequencyMhz(80);

  // 6. 配置定时唤醒
  uint64_t us = (uint64_t)seconds * 1000000ULL;
  esp_sleep_enable_timer_wakeup(us);

  // 7. 进入深度休眠（执行后 CPU 停止，到点自动复位重启）
  Serial.println("[SLEEP] 进入深度休眠...");
  Serial.flush();   // 确保串口打印完
  delay(50);
  esp_deep_sleep_start();
  // 不会执行到这里
}


bool g_apMode = false;         // 当前是否处于 AP 配网模式（影响 / 返回哪个页面）

// ---- WiFi 设置接口（AP 和 STA 模式共用）----
// POST /api/wifi  body: {"ssid":"...","pass":"..."}
void handleSetWifi() {
  String body = server.arg("plain");
  JsonDocument doc;
  auto err = deserializeJson(doc, body);
  if (err) { server.send(400, "text/plain", "JSON 解析失败"); return; }

  const char* ssid = doc["ssid"];
  const char* pass = doc["pass"] | "";
  if (!ssid || strlen(ssid) == 0) {
    server.send(400, "text/plain", "ssid 不能为空");
    return;
  }

  setWifiConfig(ssid, String(pass));   // 写 NVS
  server.send(200, "text/plain", "OK, rebooting");

  Serial.printf("WiFi 配置已更新: %s，1秒后重启...\n", ssid);
  delay(1000);
  ESP.restart();                       // 保存后自动重启，用新配置连接
}

// ---- 控制台主页：AP 模式返回配网页，STA 模式返回控制台 ----
void handleRoot() {
  if (g_apMode) {
    server.send_P(200, "text/html", getSetupHtml());
  } else {
    server.send_P(200, "text/html", getIndexHtml());
  }
}

void handleState() {
  String ip = (WiFi.status() == WL_CONNECTED) ? WiFi.localIP().toString() : String("");
  String json = buildStateJson(motion.getLogicalAngle(), motion.isRunning(), ip,
                               motion.getOutputAngle());
  server.sendHeader("Cache-Control", "no-store");
  server.send(200, "application/json", json);
}

// ---- 深度休眠接口（网页触发）----
// POST /api/sleep  body: {"seconds": 30}
// 先回复 HTTP 200，再延迟一点进休眠（确保客户端收到响应）
void handleSleep() {
  String body = server.arg("plain");
  JsonDocument doc;
  uint32_t secs = 30;   // 默认 30 秒
  if (!deserializeJson(doc, body)) {
    if (doc.containsKey("seconds")) secs = doc["seconds"].as<uint32_t>();
  }
  if (secs < 1) secs = 1;
  if (secs > 86400) secs = 86400;   // 上限 24 小时

  // 先回复，让网页知道指令已收到
  char msg[80];
  snprintf(msg, sizeof(msg), "OK, deep sleep %u seconds. WiFi将断开，网页会失联。", (unsigned)secs);
  server.send(200, "text/plain", msg);

  Serial.printf("[WEB] 收到网页休眠请求: %u 秒\n", (unsigned)secs);
  // 延迟 500ms 确保 HTTP 响应发送完毕，再进休眠
  server.handleClient();   // 让 WebServer 把响应推出去
  delay(500);
  enterDeepSleepSeconds(secs);
}

void handleControl() {
  String body = server.arg("plain");
  String err;
  if (!parseControl(body, err)) {
    server.send(400, "text/plain", err);
    return;
  }
  motion.applyConfigChange();
  server.send(200, "text/plain", "OK");
}

// captive portal：所有未知请求都重定向到首页（让手机自动弹出配网页）
void handleNotFound() {
  if (g_apMode) {
    // 重定向到 / ，手机/电脑会自动打开配网页
    server.sendHeader("Location", String("http://") + AP_IP.toString(), true);
    server.send(302, "text/plain", "");
  } else {
    server.send(404, "text/plain", "Not Found");
  }
}

// ---- 尝试 STA 连接（带 15 秒超时）----
bool connectSTA() {
  if (!hasWifiConfig()) return false;

  Serial.printf("正在连接 WiFi: %s ...\n", g_cfg.ssid);
  WiFi.mode(WIFI_STA);
  // 开启 WiFi 省电模式（PS_MIN_MODEM）：连上后空闲电流从 ~80mA 降到 ~20mA
  // 代价是网页响应略慢（~200ms），对舵机控制无影响
  // 注意：老版 IDF 叫 WIFI_PS_MIN_MODE，新版（含 Arduino-ESP32 3.x）叫 WIFI_PS_MIN_MODEM
  esp_wifi_set_ps(WIFI_PS_MIN_MODEM);
  WiFi.begin(g_cfg.ssid, g_cfg.pass);

  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 15000) {
    delay(300);
    Serial.print(".");
  }
  Serial.println();

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("WiFi 连接失败");
    return false;
  }
  Serial.print("WiFi 已连接，IP: ");
  Serial.println(WiFi.localIP());
  return true;
}

// ---- 进入 AP 配网模式 ----
void startAP() {
  g_apMode = true;
  Serial.println("进入 AP 配网模式");
  WiFi.mode(WIFI_AP);
  WiFi.softAPConfig(AP_IP, AP_IP, IPAddress(255, 255, 255, 0));
  WiFi.softAP(AP_SSID);                 // 无密码，方便手机连
  Serial.printf("热点: %s\n", AP_SSID);
  Serial.printf("手机连上后访问 http://%s\n", AP_IP.toString().c_str());

  // 启动 DNS：把所有域名解析到 AP_IP（captive portal 关键）
  dnsServer.start(DNS_PORT, "*", AP_IP);
}

void setup() {
  Serial.begin(115200);
  delay(200);

  // 1. 载入持久化参数（含 WiFi ssid/pass）
  configLoad();

  // 2. 舵机初始化
#ifdef SERVO_POWER_PIN
  pinMode(SERVO_POWER_PIN, OUTPUT);
#endif
  servoPowerOn();                     // 先给舵机通电（如果没装开关硬件，这步是空操作）
  ESP32PWM::allocateTimer(0);
  myservo.setPeriodHertz(50);
  myservo.attach(SERVO_PIN, 500, 2400);
  motion.begin(&myservo, SERVO_PIN);
  // 上电后强制 IDLE，避免 NVS 里存的 mode=POSITION/VELOCITY 导致自动跑
  // （IDLE 时 applyOutput 仍维持当前位置，保持舵盘稳定）
  g_cfg.mode = MODE_IDLE;
  motion.applyConfigChange();

  // 3. WiFi：有配置试 STA，连不上/没配置进 AP 配网
  bool connected = false;
  if (hasWifiConfig()) {
    connected = connectSTA();
  }
  if (!connected) {
    startAP();
  }

  // 4. 启动 web server（AP 和 STA 模式路由一样，靠 g_apMode 切换首页）
  server.on("/",            HTTP_GET,  handleRoot);
  server.on("/api/state",   HTTP_GET,  handleState);
  server.on("/api/control", HTTP_POST, handleControl);
  server.on("/api/wifi",    HTTP_POST, handleSetWifi);
  server.on("/api/sleep",   HTTP_POST, handleSleep);
  server.onNotFound(handleNotFound);
  server.begin();

  Serial.println("========================================");
  if (g_apMode) {
    Serial.println("【配网模式】");
    Serial.printf("1. 手机连热点: %s\n", AP_SSID);
    Serial.printf("2. 浏览器打开: http://%s\n", AP_IP.toString().c_str());
    Serial.println("3. 填 WiFi 信息保存，设备自动重启连接");
  } else {
    Serial.println("【正常模式】Web server 已启动");
    Serial.printf("浏览器打开: http://%s\n", WiFi.localIP().toString().c_str());
  }
  Serial.println("串口命令: ? wifi<ssid> <pass> p<target>[speed] v<vel> range<n> off<n>");
  Serial.println("          stop scan wifioff deepsleep[秒] detach attach raw");
  Serial.println("========================================");
}

// ---- 串口调试命令 ----
//   ?                打印当前状态
//   stop             停止
//   p 90 60          位置模式：转到 90°，速度 60°/s
//   v 60             速度模式：60°/s（负数反转）
//   range 360        设可转范围
//   off 2.5          设零位偏移（保存）
//   wifi mi_0203 abc 设 WiFi 并重启连接
//   scan             重试连 WiFi
String serialInput = "";

void handleSerialCmd(const String& raw) {
  String cmd = raw;
  cmd.trim();
  if (cmd.length() == 0) return;

  int sp = cmd.indexOf(' ');
  String op = (sp < 0) ? cmd : cmd.substring(0, sp);
  op.toLowerCase();
  String argStr = (sp < 0) ? "" : cmd.substring(sp + 1);
  argStr.trim();

  String body;
  String err;

  if (op == "?") {
    Serial.println("---- 当前状态 ----");
    Serial.printf("mode=%d  pos=%.1f  range=%.0f  offset=%.1f\n",
                  (int)g_cfg.mode, motion.getLogicalAngle(), g_cfg.range, g_cfg.offset);
    Serial.printf("target=%.1f  speed=%.0f  velocity=%.1f\n",
                  g_cfg.target, g_cfg.speed, g_cfg.velocity);
    Serial.printf("WiFi配置: %s\n", hasWifiConfig() ? g_cfg.ssid : "(未配置)");
    if (g_apMode) {
      Serial.printf("AP模式 热点:%s  配网页:http://%s\n", AP_SSID, AP_IP.toString().c_str());
    } else {
      Serial.printf("STA %s  IP:%s\n",
                    WiFi.status() == WL_CONNECTED ? "已连接" : "未连接",
                    WiFi.status() == WL_CONNECTED ? WiFi.localIP().toString().c_str() : "-");
    }
    Serial.println("------------------");
    return;
  }
  if (op == "wifi") {
    // wifi <ssid> <pass>
    int sp2 = argStr.indexOf(' ');
    if (sp2 < 0) {
      Serial.println("用法: wifi <ssid> <pass>");
      return;
    }
    String ssid = argStr.substring(0, sp2);
    String pass = argStr.substring(sp2 + 1);
    setWifiConfig(ssid, pass);
    Serial.printf("WiFi 已存: %s，1秒后重启连接...\n", ssid.c_str());
    delay(1000);
    ESP.restart();
    return;
  }
  if (op == "stop") {
    body = "{\"mode\":0}";
  }
  else if (op == "p" || op == "pos") {
    float t = 0, s = g_cfg.speed;
    int s2 = argStr.indexOf(' ');
    if (s2 < 0) { t = argStr.toFloat(); }
    else { t = argStr.substring(0, s2).toFloat(); s = argStr.substring(s2+1).toFloat(); }
    body = String("{\"mode\":1,\"action\":\"start\",\"target\":") + t +
           String(",\"speed\":") + s + String("}");
  }
  else if (op == "v" || op == "vel") {
    float v = argStr.toFloat();
    body = String("{\"mode\":2,\"action\":\"start\",\"velocity\":") + v + String("}");
  }
  else if (op == "range") {
    body = String("{\"range\":") + argStr.toFloat() + String("}");
  }
  else if (op == "off" || op == "offset") {
    body = String("{\"offset\":") + argStr.toFloat() + String(",\"save\":true}");
  }
  else if (op == "detach") {
    // 测试用：停止 PWM 输出（motion.disable 会阻止 tick 自动重连）
    motion.disable();
    pinMode(SERVO_PIN, OUTPUT);
    digitalWrite(SERVO_PIN, LOW);
    Serial.println("舵机已 disable（PWM 真停了，舵盘应失去保持力矩，可被手动转动）");
    return;
  }
  else if (op == "raw") {
    // 诊断用：绕过所有库，直接用底层 API 彻底关闭 IO13 的 PWM
    // 1. motion 禁用
    motion.disable();
    // 2. servo detach
    myservo.detach();
    // 3. 直接调底层 ledcDetach（针对 Arduino-ESP32 3.x）
    ledcDetach(SERVO_PIN);
    // 4. 把 IO13 配成 INPUT（高阻），彻底切断任何输出
    pinMode(SERVO_PIN, INPUT);
    Serial.println("RAW: 已用底层 ledcDetach + pinMode(INPUT) 彻底切断 IO13");
    Serial.println("如果舵机还有保持力/电流>50mA，说明 PWM 不是来自 ESP32（可能是舵机电源/硬件问题）");
    return;
  }
  else if (op == "attach") {
    // 测试用：重新启动 PWM
    motion.enable();
    Serial.println("舵机已 enable（PWM 恢复，舵盘重新锁住）");
    return;
  }
  else if (op == "sleep") {
    // 测试用：完整跑一遍休眠前清理（不含真正休眠），看舵机反应 + 测电流
    Serial.println("执行 prepareServoForDeepSleep() ...");
    Serial.println("（舵机应失去保持力矩；如果装了 MOSFET，舵机会完全断电）");
    prepareServoForDeepSleep();
    Serial.println("完成。测电流请看万用表；输入 attach 恢复。");
    return;
  }
  else if (op == "wifioff") {
    // 诊断用：彻底关闭 WiFi，看 TEST1 电流会跌多少
    // 验证"100+mA 里有多少是 ESP32 WiFi 吃的"
    Serial.println("关闭 WiFi...");
    WiFi.disconnect(true, true);
    WiFi.mode(WIFI_OFF);
    esp_wifi_stop();
    Serial.println("WiFi 已关。现在测 TEST1 电流，对比刚才的读数。");
    Serial.println("（恢复需要重启：按 RST 键，或输入 scan 重连）");
    return;
  }
  else if (op == "deepsleep" || op == "sleep2") {
    // 诊断用：完整进入深度休眠，测真实最低功耗
    // 用法: deepsleep 10  (休眠 10 秒后自动唤醒重启)
    //       deepsleep     (默认休眠 30 秒)
    uint32_t secs = argStr.toInt();
    if (secs <= 0) secs = 30;
    enterDeepSleepSeconds(secs);
    return;   // 不会执行到这里
  }
  else if (op == "scan") {
    Serial.println("重试 WiFi...");
    WiFi.mode(WIFI_STA);
    if (hasWifiConfig()) connectSTA();
    else Serial.println("未配置 WiFi，请用 wifi <ssid> <pass> 或 AP 配网");
    return;
  }
  else {
    Serial.printf("未知指令: %s\n", cmd.c_str());
    Serial.println("可用: ? wifi<ssid> <pass> p<target>[speed] v<vel> range<n> off<n> stop scan");
    return;
  }

  if (parseControl(body, err)) {
    motion.applyConfigChange();
    Serial.printf("OK -> %s\n", body.c_str());
  } else {
    Serial.printf("错误: %s\n", err.c_str());
  }
}

void loop() {
  // AP 模式需要处理 DNS（captive portal）；STA 模式 dnsServer 没启动，调了也无害
  if (g_apMode) {
    dnsServer.processNextRequest();
  }

  // 串口命令
  while (Serial.available()) {
    char ch = Serial.read();
    if (ch == '\n' || ch == '\r') {
      if (serialInput.length() > 0) {
        handleSerialCmd(serialInput);
        serialInput = "";
      }
    } else {
      serialInput += ch;
    }
  }

  server.handleClient();
  motion.tick();
}
