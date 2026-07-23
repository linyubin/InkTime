/*
 * ink-display-wifi-epd.ino
 * ──────────────────────────────────────────────────────────────
 * InkTime ESP32 WiFi 墨水屏驱动
 *
 * 功能：
 *   1. 首次启动进入 AP 配网模式（SSID: InkTime-xxxx / 密码: 12345678）
 *   2. 配网后通过 WiFi 连接服务器，HTTP 下载当日照片 .bin 文件
 *   3. ESP32epdx 低层驱动 (EPD.h) 驱动 7.3寸四色墨水屏 (GDEY073D46 / EL073TS3) 渲染画面
 *   4. 渲染完成后进入 Deep Sleep，定时唤醒刷新
 *   5. 上电时按住 GPIO0 (BOOT 键) 可恢复出厂设置（清空 NVS + 重新配网）
 *
 * 硬件参数（对齐 push_to_epd_ble.py 中的墨水屏配置）：
 *   - 屏幕型号：GDEY073D46 (EL073TS3)，7.3 寸四色（黑/白/红/黄）
 *   - 硬件原生分辨率：800×480（横屏）
 *   - 画布逻辑分辨率：480×800（竖屏，与 render_daily_photo.py 输出一致）
 *   - 颜色映射：0=黑, 1=白, 2=红, 3=黄（1字节/像素）
 *   - SPI 通信
 *
 * 依赖库：
 *   - ESP32epdx 
 *   - ESP32 Arduino Core
 *
 * 开发板选择：
 *   - ESP32-L Module
 * ──────────────────────────────────────────────────────────────
 */

#include <WiFi.h>
#include <WebServer.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <SPI.h>
#include <time.h>
#include "esp_heap_caps.h"
#include "esp_system.h"
#include "esp_wifi.h"
#include "esp_bt.h"
#include "driver/gpio.h"
#include "driver/rtc_io.h"

#include <ESP32epdx.h>
#include "EPD.h"
#include "ap_bg.h"


// ============================================================
//  调试开关（生产环境改成 0 以节省功耗）
// ============================================================
#define DEBUG_LOG 1

#if DEBUG_LOG
  #define DBG_BEGIN()    Serial.begin(115200)
  #define DBG_PRINT(x)   Serial.print(x)
  #define DBG_PRINTLN(x) Serial.println(x)
  #define DBG_PRINTF(fmt, ...) Serial.printf(fmt, ##__VA_ARGS__)
#else
  #define DBG_BEGIN()
  #define DBG_PRINT(x)
  #define DBG_PRINTLN(x)
  #define DBG_PRINTF(fmt, ...)
#endif

#ifndef LED_BUILTIN
#define LED_BUILTIN 2
#endif

// ============================================================
//  墨水屏参数 & SPI 引脚
//  （对齐 push_to_epd_ble.py: _SRC_W=480, _SRC_H=800, 硬件 800×480）
// ============================================================

// 硬件原生分辨率（横屏）
// (EPD_WIDTH 和 EPD_HEIGHT 在 EPD.h 中定义)

// 画布逻辑分辨率（竖屏，与 render_daily_photo.py 输出一致）
static const int FB_WIDTH   = 480;
static const int FB_HEIGHT  = 800;

// SPI 引脚定义（与 ESP32epdx 库及 GDEM075F52 示例对应）
#define PIN_EPD_BUSY 13
#define PIN_EPD_RST  12
#define PIN_EPD_DC   14
#define PIN_EPD_CS   27
#define PIN_EPD_SCLK 18
#define PIN_EPD_DIN  23

// ============================================================
//  恢复出厂设置
//  上电时按住 GPIO0 (BOOT 键) → 清 NVS + 进入 AP 配网
//  (注：原理图中无独立按键，默认使用 ESP32 常见的 IO0/BOOT)
// ============================================================
#define PIN_FACTORY_RESET       0
#define FACTORY_RESET_ACTIVE_LOW 1
static const uint32_t FACTORY_RESET_SAMPLE_DELAY_MS = 5;


// ============================================================
//  AP 配网超时：5 分钟无操作 → 休眠到下一刷新点
// ============================================================
static const uint32_t AP_TIMEOUT_MS = 5UL * 60UL * 1000UL;

// ============================================================
//  每日照片下载配置
//  URL 路径前缀，需与 config.py 中的 DOWNLOAD_KEY 保持一致
// ============================================================
#define DAILY_PHOTO_PATH_PREFIX "/static/inktime/andyhome0203/photo_"
#define DAILY_PHOTO_COUNT       5  // photo_0.bin ~ photo_4.bin

// ============================================================
//  NVS 配置结构
// ============================================================
Preferences prefs;
WebServer   server(80);

struct Config {
  String  wifi_ssid;         // WiFi SSID
  String  wifi_pass;         // WiFi 密码
  String  backend_hostport;  // 服务器地址 (host:port)
  int32_t tz_offset_hours;   // 时区偏移（小时）
  uint8_t refresh_hour;      // 每日刷新时间（0-23）
  bool    rotate180;         // 画面旋转 180°
  bool    valid;             // 配置是否有效
};

static const char*   DEFAULT_HOSTPORT = "";
static const int32_t DEFAULT_TZ       = 8;     // UTC+8
static const uint8_t DEFAULT_HOUR     = 8;     // 每天 8 点刷新

Config   g_cfg;

// 前置声明：正常调用 idx 固定为 0；debug 传入 forcedIdx 与日志缓冲
bool downloadAndRenderDailyPhoto(const Config &cfg, int forcedIdx = -1, String* logBuf = nullptr);
// 日志辅助：同时写 Serial 和（若提供）logBuf；定义在 downloadAndRenderDailyPhoto 之前
static void pushLog(String* logBuf, const String& line);

// ── 手动调试模式（仅手动唤醒后生效；RAM 变量，每个唤醒周期独立）──
volatile bool g_debugMode         = false;   // /debug?state=on|off 切换
volatile bool g_debugOffRequested = false;   // 用户请求退出 debug → 进入 deep sleep
volatile bool g_requestHappened   = false;   // 任意请求触发，用于重置空闲计时
String        g_fetchLog;                     // /fetch 产出 + /log 下载的日志缓冲
static const uint32_t INITIAL_WINDOW_MS = 3UL  * 60UL * 1000UL;  // 手动唤醒后初始 web 在线窗口
static const uint32_t DEBUG_IDLE_MS    = 30UL * 60UL * 1000UL;  // debug 下无操作自动休眠

// ============================================================
//  GPIO 管理：启动时释放所有 Deep Sleep 期间的引脚保持
// ============================================================
static void releaseAllGpioHoldsAtBoot() {
  gpio_deep_sleep_hold_dis();
  for (int gpio = 0; gpio <= 48; ++gpio) {
    gpio_num_t gn = (gpio_num_t)gpio;
    if (!GPIO_IS_VALID_GPIO(gn)) continue;
    gpio_hold_dis(gn);
    if (rtc_gpio_is_valid_gpio(gn)) rtc_gpio_hold_dis(gn);
  }
}

// ============================================================
//  NVS 配置读写
// ============================================================
static void clearConfigNVS() {
  DBG_PRINTLN("[NVS] 清空配置");
  prefs.begin("dashcfg", false);
  prefs.clear();
  prefs.end();
}

void loadConfig(Config &cfg) {
  prefs.begin("dashcfg", true);
  cfg.wifi_ssid        = prefs.getString("ssid", "");
  cfg.wifi_pass        = prefs.getString("pass", "");
  cfg.backend_hostport = prefs.getString("hostport", DEFAULT_HOSTPORT);
  cfg.tz_offset_hours  = prefs.getInt("tz", DEFAULT_TZ);
  cfg.refresh_hour     = (uint8_t)prefs.getUChar("hour", DEFAULT_HOUR);
  cfg.rotate180        = prefs.getBool("rot180", false);
  prefs.end();

  cfg.valid = (cfg.wifi_ssid.length() > 0);

#if DEBUG_LOG
  DBG_PRINTLN("──── loadConfig ────");
  DBG_PRINTF("[CFG] ssid=%s\n",       cfg.wifi_ssid.c_str());
  DBG_PRINTF("[CFG] hostport=%s\n",   cfg.backend_hostport.c_str());
  DBG_PRINTF("[CFG] tz=%d, hour=%d\n", cfg.tz_offset_hours, (int)cfg.refresh_hour);
  DBG_PRINTF("[CFG] rotate180=%s\n",  cfg.rotate180 ? "true" : "false");
  DBG_PRINTF("[CFG] valid=%s\n",      cfg.valid ? "true" : "false");
#endif
}

void saveConfig(const Config &cfg) {
  prefs.begin("dashcfg", false);
  prefs.putString("ssid",     cfg.wifi_ssid);
  prefs.putString("pass",     cfg.wifi_pass);
  prefs.putString("hostport", cfg.backend_hostport);
  prefs.putInt("tz",          cfg.tz_offset_hours);
  prefs.putUChar("hour",      cfg.refresh_hour);
  prefs.putBool("rot180",     cfg.rotate180);
  prefs.end();
  DBG_PRINTLN("[CFG] 配置已保存");
}

// ============================================================
//  Deep Sleep 时间戳记录（用于离线计算下次唤醒）
// ============================================================
static void saveLastTimeEpoch(time_t epoch) {
  prefs.begin("dashcfg", false);
  prefs.putULong("last_epoch", (uint32_t)epoch);
  prefs.end();
  DBG_PRINTF("[TIME] 保存 last_epoch=%u\n", (uint32_t)epoch);
}

static bool loadLastTimeEpoch(time_t &epochOut) {
  prefs.begin("dashcfg", true);
  uint32_t v = prefs.getULong("last_epoch", 0);
  prefs.end();
  if (v == 0) return false;
  epochOut = (time_t)v;
  return true;
}

// 根据上次记录的时间，计算距离下一个刷新点的分钟数
static uint32_t minutesToNextRefreshFromLastEpoch(const Config &cfg) {
  time_t lastEpoch;
  if (!loadLastTimeEpoch(lastEpoch)) {
    return 1440;  // 无记录则默认 24 小时后
  }

  struct tm t;
  localtime_r(&lastEpoch, &t);

  int curMinOfDay = t.tm_hour * 60 + t.tm_min;
  int targetMin   = (int)cfg.refresh_hour * 60;
  int deltaMin;

  if (curMinOfDay < targetMin) deltaMin = targetMin - curMinOfDay;
  else                         deltaMin = 24 * 60 - (curMinOfDay - targetMin);

  if (deltaMin < 1)    deltaMin = 24 * 60;
  if (deltaMin > 1440) deltaMin = 1440;
  return (uint32_t)deltaMin;
}

// ============================================================
//  恢复出厂设置检测
// ============================================================
static bool isFactoryResetRequestedAtBoot() {
  pinMode(PIN_FACTORY_RESET, INPUT_PULLUP);
  delay(FACTORY_RESET_SAMPLE_DELAY_MS);
#if FACTORY_RESET_ACTIVE_LOW
  return (digitalRead(PIN_FACTORY_RESET) == LOW);
#else
  return (digitalRead(PIN_FACTORY_RESET) == HIGH);
#endif
}

// ============================================================
//  HTML 工具函数
// ============================================================
String htmlEscape(const String &s) {
  String out;
  out.reserve(s.length());
  for (size_t i = 0; i < s.length(); ++i) {
    char c = s[i];
    if      (c == '&')  out += F("&amp;");
    else if (c == '<')  out += F("&lt;");
    else if (c == '>')  out += F("&gt;");
    else if (c == '"')  out += F("&quot;");
    else                out += c;
  }
  return out;
}

// ============================================================
//  WiFi 硬件重置（进入 AP 配网前调用）
// ============================================================
static void wifiHardResetForPortal() {
  DBG_PRINTLN("[WIFI] 重置 WiFi 硬件");
  WiFi.scanDelete();
  WiFi.disconnect(true, true);
  WiFi.mode(WIFI_OFF);
  delay(200);

  WiFi.mode(WIFI_AP_STA);
  WiFi.setSleep(false);
  esp_wifi_set_ps(WIFI_PS_NONE);
  WiFi.scanDelete();
  delay(50);
}

// ============================================================
//  AP 配网页面构建
// ============================================================
String buildConfigPage() {
  WiFi.scanDelete();
  delay(30);

  int n = WiFi.scanNetworks(false, true);
  DBG_PRINTF("[CFG] 扫描到 %d 个 WiFi 网络\n", n);

  String curSsid = g_cfg.wifi_ssid;
  String host    = htmlEscape(g_cfg.backend_hostport);
  int32_t tz     = g_cfg.tz_offset_hours;
  if (tz < -12 || tz > 14) tz = DEFAULT_TZ;
  uint8_t hour   = g_cfg.refresh_hour;
  if (hour > 23) hour = DEFAULT_HOUR;
  bool rot180    = g_cfg.rotate180;

  String html;
  html.reserve(6144);

  // ── HTML 头部 & 样式 ──
  html += F("<!DOCTYPE html><html><head><meta charset='utf-8'>");
  html += F("<meta name='viewport' content='width=device-width,initial-scale=1'>");
  html += F("<title>InkTime WiFi 设置</title>");
  html += F("<style>");
  html += F("*{box-sizing:border-box;margin:0;padding:0}");
  html += F("body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;");
  html += F("background:#f0f2f5;color:#1a1a2e;padding:20px;min-height:100vh}");
  html += F(".card{max-width:420px;margin:0 auto;background:#fff;border-radius:16px;");
  html += F("box-shadow:0 4px 24px rgba(0,0,0,0.08);padding:32px;border:1px solid #e8eaed}");
  html += F("h2{text-align:center;color:#16213e;margin-bottom:8px;font-size:22px}");
  html += F(".subtitle{text-align:center;color:#888;font-size:13px;margin-bottom:24px}");
  html += F("label{display:block;font-weight:600;margin-bottom:6px;color:#333;font-size:14px}");
  html += F("input[type=text],input[type=password],select{width:100%;padding:10px 14px;");
  html += F("border:1.5px solid #ddd;border-radius:10px;font-size:15px;outline:none;");
  html += F("transition:border-color 0.2s;background:#fafbfc}");
  html += F("input:focus,select:focus{border-color:#4361ee}");
  html += F(".group{margin-bottom:18px}");
  html += F(".row{display:flex;gap:12px}");
  html += F(".row .group{flex:1}");
  html += F(".cb-group{display:flex;align-items:center;gap:8px;margin:18px 0}");
  html += F(".cb-group input{width:18px;height:18px;accent-color:#4361ee}");
  html += F(".cb-group label{margin:0;font-weight:400;font-size:14px}");
  html += F("button{width:100%;padding:12px;background:linear-gradient(135deg,#4361ee,#3a0ca3);");
  html += F("color:#fff;border:none;border-radius:10px;font-size:16px;font-weight:600;");
  html += F("cursor:pointer;transition:opacity 0.2s;margin-top:8px}");
  html += F("button:hover{opacity:0.9}");
  html += F(".warn{color:#c00;font-size:13px;margin-top:12px;text-align:center}");
  html += F("</style></head><body>");

  // ── 表单主体 ──
  html += F("<div class='card'>");
  html += F("<h2>🖼 InkTime</h2>");
  html += F("<p class='subtitle'>WiFi 墨水屏相框设置</p>");

  // 显示当前网络状态
  html += F("<div style='background:#f8f9fa;padding:12px;border-radius:8px;margin-bottom:20px;text-align:center;font-size:14px;'>");
  if (WiFi.status() == WL_CONNECTED) {
    html += F("<strong>网络状态:</strong> <span style='color:#2e7d32'>已连接</span><br>");
    html += F("<span style='color:#666'>IP: </span><strong>");
    html += WiFi.localIP().toString();
    html += F("</strong>");
  } else {
    html += F("<strong>网络状态:</strong> <span style='color:#c62828'>未连接 (AP 模式)</span>");
  }
  html += F("</div>");

  html += F("<form method='POST' action='/save'>");


  // WiFi SSID 选择 + 输入
  html += F("<div class='group'><label>WiFi 网络</label>");
  html += F("<select id='ssid_select' onchange=\"document.getElementById('ssid_input').value=this.value;\">");
  html += F("<option value=''>（手动输入或选择）</option>");
  if (n > 0) {
    for (int i = 0; i < n; ++i) {
      String s = WiFi.SSID(i);
      if (s.length() == 0) continue;
      String esc = htmlEscape(s);
      html += F("<option value='");
      html += esc;
      html += F("'");
      if (s == curSsid) html += F(" selected");
      html += F(">");
      html += esc;
      html += F("</option>");
    }
  }
  html += F("</select></div>");

  html += F("<div class='group'><label>SSID</label>");
  html += F("<input id='ssid_input' name='ssid' type='text' value='");
  html += htmlEscape(curSsid);
  html += F("' placeholder='WiFi 名称'></div>");

  // WiFi 密码
  html += F("<div class='group'><label>密码</label>");
  html += F("<input name='pass' type='password' placeholder='WiFi 密码'></div>");

  // 服务器地址
  html += F("<div class='group'><label>服务器地址 (host:port)</label>");
  html += F("<input name='hostport' type='text' value='");
  html += host;
  html += F("' placeholder='192.168.1.100:8765'></div>");

  // 刷新时间 + 时区（并排）
  html += F("<div class='row'>");

  // 每日刷新时间
  html += F("<div class='group'><label>刷新时间</label><select name='hour'>");
  for (int h = 0; h < 24; ++h) {
    html += "<option value='";
    html += String(h);
    html += "'";
    if (h == hour) html += " selected";
    html += ">";
    html += String(h);
    html += F(" 点</option>");
  }
  html += F("</select></div>");

  // 时区
  html += F("<div class='group'><label>时区</label><select name='tz'>");
  for (int t = -12; t <= 14; ++t) {
    html += "<option value='";
    html += String(t);
    html += "'";
    if (t == tz) html += " selected";
    html += ">UTC";
    if (t >= 0) html += "+";
    html += String(t);
    html += F("</option>");
  }
  html += F("</select></div>");

  html += F("</div>"); // .row

  // 旋转 180°
  html += F("<div class='cb-group'>");
  html += F("<input type='checkbox' id='rot180' name='rot180' value='1'");
  if (rot180) html += F(" checked");
  html += F("><label for='rot180'>画面旋转 180°</label></div>");

  // 未扫描到 WiFi 的提示
  if (n <= 0) {
    html += F("<p class='warn'>未扫描到 WiFi 网络，可在上方手动输入 SSID。</p>");
  }

  // 提交按钮
  html += F("<button type='submit'>保存并重启</button>");
  html += F("</form></div></body></html>");

  return html;
}

// ============================================================
//  WebServer 路由处理
// ============================================================
void handleRoot() {
  DBG_PRINTLN("[HTTP] GET /");
  server.send(200, "text/html; charset=utf-8", buildConfigPage());
}

void handleSave() {
  DBG_PRINTLN("[HTTP] POST /save");

  String ssid    = server.arg("ssid");
  String pass    = server.arg("pass");
  String host    = server.arg("hostport");
  String hourStr = server.arg("hour");
  String tzStr   = server.arg("tz");
  bool rot180Req = (server.arg("rot180") == "1");

  ssid.trim();
  host.trim();

  Config newCfg = g_cfg;

  if (ssid.length() > 0) newCfg.wifi_ssid = ssid;
  if (pass.length() > 0) newCfg.wifi_pass = pass;
  newCfg.backend_hostport = host;

  int32_t tz = tzStr.toInt();
  if (tz < -12) tz = -12;
  if (tz > 14)  tz = 14;
  newCfg.tz_offset_hours = tz;

  int hour = hourStr.toInt();
  if (hour < 0)  hour = 0;
  if (hour > 23) hour = 23;
  newCfg.refresh_hour = (uint8_t)hour;

  newCfg.rotate180 = rot180Req;
  newCfg.valid     = (newCfg.wifi_ssid.length() > 0);

  saveConfig(newCfg);

  String resp;
  resp += F("<!DOCTYPE html><html><head><meta charset='utf-8'>");
  resp += F("<style>body{font-family:sans-serif;display:flex;align-items:center;");
  resp += F("justify-content:center;height:100vh;background:#f0f2f5}");
  resp += F(".msg{text-align:center;padding:40px;background:#fff;border-radius:16px;");
  resp += F("box-shadow:0 4px 24px rgba(0,0,0,0.08)}</style></head><body>");
  resp += F("<div class='msg'><h3>✅ 保存成功</h3><p>设备即将重启...</p></div>");
  resp += F("</body></html>");

  server.send(200, "text/html; charset=utf-8", resp);
  delay(800);
  ESP.restart();
}

// ============================================================
//  手动调试路由：/fetch  /log  /debug
// ============================================================

// /fetch?idx=N —— 手动拉取指定照片并渲染，把本次完整日志返回给浏览器
void handleFetch() {
  g_requestHappened = true;
  DBG_PRINTLN("[HTTP] GET /fetch");

  g_fetchLog = "";
  pushLog(&g_fetchLog, String("[Fetch] 开始 ms=") + (int)millis());

  int idx = server.hasArg("idx") ? server.arg("idx").toInt() : -1;
  if (idx < 0 || idx >= DAILY_PHOTO_COUNT) {
    pushLog(&g_fetchLog, String("[Fetch] idx 非法: ") + idx + "，回退为 0");
    idx = 0;
  }

  bool ok = downloadAndRenderDailyPhoto(g_cfg, idx, &g_fetchLog);
  pushLog(&g_fetchLog, ok ? "[Fetch] 结果: 成功 ✅" : "[Fetch] 结果: 失败 ❌");

  String html;
  html.reserve(g_fetchLog.length() + 700);
  html += F("<!DOCTYPE html><html><head><meta charset='utf-8'>");
  html += F("<meta name='viewport' content='width=device-width,initial-scale=1'>");
  html += F("<title>InkTime Debug - Fetch</title><style>");
  html += F("body{font-family:monospace;background:#0b0c10;color:#cfd8e3;padding:16px;margin:0}");
  html += F("h3{color:#9cffd6}.bar{margin:12px 0}");
  html += F("pre{background:#11161f;padding:12px;border-radius:8px;white-space:pre-wrap;");
  html += F("word-break:break-all;border:1px solid #1f2a37;overflow:auto}");
  html += F(".links a{display:inline-block;margin-right:14px;color:#8ab4ff}");
  html += F("</style></head><body>");
  html += F("<h3>📸 手动拉取渲染</h3>");
  html += F("<div class='bar'>结果：<strong>");
  html += (ok ? F("✅ 成功") : F("❌ 失败"));
  html += F("</strong> | idx=");
  html += String(idx);
  html += F("</div>");
  html += F("<div class='links'>");
  html += F("<a href='/'>[配置页]</a>");
  html += F("<a href='/log'>[下载日志]</a>");
  html += F("<a href='/debug?state=off'>[退出 debug]</a>");
  html += F("</div>");
  html += F("<pre>");
  html += htmlEscape(g_fetchLog);
  html += F("</pre></body></html>");

  server.send(200, "text/html; charset=utf-8", html);
}

// /log —— 下载累计的拉取日志
void handleLog() {
  g_requestHappened = true;
  DBG_PRINTLN("[HTTP] GET /log");
  server.sendHeader("Content-Disposition", "attachment; filename=inktime_fetch.log");
  server.send(200, "text/plain; charset=utf-8", g_fetchLog.length() ? g_fetchLog : String(F("(暂无日志)")));
}

// /debug?state=on|off|confirm_off —— 切换 debug 模式（两步关闭，关前提示下载日志）
void handleDebug() {
  g_requestHappened = true;
  String st = server.hasArg("state") ? server.arg("state") : "";
  st.toLowerCase();
  DBG_PRINTF("[HTTP] GET /debug state=%s\n", st.c_str());

  String html;
  html += F("<!DOCTYPE html><html><head><meta charset='utf-8'>");
  html += F("<meta name='viewport' content='width=device-width,initial-scale=1'>");
  html += F("<title>InkTime Debug</title><style>");
  html += F("body{font-family:sans-serif;background:#f0f2f5;padding:24px;text-align:center;color:#1a1a2e}");
  html += F("a{display:inline-block;margin:8px;padding:10px 16px;background:#4361ee;color:#fff;");
  html += F("text-decoration:none;border-radius:8px}");
  html += F("</style></head><body>");

  if (st == "on") {
    g_debugMode = true;
    g_debugOffRequested = false;
    html += F("<h3>🔧 Debug 模式已开启</h3>");
    html += F("<p>设备保持唤醒，直到你手动关闭或 30 分钟无操作。</p>");
    html += F("<p><a href='/fetch?idx=0'>手动拉取 idx=0</a>");
    html += F("<a href='/log'>下载日志</a></p>");
  } else if (st == "off") {
    html += F("<h3>准备退出 Debug 模式</h3>");
    html += F("<p>设备即将进入 Deep Sleep。请先下载本次日志：</p>");
    html += F("<p><a href='/log'>⬇️ 下载日志 (inktime_fetch.log)</a></p>");
    html += F("<p><a href='/debug?state=confirm_off'>确认退出并休眠 →</a></p>");
  } else if (st == "confirm_off") {
    html += F("<h3>已退出 Debug，进入 Deep Sleep...</h3>");
    g_debugOffRequested = true;
  } else {
    html += F("<h3>🔧 InkTime Debug</h3>");
    html += F("<p>当前 debug 模式：<strong>");
    html += (g_debugMode ? F("开启") : F("关闭"));
    html += F("</strong></p>");
    html += F("<p><a href='/debug?state=on'>开启 debug</a>");
    html += F("<a href='/debug?state=off'>关闭 debug</a></p>");
  }
  html += F("</body></html>");
  server.send(200, "text/html; charset=utf-8", html);
}

// ============================================================
//  Deep Sleep 电源管理
// ============================================================

// 关闭 RTC 各域以降低深度休眠功耗
void prepareDeepSleepDomains() {
  esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_PERIPH,   ESP_PD_OPTION_OFF);
  esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_SLOW_MEM, ESP_PD_OPTION_OFF);
  esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_FAST_MEM, ESP_PD_OPTION_OFF);
}

// 将墨水屏 SPI 引脚全部设为下拉输入，避免漏电
static void powerDownEPD() {
  const int epdPins[] = {
    PIN_EPD_BUSY, PIN_EPD_RST, PIN_EPD_DC,
    PIN_EPD_CS, PIN_EPD_SCLK, PIN_EPD_DIN
  };
  for (size_t i = 0; i < sizeof(epdPins) / sizeof(epdPins[0]); ++i) {
    pinMode(epdPins[i], INPUT);
    pinMode(epdPins[i], INPUT_PULLDOWN);
  }
}

// Deep Sleep 期间保持 EPD 引脚状态（防止意外唤醒屏幕）
static void deepSleepHoldOnlyEpdPins() {
  const int epdPins[] = {
    PIN_EPD_BUSY, PIN_EPD_RST, PIN_EPD_DC,
    PIN_EPD_CS, PIN_EPD_SCLK, PIN_EPD_DIN
  };
  for (size_t i = 0; i < sizeof(epdPins) / sizeof(epdPins[0]); ++i) {
    gpio_num_t gn = (gpio_num_t)epdPins[i];
    if (!GPIO_IS_VALID_GPIO(gn)) continue;

    gpio_set_direction(gn, GPIO_MODE_INPUT);
    gpio_pulldown_en(gn);
    gpio_pullup_dis(gn);
    gpio_hold_en(gn);

    if (rtc_gpio_is_valid_gpio(gn)) rtc_gpio_isolate(gn);
  }
  gpio_deep_sleep_hold_en();
}

// ============================================================
//  Deep Sleep 入口
// ============================================================
void goDeepSleepMinutes(uint32_t minutes) {
  if (minutes < 1)    minutes = 1;
  if (minutes > 1440) minutes = 1440;

  DBG_PRINTF("[SLEEP] 即将休眠 %d 分钟\n", (int)minutes);

  uint64_t us = (uint64_t)minutes * 60ULL * 1000000ULL;

  // 关闭外设
  powerDownEPD();

  WiFi.disconnect(true, true);
  WiFi.mode(WIFI_OFF);
  esp_wifi_stop();

#if defined(CONFIG_BT_ENABLED)
  esp_bt_controller_disable();
#endif

  deepSleepHoldOnlyEpdPins();
  prepareDeepSleepDomains();
  esp_sleep_enable_timer_wakeup(us);

  DBG_PRINTLN("[SLEEP] 进入深度休眠...");
  delay(50);
  esp_deep_sleep_start();
}

// ============================================================
//  AP 配网模式
// ============================================================
void startConfigPortal() {
  DBG_PRINTLN("[CFG] 进入 AP 配网模式");

  wifiHardResetForPortal();

  // AP 名称：InkTime-<MAC后4位>
  String apSsid     = "InkTime-" + String((uint32_t)ESP.getEfuseMac(), HEX).substring(4);
  const char* apPwd = "12345678";

  bool apOk = WiFi.softAP(apSsid.c_str(), apPwd);

  DBG_PRINTF("[CFG] AP 启动 %s\n", apOk ? "成功" : "失败");
  DBG_PRINTF("[CFG] SSID: %s, IP: %s\n", apSsid.c_str(), WiFi.softAPIP().toString().c_str());

  server.on("/",     HTTP_GET,  handleRoot);
  server.on("/save", HTTP_POST, handleSave);
  server.begin();

  uint32_t enterMs = millis();

  for (;;) {
    server.handleClient();

    // 超时：休眠到下一刷新点
    if (millis() - enterMs > AP_TIMEOUT_MS) {
      DBG_PRINTLN("[AP] 配网超时，进入休眠");
      uint32_t mins = minutesToNextRefreshFromLastEpoch(g_cfg);
      DBG_PRINTF("[AP] 休眠 %d 分钟到下次刷新\n", (int)mins);
      delay(50);
      goDeepSleepMinutes(mins);
    }

    delay(10);
  }
}

// ============================================================
//  WiFi STA 连接
// ============================================================
bool connectWiFi(const Config &cfg, uint32_t timeout_ms = 15000) {
  DBG_PRINTF("[WIFI] 连接到 %s ...\n", cfg.wifi_ssid.c_str());

  if (cfg.wifi_ssid.isEmpty()) return false;

  WiFi.disconnect(true, true);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(true);
  WiFi.setTxPower(WIFI_POWER_8_5dBm);  // 降低发射功率以省电
  WiFi.begin(cfg.wifi_ssid.c_str(), cfg.wifi_pass.c_str());

  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < timeout_ms) {
    delay(200);
    DBG_PRINT(".");
  }
  DBG_PRINTLN();

  bool ok = (WiFi.status() == WL_CONNECTED);

  if (ok) {
    DBG_PRINTF("[WIFI] 已连接, IP=%s\n", WiFi.localIP().toString().c_str());
  } else {
    DBG_PRINTLN("[WIFI] 连接失败");
  }

  return ok;
}

// ============================================================
//  NTP 时间同步
// ============================================================
bool syncTime(const Config &cfg, struct tm &outLocal) {
  DBG_PRINTLN("[TIME] NTP 同步...");
  long offsetSec = (long)cfg.tz_offset_hours * 3600;
  configTime(offsetSec, 0, "pool.ntp.org", "time.nist.gov", "ntp.aliyun.com");

  for (int i = 0; i < 30; ++i) {
    if (getLocalTime(&outLocal)) {
      char buf[64];
      strftime(buf, sizeof(buf), "%Y-%m-%d %H:%M:%S", &outLocal);
      DBG_PRINTF("[TIME] 同步成功: %s\n", buf);

      time_t nowEpoch = time(nullptr);
      if (nowEpoch > 0) saveLastTimeEpoch(nowEpoch);
      return true;
    }
    delay(500);
  }

  DBG_PRINTLN("[TIME] NTP 同步失败");
  return false;
}

// ============================================================
//  HTTP 下载每日照片 .bin 文件到 framebuffer
//
//  .bin 格式（与 render_daily_photo.py 输出一致）：
//    - 尺寸：480 × 800 = 384,000 字节
//    - 编码：1 字节/像素
//    - 颜色值：0=黑, 1=白, 2=红, 3=黄
//    - 行序：竖屏逐行扫描（y=0..799, x=0..479）
// ============================================================
//  下载每日照片并渲染到 GDEM075F52
// ============================================================
// 把一行日志同时写到 Serial 和（若提供）logBuf，供 /fetch 返回给浏览器
static void pushLog(String* logBuf, const String& line) {
  DBG_PRINTLN(line);
  if (logBuf) { *logBuf += line; *logBuf += '\n'; }
}

bool downloadAndRenderDailyPhoto(const Config &cfg, int forcedIdx, String* logBuf) {
  size_t epd_array_size = (size_t)EPD_WIDTH * EPD_HEIGHT / 4;  // 96,000 bytes
  
  // 分配 Canvas
  unsigned char* BlackImage = (unsigned char*)heap_caps_malloc(epd_array_size, MALLOC_CAP_8BIT | MALLOC_CAP_SPIRAM);
  if (!BlackImage) {
    BlackImage = (unsigned char*)heap_caps_malloc(epd_array_size, MALLOC_CAP_8BIT);
  }
  if (!BlackImage) {
    DBG_PRINTLN("[EPD] 无法为 BlackImage 分配内存！");
    return false;
  }

  // 初始化 Paint 属性
  uint16_t paintRotate = cfg.rotate180 ? ROTATE_270 : ROTATE_90;
  Paint_NewImage(BlackImage, EPD_WIDTH, EPD_HEIGHT, paintRotate, WHITE0);
  Paint_SetScale(4);
  Paint_SelectImage(BlackImage);
  Paint_Clear(WHITE0);

  if (cfg.backend_hostport.length() == 0) {
    DBG_PRINTLN("[HTTP] 服务器地址为空，跳过下载");
    pushLog(logBuf, "[拉取] 服务器地址为空，跳过下载");
    heap_caps_free(BlackImage);
    return false;
  }

  // 选择照片：正常固定 idx=0（评分最高）；debug 时用传入的 forcedIdx
  int idx = (forcedIdx >= 0 && forcedIdx < DAILY_PHOTO_COUNT) ? forcedIdx : 0;

  // 构建下载 URL
  String url;
  String hp = cfg.backend_hostport;
  hp.trim();

  if (hp.startsWith("http://") || hp.startsWith("https://")) {
    url = hp + String(DAILY_PHOTO_PATH_PREFIX) + String(idx) + ".bin";
  } else {
    url = "http://" + hp + String(DAILY_PHOTO_PATH_PREFIX) + String(idx) + ".bin";
  }

  DBG_PRINTF("[HTTP] GET %s\n", url.c_str());
  pushLog(logBuf, String("[拉取] idx=") + idx + " | GET " + url);

  HTTPClient http;
  http.begin(url);
  int code = http.GET();
  if (code != HTTP_CODE_OK) {
    DBG_PRINTF("[HTTP] 返回码: %d\n", code);
    pushLog(logBuf, String("[拉取] HTTP 失败 code=") + code + "（非 200）");
    http.end();
    heap_caps_free(BlackImage);
    return false;
  }

  int len = http.getSize();
  DBG_PRINTF("[HTTP] Content-Length: %d\n", len);
  pushLog(logBuf, String("[拉取] Content-Length: ") + len + " bytes");

  WiFiClient *stream = http.getStreamPtr();
  size_t totalBytesReceived = 0;
  size_t targetBytes = (size_t)FB_WIDTH * FB_HEIGHT; // 384,000 bytes

  const uint32_t DOWNLOAD_TIMEOUT_MS = 60 * 1000;
  uint32_t start_ms = millis();

  // 缓冲区
  const size_t bufSize = 512;
  uint8_t buf[bufSize];

  int currentX = 0;
  int currentY = 0;

  while (http.connected() && (len > 0 || len == -1) && totalBytesReceived < targetBytes) {
    if (millis() - start_ms > DOWNLOAD_TIMEOUT_MS) {
      DBG_PRINTLN("[HTTP] 下载超时");
      http.end();
      heap_caps_free(BlackImage);
      return false;
    }

    size_t avail = stream->available();
    if (avail) {
      size_t toRead = avail;
      if (toRead > bufSize) toRead = bufSize;
      if (toRead > targetBytes - totalBytesReceived) toRead = targetBytes - totalBytesReceived;

      int r = stream->read(buf, toRead);
      if (r > 0) {
        for (int i = 0; i < r; ++i) {
          uint8_t c = buf[i];
          uint8_t colorVal;
          switch (c) {
            case 0:  colorVal = BLACK0;  break;  // 黑
            case 1:  colorVal = WHITE0;  break;  // 白
            case 2:  colorVal = RED0;    break;  // 红
            case 3:  colorVal = YELLOW0; break;  // 黄
            default: colorVal = WHITE0;  break;
          }
          Paint_SetPixel(currentX, currentY, colorVal);

          currentX++;
          if (currentX >= FB_WIDTH) {
            currentX = 0;
            currentY++;
          }
        }
        totalBytesReceived += r;
        if (len > 0) len -= r;
      }
    } else {
      delay(1);
    }
  }

  http.end();

  DBG_PRINTF("[HTTP] 下载完成: %d / %d bytes\n", (int)totalBytesReceived, (int)targetBytes);
  pushLog(logBuf, String("[拉取] 下载完成: ") + (int)totalBytesReceived + " / " + (int)targetBytes
                       + " bytes，耗时 " + (int)(millis() - start_ms) + "ms");

  if (totalBytesReceived != targetBytes) {
    DBG_PRINTF("[HTTP] 尺寸不匹配！期望 %d，实际 %d\n", (int)targetBytes, (int)totalBytesReceived);
    pushLog(logBuf, "[拉取] 尺寸不匹配，渲染中止");
    heap_caps_free(BlackImage);
    return false;
  }

  // 刷屏显示
  DBG_PRINTLN("[EPD] 初始化并开始显示画面");
  EPD_init();
  PIC_display(BlackImage);
  EPD_DeepSleep();

  heap_caps_free(BlackImage);
  DBG_PRINTLN("[EPD] 渲染完成，屏幕已休眠");
  pushLog(logBuf, "[拉取] EPD 渲染完成 ✅");
  return true;
}

// ============================================================
//  计算下次唤醒并进入 Deep Sleep
// ============================================================
void sleepUntilNextSchedule(const Config &cfg, bool hasTime, const struct tm &now) {
  if (!hasTime) {
    DBG_PRINTLN("[SLEEP] 无有效时间，默认休眠 24 小时");
    goDeepSleepMinutes(1440);
    return;
  }

  int curMinOfDay = now.tm_hour * 60 + now.tm_min;
  int targetMin   = (int)cfg.refresh_hour * 60;
  int delta;

  if (curMinOfDay < targetMin) delta = targetMin - curMinOfDay;
  else                         delta = 24 * 60 - (curMinOfDay - targetMin);

  if (delta < 1) delta = 24 * 60;

  DBG_PRINTF("[SLEEP] 当前 %02d:%02d, 目标 %02d:00, 休眠 %d 分钟\n",
             now.tm_hour, now.tm_min, cfg.refresh_hour, delta);

  goDeepSleepMinutes((uint32_t)delta);
}

// ============================================================
//  显示配网/网络状态信息页面
// ============================================================
void showNetworkInfoScreen(bool isAP, bool isSuccess = false) {
  size_t epd_array_size = (size_t)EPD_WIDTH * EPD_HEIGHT / 4;
  unsigned char* BlackImage = (unsigned char*)heap_caps_malloc(epd_array_size, MALLOC_CAP_8BIT | MALLOC_CAP_SPIRAM);
  if (!BlackImage) {
    BlackImage = (unsigned char*)heap_caps_malloc(epd_array_size, MALLOC_CAP_8BIT);
  }
  if (!BlackImage) {
    DBG_PRINTLN("[EPD] 无法为网络信息页面分配内存");
    return;
  }

  uint16_t paintRotate = g_cfg.rotate180 ? ROTATE_270 : ROTATE_90;
  Paint_NewImage(BlackImage, EPD_WIDTH, EPD_HEIGHT, paintRotate, WHITE0);
  Paint_SetScale(4);
  Paint_SelectImage(BlackImage);
  Paint_Clear(WHITE0);

  // 1. 绘制附件图片作为背景
  for (int y = 0; y < FB_HEIGHT; ++y) {
    for (int x = 0; x < FB_WIDTH; ++x) {
      int byte_idx = (x / 4) + y * (FB_WIDTH / 4);
      int bit_shift = 6 - (x % 4) * 2;
      uint8_t colorVal = (ap_bg_data[byte_idx] >> bit_shift) & 0x03;
      Paint_SetPixel(x, y, colorVal);
    }
  }

  // 2. 绘制文字 (白色区域假设在中间偏下位置)
  int textY = 600; 
  int textX = 30;

  if (isAP) {
    String apSsid = "InkTime-" + String((uint32_t)ESP.getEfuseMac(), HEX).substring(4);
    Paint_DrawString_EN(textX, textY, "AP Mode - WiFi Config", &Font24, BLACK0, WHITE0);
    textY += 40;
    Paint_DrawString_EN(textX, textY, "Please connect to:", &Font20, BLACK0, WHITE0);
    textY += 30;
    Paint_DrawString_EN(textX, textY, ("SSID: " + apSsid).c_str(), &Font24, RED0, WHITE0);
    textY += 30;
    Paint_DrawString_EN(textX, textY, "Pass: 12345678", &Font24, BLACK0, WHITE0);
    textY += 40;
    Paint_DrawString_EN(textX, textY, "Then visit:", &Font20, BLACK0, WHITE0);
    textY += 30;
    Paint_DrawString_EN(textX, textY, "http://192.168.4.1", &Font24, RED0, WHITE0);
  } else {
    if (isSuccess) {
      Paint_DrawString_EN(textX, textY, "Andy Home Inktime v2.0", &Font24, WHITE0,RED0);
      textY += 40;
      Paint_DrawString_EN(textX, textY, "WiFi Configured!", &Font24, RED0, WHITE0);
      textY += 30;
      Paint_DrawString_EN(textX, textY, ("SSID: " + g_cfg.wifi_ssid).c_str(), &Font16, BLACK0, WHITE0);
      textY += 30;
      Paint_DrawString_EN(textX, textY, ("IP: " + WiFi.localIP().toString()).c_str(), &Font16, BLACK0, WHITE0);
      textY += 30;
      Paint_DrawString_EN(textX, textY, ("MAC: " + WiFi.macAddress()).c_str(), &Font16, BLACK0, WHITE0);
      textY += 30;

      Paint_DrawString_EN(textX, textY, "Fetching new photo...", &Font16, BLACK0, WHITE0);
    }
  }

  DBG_PRINTLN("[EPD] 刷新网络信息页面...");
  DBG_PRINTLN("[EPD] -> EPD_init()");
  EPD_init();
  DBG_PRINTLN("[EPD] -> PIC_display()");
  PIC_display(BlackImage);
  DBG_PRINTLN("[EPD] -> EPD_DeepSleep()");
  EPD_DeepSleep();
  heap_caps_free(BlackImage);
}

// ============================================================
//  setup()
// ============================================================
void setup() {
  // 1. 释放 Deep Sleep 引脚保持
  releaseAllGpioHoldsAtBoot();

  // 2. 降频省电 (注释掉，防止某些开发板在 80MHz 下 WiFi 不稳定)
  setCpuFrequencyMhz(80);
  pinMode(LED_BUILTIN, OUTPUT);
  digitalWrite(LED_BUILTIN, LOW);

  // 3. 串口初始化
  DBG_BEGIN();
  delay(200);

  DBG_PRINTLN();
  DBG_PRINTLN("===== InkTime WiFi EPD 启动 =====");

  // 4. 检查是否请求恢复出厂设置
  if (isFactoryResetRequestedAtBoot()) {
    DBG_PRINTLN("[BOOT] 触发 → 恢复出厂设置");
    clearConfigNVS();

    WiFi.disconnect(true, true);
    WiFi.mode(WIFI_OFF);
    esp_wifi_stop();
    delay(200);
  }

  // 5. 初始化随机数种子
  randomSeed(esp_random());

  // 6. 加载 NVS 配置
  loadConfig(g_cfg);

  // 6.5 初始化墨水屏 SPI 引脚 (必须在 showNetworkInfoScreen 之前调用)
  pinMode(PIN_EPD_BUSY, INPUT);  // BUSY
  pinMode(PIN_EPD_RST, OUTPUT);  // RES
  pinMode(PIN_EPD_DC, OUTPUT);   // DC
  pinMode(PIN_EPD_CS, OUTPUT);   // CS
  SPI.beginTransaction(SPISettings(10000000, MSBFIRST, SPI_MODE0));
  // 注意：SPI.begin() 不传 ss 参数！PIN_EPD_CS(27) 由 EPD 驱动手动控制
  // （EPD_W21_CS_0/1 宏）。若把 27 作为 ss 传给 SPI.begin，硬件会抢着管理
  // 片选，与驱动冲突，导致 EPD_init 卡死在 BUSY 等待。
  SPI.begin();

  // 7. 无有效配置 → 进入 AP 配网
  if (!g_cfg.valid) {
    DBG_PRINTLN("[BOOT] 无有效配置 → AP 配网模式");
    showNetworkInfoScreen(true, false);
    startConfigPortal();
    // startConfigPortal() 不返回
  }

  // 8. 连接 WiFi
  DBG_PRINTLN("[BOOT] 尝试连接 WiFi...");
  if (!connectWiFi(g_cfg)) {
    DBG_PRINTLN("[BOOT] WiFi 连接失败 → AP 配网模式");
    showNetworkInfoScreen(true, false);
    startConfigPortal();
  }

  // 8.5 检查唤醒原因，如果是手动短按复位键（非定时唤醒），则展示当前配网和IP信息页面
  esp_sleep_wakeup_cause_t wakeup_reason = esp_sleep_get_wakeup_cause();
  if (wakeup_reason != ESP_SLEEP_WAKEUP_TIMER) {
    DBG_PRINTLN("[BOOT] 手动唤醒，显示当前配网成功及 IP 信息...");
    showNetworkInfoScreen(false, true);
    delay(3000); // 稍作停留，接着下载照片将覆盖此画面
  }

  // 9. NTP 时间同步
  struct tm timeinfo;
  bool hasTime = syncTime(g_cfg, timeinfo);

  // 10. 下载并渲染今日照片
  bool ok = downloadAndRenderDailyPhoto(g_cfg);
  if (!ok) {
    DBG_PRINTLN("[BOOT] 照片下载或渲染失败");
  }

  // 10.5 手动唤醒：启动局域网 Web 服务器
  //      - 未开 debug：保持 INITIAL_WINDOW_MS（3 分钟）供改配置，到期即睡
  //      - 开了 debug：保持唤醒，直到用户 /debug?state=confirm_off 或 DEBUG_IDLE_MS(30min) 无操作
  if (wakeup_reason != ESP_SLEEP_WAKEUP_TIMER) {
    DBG_PRINTLN("[HTTP] 启动局域网 Web 服务器（手动唤醒）...");
    server.on("/",      HTTP_GET,  handleRoot);
    server.on("/save",  HTTP_POST, handleSave);
    server.on("/fetch", HTTP_GET,  handleFetch);
    server.on("/log",   HTTP_GET,  handleLog);
    server.on("/debug", HTTP_GET,  handleDebug);
    server.begin();

    uint32_t bootMs = millis();
    uint32_t lastAct = bootMs;
    for (;;) {
      server.handleClient();
      if (g_requestHappened) { g_requestHappened = false; lastAct = millis(); }
      if (g_debugOffRequested) break;                                       // 用户确认退出 debug
      if (!g_debugMode && (millis() - bootMs > INITIAL_WINDOW_MS)) break;   // 初始窗口到期且未开 debug
      if (g_debugMode && (millis() - lastAct > DEBUG_IDLE_MS)) break;       // debug 下 30 分钟无操作
      delay(10);
    }
    DBG_PRINTLN("[HTTP] Web 服务器结束，准备进入休眠");
  }

  // 11. 计算下次唤醒时间，进入 Deep Sleep
  if (!hasTime) {
    // 重试一次 NTP 同步
    struct tm tmp;
    if (syncTime(g_cfg, tmp)) {
      sleepUntilNextSchedule(g_cfg, true, tmp);
    } else {
      sleepUntilNextSchedule(g_cfg, false, timeinfo);
    }
  } else {
    sleepUntilNextSchedule(g_cfg, true, timeinfo);
  }
}

// ============================================================
//  loop() - Deep Sleep 唤醒后直接进 setup()，loop 不执行
// ============================================================
void loop() {
  // 不会执行到这里
}
