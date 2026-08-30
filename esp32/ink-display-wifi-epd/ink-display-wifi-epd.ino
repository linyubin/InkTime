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
#include "servo_rotate.h"
#include "device_log.h"


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
//  支持三种文件：.bin（画面）/ .json（朝向 sidecar）/ calib_{p,l}.bin（标定卡）
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
  // ── 舵机标定参数（相框旋转功能）──
  float   servo_portrait_deg;   // 竖屏目标角度（°，标定写入）
  float   servo_landscape_deg;  // 横屏目标角度（°，标定写入）
  float   servo_speed;          // 转动速度（°/s，默认 40，标定可调）
  bool    servo_calibrated;     // 是否已标定（false 时禁用舵机、不挡渲染）
  bool    landscape_invert;     // 横屏画面方向反转（现场标定）
  String  last_orientation;     // 上次朝向 "portrait"/"landscape"，跨深睡存活；空串触发首次归位
};

static const char*   DEFAULT_HOSTPORT = "";
static const int32_t DEFAULT_TZ       = 8;     // UTC+8
static const uint8_t DEFAULT_HOUR     = 8;     // 每天 8 点刷新
// 舵机默认值（servo_calibrated=false 时这些值不会被使用）
static const float   DEFAULT_SERVO_SPEED = 40.0f;   // °/s，偏慢保扭矩

Config   g_cfg;

// 前置声明
// 主流程：fetch json → fetch bin 到 framebuffer → 转舵机 → 刷屏
//   forcedIdx<0 时取 idx=0（评分最高）；debug /fetch?idx=N 传具体 idx
//   forcedCalibBin 非空时跳过 bin 下载，直接把标定卡字节流渲染到 framebuffer（标定台用）
bool downloadAndRenderDailyPhoto(const Config &cfg, int forcedIdx = -1, String* logBuf = nullptr,
                                 const char* forcedCalibBin = nullptr, const char* forcedCalibOri = nullptr);
// 日志辅助：同时写 Serial 和（若提供）logBuf
static void pushLog(String* logBuf, const String& line);
// 舵机决策：根据当前/上次朝向决定是否转、转到哪
static void applyServoForOrientation(const Config &cfg, const String &orientation, String* logBuf);
// 渲染阶段辅助函数的前置声明（定义在文件后半部，handleServo 在它们之前调用）
static String buildPhotoUrl(const Config &cfg, const String &suffix);
static String buildServerTimeUrl(const Config &cfg);
static bool fetchServerTime(const Config &cfg, struct tm &outLocal);
static bool fetchPhotoOrientation(const Config &cfg, int idx, String &outOrientation, String* logBuf);
static bool fetchPhotoBinToFramebuffer(const Config &cfg, int idx, const String &orientation,
                                       unsigned char* BlackImage, String* logBuf);
static void displayFramebuffer(unsigned char* BlackImage, const Config &cfg, const String &orientation);
static bool fetchCalibCardToFramebuffer(const Config &cfg, const String &calibName,
                                        const String &orientation,
                                        unsigned char* BlackImage, String* logBuf);
// HTML 组件前置声明（定义在文件前部 htmlEscape 之后）
static String htmlHead(const String& title, const char* activeKey);
static String htmlNav(const char* activeKey);
static String htmlFoot();
static String buildNeedNetworkPage();
// 路由 handler 前置声明
void handleRoot();         // /  (hub 首页)
void handleNetwork();      // /network
void handleScreen();       // /screen
void handleServoTest();    // /servo_test

// ── 手动调试模式（仅手动唤醒后生效；RAM 变量，每个唤醒周期独立）──
volatile bool g_debugMode         = false;   // /debug?state=on|off 切换
volatile bool g_debugOffRequested = false;   // 用户请求退出 debug → 进入 deep sleep
volatile bool g_requestHappened   = false;   // 任意请求触发，用于重置空闲计时
String        g_fetchLog;                     // /fetch 产出 + /log 下载的日志缓冲
static const uint32_t INITIAL_WINDOW_MS = 3UL  * 60UL * 1000UL;  // 手动唤醒后初始 web 在线窗口
static const uint32_t DEBUG_IDLE_MS    = 30UL * 60UL * 1000UL;  // debug 下无操作自动休眠

// ── EPD 屏幕存在检测（开机时探测一次）──
// false 时所有 EPD 调用静默跳过，避免无屏时 lcd_chkstatus 卡满 30s × 多次。
// 默认 true（乐观），detectEpdPresent() 在 setup() 早期修正它。
bool g_epdPresent = true;
static bool detectEpdPresent();   // 定义在 setup() 附近

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
  // 舵机标定参数（默认未标定）
  cfg.servo_portrait_deg  = prefs.getFloat("sv_p_deg", 0.0f);
  cfg.servo_landscape_deg = prefs.getFloat("sv_l_deg", 90.0f);
  cfg.servo_speed         = prefs.getFloat("sv_spd",   DEFAULT_SERVO_SPEED);
  cfg.servo_calibrated    = prefs.getBool("sv_cal",    false);
  cfg.landscape_invert    = prefs.getBool("ls_inv",    false);
  cfg.last_orientation    = prefs.getString("last_ori", "");
  prefs.end();

  cfg.valid = (cfg.wifi_ssid.length() > 0);

#if DEBUG_LOG
  DBG_PRINTLN("──── loadConfig ────");
  DBG_PRINTF("[CFG] ssid=%s\n",       cfg.wifi_ssid.c_str());
  DBG_PRINTF("[CFG] hostport=%s\n",   cfg.backend_hostport.c_str());
  DBG_PRINTF("[CFG] tz=%d, hour=%d\n", cfg.tz_offset_hours, (int)cfg.refresh_hour);
  DBG_PRINTF("[CFG] rotate180=%s\n",  cfg.rotate180 ? "true" : "false");
  DBG_PRINTF("[CFG] servo_cal=%s p=%.1f l=%.1f spd=%.1f inv=%s\n",
             cfg.servo_calibrated ? "Y" : "N",
             cfg.servo_portrait_deg, cfg.servo_landscape_deg,
             cfg.servo_speed, cfg.landscape_invert ? "Y" : "N");
  DBG_PRINTF("[CFG] last_ori=%s\n",   cfg.last_orientation.c_str());
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

// 保存舵机标定参数（独立于 saveConfig，避免每次改 WiFi 都重写舵机字段）
// 注：rot180（竖屏画面旋转 180°）也在此保存，让标定页能即时调整竖屏方向；
//     /network 页面的 saveConfig 仍会写同一个 NVS key，两边对 rot180 共享。
void saveServoConfig(const Config &cfg) {
  prefs.begin("dashcfg", false);
  prefs.putFloat("sv_p_deg", cfg.servo_portrait_deg);
  prefs.putFloat("sv_l_deg", cfg.servo_landscape_deg);
  prefs.putFloat("sv_spd",   cfg.servo_speed);
  prefs.putBool ("sv_cal",   cfg.servo_calibrated);
  prefs.putBool ("ls_inv",   cfg.landscape_invert);
  prefs.putBool ("rot180",   cfg.rotate180);
  prefs.end();
  DBG_PRINTLN("[CFG] 舵机标定已保存");
}

// 保存上次朝向（每次成功转动后调用，跨深睡存活）
void saveLastOrientation(const String &ori) {
  prefs.begin("dashcfg", false);
  prefs.putString("last_ori", ori);
  prefs.end();
  DBG_PRINTF("[CFG] last_orientation=%s 已保存\n", ori.c_str());
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
//  共享 HTML 组件（统一浅色主题 + 顶部导航）
//  所有页面通过 htmlHead/htmlFoot 包裹，单点维护样式和导航。
// ============================================================

// 模块定义：导航栏 + hub 卡片共用。activeKey 用于导航高亮。
struct WebModule {
  const char* key;     // 唯一标识
  const char* path;    // URL
  const char* title;   // 显示名
  const char* desc;    // 一句话描述（hub 卡片用）
  const char* icon;    // emoji 图标
  bool needsNetwork;   // true=依赖 STA 网络（AP 模式下灰显）
};

static const WebModule WEB_MODULES[] = {
  {"network", "/network",    "网络配置",     "WiFi / 服务器 / 刷新时间",          "📶", false},
  {"fetch",   "/fetch",      "图片拉取",     "手动拉取指定 idx 的照片并渲染",     "🖼", true},
  {"screen",  "/screen",     "屏幕显示测试", "发送 4 色测试条，验证墨水屏",        "📺", false},
  {"servotest","/servo_test", "舵机控制测试", "填角度+速度，纯转动验证（不保存）", "⚙", false},
  {"calib",   "/servo?test=show", "相框标定", "标定横竖屏角度+保存，端到端测试",   "🎯", false},
};
static const int WEB_MODULE_COUNT = sizeof(WEB_MODULES) / sizeof(WEB_MODULES[0]);

// 顶部导航条：返回 hub 首页 + 当前模块名。activeKey 高亮当前模块（nullptr 则只显首页链接）。
static String htmlNav(const char* activeKey) {
  String s;
  s += F("<nav class='topnav'><div class='nav-inner'>");
  s += F("<a href='/' class='brand'>🖼 InkTime</a>");
  if (activeKey) {
    // 找到当前模块名显示在导航上
    for (int i = 0; i < WEB_MODULE_COUNT; ++i) {
      if (strcmp(WEB_MODULES[i].key, activeKey) == 0) {
        s += F("<span class='nav-sep'>›</span><span class='nav-current'>");
        s += WEB_MODULES[i].title;
        s += F("</span>");
        break;
      }
    }
  }
  s += F("</div></nav>");
  return s;
}

// HTML 头部 + 共享 CSS（浅色主题）+ 导航条。
// activeKey: 当前模块 key（用于导航高亮），nullptr 表示是 hub 自身（不高亮任何模块）。
static String htmlHead(const String& title, const char* activeKey) {
  String s;
  s.reserve(2400);
  s += F("<!DOCTYPE html><html><head><meta charset='utf-8'>");
  s += F("<meta name='viewport' content='width=device-width,initial-scale=1'>");
  s += F("<title>");
  s += title;
  s += F("</title><style>");
  // 共享浅色主题（提取自原 buildConfigPage，统一全站）
  s += F("*{box-sizing:border-box;margin:0;padding:0}");
  s += F("body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;");
  s += F("background:#f0f2f5;color:#1a1a2e;padding:20px;min-height:100vh}");
  s += F(".card{max-width:480px;margin:0 auto;background:#fff;border-radius:16px;");
  s += F("box-shadow:0 4px 24px rgba(0,0,0,0.08);padding:28px;border:1px solid #e8eaed}");
  s += F("h2{text-align:center;color:#16213e;margin-bottom:6px;font-size:22px}");
  s += F("h3{color:#16213e;margin-bottom:12px;font-size:18px}");
  s += F(".subtitle{text-align:center;color:#888;font-size:13px;margin-bottom:20px}");
  s += F("label{display:block;font-weight:600;margin-bottom:6px;color:#333;font-size:14px}");
  s += F("input[type=text],input[type=password],input[type=number],select{width:100%;padding:10px 14px;");
  s += F("border:1.5px solid #ddd;border-radius:10px;font-size:15px;outline:none;");
  s += F("transition:border-color 0.2s;background:#fafbfc}");
  s += F("input:focus,select:focus{border-color:#4361ee}");
  s += F(".group{margin-bottom:18px}");
  s += F(".row{display:flex;gap:12px}");
  s += F(".row .group{flex:1}");
  s += F(".cb-group{display:flex;align-items:center;gap:8px;margin:18px 0}");
  s += F(".cb-group input{width:18px;height:18px;accent-color:#4361ee}");
  s += F(".cb-group label{margin:0;font-weight:400;font-size:14px}");
  s += F("button,.btn{display:inline-block;width:100%;padding:12px;background:linear-gradient(135deg,#4361ee,#3a0ca3);");
  s += F("color:#fff;border:none;border-radius:10px;font-size:15px;font-weight:600;");
  s += F("cursor:pointer;transition:opacity 0.2s;margin-top:8px;text-decoration:none;text-align:center}");
  s += F("button:hover,.btn:hover{opacity:0.9}");
  s += F(".btn-secondary{background:#e8eaed;color:#1a1a2e}");
  s += F(".btn-row{display:flex;gap:10px}");
  s += F(".btn-row button,.btn-row .btn{margin-top:0}");
  // 导航条样式
  s += F(".topnav{background:#fff;border-bottom:1px solid #e8eaed;margin:-20px -20px 20px;padding:14px 20px}");
  s += F(".nav-inner{max-width:480px;margin:0 auto;display:flex;align-items:center;gap:8px}");
  s += F(".brand{color:#16213e;text-decoration:none;font-weight:700;font-size:16px}");
  s += F(".nav-sep{color:#aaa}");
  s += F(".nav-current{color:#4361ee;font-weight:600;font-size:15px}");
  // hub 专用样式
  s += F(".grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:8px}");
  s += F(".module-card{display:block;background:#fff;border:1px solid #e8eaed;border-radius:14px;");
  s += F("padding:18px;text-decoration:none;color:#1a1a2e;transition:all 0.2s}");
  s += F(".module-card:hover{border-color:#4361ee;box-shadow:0 4px 16px rgba(67,97,238,0.12);transform:translateY(-1px)}");
  s += F(".module-card.disabled{opacity:0.45;pointer-events:none;background:#f8f9fa}");
  s += F(".module-card .ico{font-size:28px;margin-bottom:8px}");
  s += F(".module-card .t{font-weight:700;font-size:15px;margin-bottom:4px}");
  s += F(".module-card .d{font-size:12px;color:#888;line-height:1.4}");
  // 状态条
  s += F(".status-bar{background:#f8f9fa;border:1px solid #e8eaed;border-radius:10px;");
  s += F("padding:12px 14px;margin-bottom:18px;font-size:13px}");
  s += F(".status-bar .row-s{display:flex;justify-content:space-between;padding:3px 0}");
  s += F(".status-bar .k{color:#666}");
  s += F(".status-bar .v{font-weight:600}");
  s += F(".ok{color:#2e7d32}.warn{color:#c62828}");
  // 提示框
  s += F(".note{background:#fff8e1;border:1px solid #ffe082;border-radius:8px;");
  s += F("padding:10px 14px;font-size:13px;color:#6b5800;margin:12px 0}");
  s += F(".log{background:#0b0c10;color:#cfd8e3;padding:12px;border-radius:8px;");
  s += F("white-space:pre-wrap;font-family:monospace;font-size:12px;margin-top:12px;");
  s += F("max-height:300px;overflow:auto}");
  s += F("</style></head><body>");
  s += htmlNav(activeKey);
  return s;
}

static String htmlFoot() {
  return F("</body></html>");
}

// "需要先配网" 提示页（AP 模式下访问需要网络的模块时返回）
static String buildNeedNetworkPage() {
  String s = htmlHead(F("需要网络"), nullptr);
  s += F("<div class='card'>");
  s += F("<h3>⚠️ 需要先完成网络配置</h3>");
  s += F("<p style='color:#666;font-size:14px;margin:12px 0'>");
  s += F("此模块需要设备已连接 WiFi（STA 模式）。请先在网络配置里填好 WiFi 和服务器，");
  s += F("设备重启进入正常运行后，手动唤醒（按 RST 键）再访问。</p>");
  s += F("<a href='/' class='btn'>← 返回首页</a>");
  s += F("<a href='/network' class='btn btn-secondary' style='margin-top:10px'>去网络配置</a>");
  s += F("</div>");
  s += htmlFoot();
  return s;
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

  // ── HTML 头部（共享样式 + 导航）──
  html += htmlHead(F("网络配置 · InkTime"), "network");

  // ── 表单主体 ──
  html += F("<div class='card'>");
  html += F("<h2>📶 网络配置</h2>");
  html += F("<p class='subtitle'>WiFi / 服务器 / 刷新时间</p>");

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
  html += F("</form>");
  html += F("<a href='/' class='btn btn-secondary' style='margin-top:10px'>← 返回首页</a>");
  html += F("</div>");
  html += htmlFoot();

  return html;
}

// ============================================================
//  WebServer 路由处理
// ============================================================

// 构建状态条（hub 顶部显示设备状态）
static String buildStatusBar() {
  String s;
  s += F("<div class='status-bar'>");
  // 屏幕
  s += F("<div class='row-s'><span class='k'>屏幕</span><span class='v ");
  s += g_epdPresent ? F("ok'>已连接") : F("warn'>未检测到");
  s += F("</span></div>");
  // 舵机
  s += F("<div class='row-s'><span class='k'>舵机</span><span class='v ");
  s += g_cfg.servo_calibrated ? F("ok'>已标定") : F("warn'>未标定");
  s += F("</span></div>");
  // WiFi
  bool apMode = (WiFi.status() != WL_CONNECTED);
  s += F("<div class='row-s'><span class='k'>网络</span><span class='v ");
  s += apMode ? F("warn'>AP 配网模式") : F("ok'>STA 已连接");
  s += F("</span></div>");
  // IP
  s += F("<div class='row-s'><span class='k'>IP</span><span class='v'>");
  s += apMode ? WiFi.softAPIP().toString() : WiFi.localIP().toString();
  s += F("</span></div>");
  s += F("</div>");
  return s;
}

// GET / —— hub 首页（卡片导航）
void handleRoot() {
  DBG_PRINTLN("[HTTP] GET / (hub)");
  g_requestHappened = true;

  bool apMode = (WiFi.status() != WL_CONNECTED);

  String s = htmlHead(F("InkTime 控制台"), nullptr);
  s += F("<div class='card'>");
  s += F("<h2>🖼 InkTime</h2>");
  s += F("<p class='subtitle'>墨水屏相框控制台</p>");

  s += buildStatusBar();

  if (apMode) {
    s += F("<div class='note'>📡 AP 配网模式：请先完成「网络配置」连上 WiFi，");
    s += F("设备重启进入正常运行后，手动唤醒（按 RST）即可使用全部模块。</div>");
  }

  s += F("<div class='grid'>");
  for (int i = 0; i < WEB_MODULE_COUNT; ++i) {
    const WebModule& m = WEB_MODULES[i];
    bool disabled = apMode && m.needsNetwork;
    s += F("<a class='module-card");
    if (disabled) s += F(" disabled");
    s += F("' href='");
    s += m.path;
    s += F("'>");
    s += F("<div class='ico'>");
    s += m.icon;
    s += F("</div><div class='t'>");
    s += m.title;
    s += F("</div><div class='d'>");
    s += m.desc;
    if (disabled) s += F("（需先配网）");
    s += F("</div></a>");
  }
  s += F("</div>");  // .grid
  s += F("</div>");  // .card
  s += htmlFoot();
  server.send(200, "text/html; charset=utf-8", s);
}

// GET /network —— 网络配置表单（原 handleRoot 的内容）
void handleNetwork() {
  DBG_PRINTLN("[HTTP] GET /network");
  g_requestHappened = true;
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

  String resp = htmlHead(F("保存成功 · InkTime"), "network");
  resp += F("<div class='card' style='text-align:center'>");
  resp += F("<h3>✅ 保存成功</h3>");
  resp += F("<p style='color:#666;margin:12px 0'>设备即将重启...</p>");
  resp += F("</div>");
  resp += htmlFoot();

  server.send(200, "text/html; charset=utf-8", resp);
  delay(800);
  ESP.restart();
}

// ============================================================
//  手动调试路由：/fetch  /log  /debug
// ============================================================

// /fetch —— 图片拉取模块页。无 idx 参数显示选择器；有 idx 执行拉取渲染并显示日志。
void handleFetch() {
  g_requestHappened = true;
  DBG_PRINTLN("[HTTP] GET /fetch");

  // AP 模式 / hostport 为空：这个模块依赖服务器，提示先配网
  bool apMode = (WiFi.status() != WL_CONNECTED);
  bool noHost = (g_cfg.backend_hostport.length() == 0);
  if (apMode || noHost) {
    server.send(200, "text/html; charset=utf-8", buildNeedNetworkPage());
    return;
  }

  String s = htmlHead(F("图片拉取 · InkTime"), "fetch");
  s += F("<div class='card'>");
  s += F("<h2>🖼 图片拉取</h2>");
  s += F("<p class='subtitle'>手动拉取指定编号的照片并渲染到屏幕</p>");

  // idx 选择表单
  s += F("<div class='group'><label>照片编号 (0=评分最高)</label>");
  s += F("<select id='idx'>");
  for (int i = 0; i < DAILY_PHOTO_COUNT; ++i) {
    s += F("<option value='");
    s += String(i);
    s += F("'>photo_");
    s += String(i);
    s += F("</option>");
  }
  s += F("</select></div>");
  s += F("<button onclick='doFetch()'>📥 拉取并渲染</button>");
  s += F("<a href='/' class='btn btn-secondary' style='margin-top:10px'>← 返回首页</a>");
  s += F("<div id='log' class='log' style='display:none'></div>");
  s += F("</div>");

  s += F("<script>");
  s += F("function doFetch(){");
  s += F("var idx=document.getElementById('idx').value;");
  s += F("var el=document.getElementById('log');");
  s += F("el.style.display='block';el.textContent='正在拉取 idx='+idx+' ...（可能需要 10-30 秒）';");
  s += F("fetch('/fetch?idx='+idx).then(r=>r.text()).then(t=>{el.textContent=t;}).catch(e=>{el.textContent='请求失败: '+e;});");
  s += F("}");
  s += F("</script>");

  // 如果带 idx 参数（来自上面的 fetch 调用），执行拉取并返回纯文本日志（内联进 #log）
  if (server.hasArg("idx")) {
    g_fetchLog = "";
    pushLog(&g_fetchLog, String("[Fetch] 开始 idx=") + server.arg("idx") + " ms=" + (int)millis());

    int idx = server.arg("idx").toInt();
    if (idx < 0 || idx >= DAILY_PHOTO_COUNT) {
      pushLog(&g_fetchLog, String("[Fetch] idx 非法: ") + idx + "，回退为 0");
      idx = 0;
    }
    journalEvent(EV_HTTP, "ep=/fetch idx=%d", idx);

    bool ok = downloadAndRenderDailyPhoto(g_cfg, idx, &g_fetchLog);
    pushLog(&g_fetchLog, ok ? "[Fetch] 结果: 成功 ✅" : "[Fetch] 结果: 失败 ❌");

    // 直接返回纯文本日志（JS 端塞进 #log）
    server.send(200, "text/plain; charset=utf-8", g_fetchLog);
    return;
  }

  s += htmlFoot();
  server.send(200, "text/html; charset=utf-8", s);
}

// /log —— 下载累计的拉取日志
//   默认内联显示（首行是 DEVLOG 诊断状态，便于排查设备日志上传链路）；
//   ?dl=1 才加下载头（原行为）。
void handleLog() {
  g_requestHappened = true;
  DBG_PRINTLN("[HTTP] GET /log");
  if (server.hasArg("dl")) {
    server.sendHeader("Content-Disposition", "attachment; filename=inktime_fetch.log");
  }
  String body = String("== DEVLOG ") + journalStatus() + " ==\n";
  body += g_fetchLog.length() ? g_fetchLog : String(F("(暂无日志)"));
  server.send(200, "text/plain; charset=utf-8", body);
}

// /debug?state=on|off|confirm_off —— 切换 debug 模式（两步关闭，关前提示下载日志）
void handleDebug() {
  g_requestHappened = true;
  String st = server.hasArg("state") ? server.arg("state") : "";
  st.toLowerCase();
  DBG_PRINTF("[HTTP] GET /debug state=%s\n", st.c_str());

  String html = htmlHead(F("Debug · InkTime"), nullptr);
  html += F("<div class='card' style='text-align:center'>");

  if (st == "on") {
    g_debugMode = true;
    g_debugOffRequested = false;
    html += F("<h3>🔧 Debug 模式已开启</h3>");
    html += F("<p style='color:#666;margin:12px 0'>设备保持唤醒，直到你手动关闭或 30 分钟无操作。</p>");
    html += F("<p style='margin-top:16px'><a href='/' class='btn'>← 返回首页</a></p>");
  } else if (st == "off") {
    html += F("<h3>准备退出 Debug 模式</h3>");
    html += F("<p style='color:#666;margin:12px 0'>设备即将进入 Deep Sleep。请先下载本次日志：</p>");
    html += F("<p style='margin-top:16px'><a href='/log' class='btn'>⬇️ 下载日志 (inktime_fetch.log)</a></p>");
    html += F("<p><a href='/debug?state=confirm_off' class='btn btn-secondary'>确认退出并休眠 →</a></p>");
  } else if (st == "confirm_off") {
    html += F("<h3>已退出 Debug，进入 Deep Sleep...</h3>");
    g_debugOffRequested = true;
  } else {
    html += F("<h3>🔧 InkTime Debug</h3>");
    html += F("<p style='color:#666;margin:12px 0'>当前 debug 模式：<strong>");
    html += (g_debugMode ? F("开启") : F("关闭"));
    html += F("</strong></p>");
    html += F("<p style='margin-top:16px'><a href='/debug?state=on' class='btn'>开启 debug</a></p>");
    html += F("<p><a href='/debug?state=off' class='btn btn-secondary'>关闭 debug</a></p>");
    html += F("<p style='margin-top:16px'><a href='/' class='btn btn-secondary'>← 返回首页</a></p>");
  }
  html += F("</div>");
  html += htmlFoot();
  server.send(200, "text/html; charset=utf-8", html);
}

// /screen —— 屏幕显示测试模块页。
//   无参数：显示页面（屏幕状态 + "发送 4 色测试条"按钮）。
//   ?run=1：执行 4 色测试图案刷新，返回纯文本结果（AJAX 用）。
void handleScreen() {
  g_requestHappened = true;

  // ?run=1 执行测试
  if (server.hasArg("run")) {
    if (!g_epdPresent) {
      server.send(200, "text/plain; charset=utf-8", "❌ 未检测到屏幕，无法测试。");
      return;
    }
    DBG_PRINTLN("[HTTP] GET /screen?run (4 色测试图案)");

    size_t epd_array_size = (size_t)EPD_WIDTH * EPD_HEIGHT / 4;
    unsigned char* BlackImage = (unsigned char*)heap_caps_malloc(epd_array_size, MALLOC_CAP_8BIT | MALLOC_CAP_SPIRAM);
    if (!BlackImage) BlackImage = (unsigned char*)heap_caps_malloc(epd_array_size, MALLOC_CAP_8BIT);
    if (!BlackImage) {
      server.send(200, "text/plain; charset=utf-8", "❌ 内存分配失败");
      return;
    }

    uint16_t paintRotate = g_cfg.rotate180 ? ROTATE_270 : ROTATE_90;
    Paint_NewImage(BlackImage, EPD_WIDTH, EPD_HEIGHT, paintRotate, WHITE0);
    Paint_SetScale(4);
    Paint_SelectImage(BlackImage);

    // 4 条横带：黑 / 白 / 红 / 黄
    for (int y = 0; y < FB_HEIGHT; ++y) {
      uint8_t c;
      if      (y < FB_HEIGHT / 4)     c = BLACK0;
      else if (y < FB_HEIGHT / 2)     c = WHITE0;
      else if (y < FB_HEIGHT * 3 / 4) c = RED0;
      else                            c = YELLOW0;
      for (int x = 0; x < FB_WIDTH; ++x) Paint_SetPixel(x, y, c);
    }

    DBG_PRINTLN("[TEST] 刷新测试图案...");
    EPD_init();
    PIC_display(BlackImage);
    EPD_DeepSleep();
    heap_caps_free(BlackImage);
    server.send(200, "text/plain; charset=utf-8", "✅ 已发送 4 色测试图案（黑/白/红/黄），请看屏幕。");
    return;
  }

  // 显示页面
  DBG_PRINTLN("[HTTP] GET /screen");
  String s = htmlHead(F("屏幕显示测试 · InkTime"), "screen");
  s += F("<div class='card'>");
  s += F("<h2>📺 屏幕显示测试</h2>");
  s += F("<p class='subtitle'>发送 4 色测试条验证墨水屏</p>");

  // 屏幕状态
  s += F("<div class='status-bar'>");
  s += F("<div class='row-s'><span class='k'>屏幕</span><span class='v ");
  s += g_epdPresent ? F("ok'>已连接") : F("warn'>未检测到");
  s += F("</span></div></div>");

  if (!g_epdPresent) {
    s += F("<div class='note'>⚠️ 未检测到屏幕接入。请检查排线，或重启设备重新探测。");
    s += F("（无屏状态下按钮已禁用）</div>");
    s += F("<button disabled>🎨 发送 4 色测试条</button>");
  } else {
    s += F("<button onclick='runTest()'>🎨 发送 4 色测试条</button>");
    s += F("<div id='log' class='log' style='display:none'></div>");
    s += F("<script>");
    s += F("function runTest(){");
    s += F("var el=document.getElementById('log');");
    s += F("el.style.display='block';el.textContent='正在刷新...（约 12-16 秒）';");
    s += F("fetch('/screen?run=1').then(r=>r.text()).then(t=>{el.textContent=t;}).catch(e=>{el.textContent='请求失败: '+e;});");
    s += F("}");
    s += F("</script>");
  }
  s += F("<a href='/' class='btn btn-secondary' style='margin-top:10px'>← 返回首页</a>");
  s += F("</div>");
  s += htmlFoot();
  server.send(200, "text/html; charset=utf-8", s);
}

// ============================================================
//  /servo_test —— 舵机控制测试（极简纯转动页）
//    用途：装相框时随手转转找物理 home 位。
//    与 /servo（标定页）的区别：只填角度+速度+点转动，不 fetch 标定卡、不刷屏、不写 NVS。
//    ?run=1 时执行 servo_rotate_to，返回纯文本结果（AJAX 用）。
// ============================================================
void handleServoTest() {
  g_requestHappened = true;

  // ?run=1 + angle + speed 参数：执行转动
  if (server.hasArg("run")) {
    if (!server.hasArg("angle")) {
      server.send(200, "text/plain; charset=utf-8", "❌ 缺 angle 参数");
      return;
    }
    float angle = server.arg("angle").toFloat();
    float speed = server.hasArg("speed") ? server.arg("speed").toFloat() : g_cfg.servo_speed;
    if (speed < 1.0f) speed = 40.0f;

    DBG_PRINTF("[HTTP] /servo_test?run angle=%.1f speed=%.1f\n", angle, speed);
    journalEvent(EV_HTTP, "ep=/servo_test angle=%.1f speed=%.1f", angle, speed);
    bool ok = servo_rotate_to(angle, speed, 3000);
    journalEvent(EV_SDONE, ok ? "ok test angle=%.1f" : "timeout test angle=%.1f", angle);
    String msg = ok ? (String("✅ 转到 ") + angle + "° 完成") 
                    : (String("⚠️ 转到 ") + angle + "° 超时（已 detach）");
    server.send(200, "text/plain; charset=utf-8", msg);
    return;
  }

  // 显示页面
  DBG_PRINTLN("[HTTP] GET /servo_test");
  String s = htmlHead(F("舵机控制测试 · InkTime"), "servotest");
  s += F("<div class='card'>");
  s += F("<h2>⚙ 舵机控制测试</h2>");
  s += F("<p class='subtitle'>填角度+速度，纯转动验证（不保存、不刷屏）</p>");

  s += F("<div class='group'><label>目标角度 (°)</label>");
  s += F("<input id='angle' type='number' step='1' value='0'></div>");
  s += F("<div class='group'><label>转动速度 (°/s, 默认 ");
  s += String(g_cfg.servo_speed, 0);
  s += F(")</label><input id='speed' type='number' step='1' value='");
  s += String(g_cfg.servo_speed, 0);
  s += F("'></div>");

  s += F("<button onclick='doRotate()'>🔄 转动到该角度</button>");
  s += F("<a href='/' class='btn btn-secondary' style='margin-top:10px'>← 返回首页</a>");
  s += F("<a href='/servo?test=show' class='btn btn-secondary' style='margin-top:10px'>→ 去正式标定</a>");

  s += F("<div id='log' class='log' style='display:none'></div>");
  s += F("</div>");

  s += F("<script>");
  s += F("function doRotate(){");
  s += F("var a=document.getElementById('angle').value;");
  s += F("var sp=document.getElementById('speed').value;");
  s += F("var el=document.getElementById('log');");
  s += F("el.style.display='block';el.textContent='正在转到 '+a+'° ...';");
  s += F("fetch('/servo_test?run=1&angle='+a+'&speed='+sp).then(r=>r.text()).then(t=>{el.textContent=t;}).catch(e=>{el.textContent='请求失败: '+e;});");
  s += F("}");
  s += F("</script>");

  s += htmlFoot();
  server.send(200, "text/html; charset=utf-8", s);
}


//  - "测试"用表单当前值（非已保存值），方便反复迭代标定不用先存
//  - fetch 失败时仍执行舵机转动（不刷屏），方便在服务器还没配好时调机械角度
//  - 测试路径与每日正常运行共用 servo_rotate_to / displayFramebuffer，标定=端到端真测试
// ============================================================

// 解析表单 float 参数（缺省回退到已保存值）
static float parseServoArg(const String &name, float fallback) {
  if (!server.hasArg(name)) return fallback;
  String v = server.arg(name);
  v.trim();
  if (v.length() == 0) return fallback;
  return v.toFloat();
}

// 把多行纯文本日志转成 HTML（换行→<br>，转义 <>&），
// 供 /servo、/servo_sync 等返回 innerHTML 的接口共用
static String logToHtml(const String &log) {
  String out;
  out.reserve(log.length() + 64);
  for (size_t i = 0; i < log.length(); ++i) {
    char c = log[i];
    if      (c == '\n') out += F("<br>");
    else if (c == '<')  out += F("&lt;");
    else if (c == '>')  out += F("&gt;");
    else if (c == '&')  out += F("&amp;");
    else                out += c;
  }
  return out;
}

// 标定页 HTML（显示当前已保存值 + 表单 + 测试/保存按钮）
static String buildServoPage(const Config &cfg) {
  String html = htmlHead(F("相框标定 · InkTime"), "calib");
  html += F("<div class='card'>");
  html += F("<h2>🎯 相框标定</h2>");
  html += F("<p class='subtitle'>标定横竖屏角度+保存，端到端测试</p>");
  html += F("<p style='font-size:12px;color:#888;margin:0 0 16px'>");
  html += F("填入当前值 → 点测试按钮验证 → 满意后保存。测试用表单当前值（非已保存值）。</p>");

  html += F("<form id='f' method='post' action='/servo_save'>");
  html += F("<div class='group'><label>竖屏角度 (°)</label>");
  html += F("<input type='number' step='0.1' name='p_deg' value='");
  html += String(cfg.servo_portrait_deg, 1);
  html += F("'></div>");
  html += F("<div class='group'><label>横屏角度 (°)</label>");
  html += F("<input type='number' step='0.1' name='l_deg' value='");
  html += String(cfg.servo_landscape_deg, 1);
  html += F("'></div>");
  html += F("<div class='group'><label>转动速度 (°/s, 默认 ");
  html += String(cfg.servo_speed, 0);
  html += F(")</label><input type='number' step='1' name='spd' value='");
  html += String(cfg.servo_speed, 1);
  html += F("'></div>");
  html += F("<div class='cb-group'><input type='checkbox' id='rot180' name='rot180' value='1'");
  if (cfg.rotate180) html += F(" checked");
  html += F("><label for='rot180'>竖屏画面旋转 180°</label></div>");
  html += F("<div class='cb-group'><input type='checkbox' id='inv' name='inv' value='1'");
  if (cfg.landscape_invert) html += F(" checked");
  html += F("><label for='inv'>横屏画面方向反转</label></div>");
  html += F("</form>");

  html += F("<div class='btn-row'>");
  // 测试按钮：把表单值塞 query string 走 GET /servo?test=*
  html += F("<button class='btn-test' onclick=\"testOri('portrait')\">测试竖屏</button>");
  html += F("<button class='btn-test' onclick=\"testOri('landscape')\">测试横屏</button>");
  html += F("</div>");
  html += F("<button class='btn-save' onclick=\"document.getElementById('f').submit()\">💾 保存标定</button>");

  // ── 舵机位置校正（断电/卡死/人手拨动后，物理位置与记录不符时用）──
  html += F("<hr style='margin:24px 0;border:none;border-top:1px solid #e8eaed'>");
  html += F("<h3 style='margin-bottom:4px'>🧭 当前位置校正</h3>");
  html += F("<p style='font-size:12px;color:#888;margin:0 0 6px'>记录的舵机朝向：<b>");
  html += (cfg.last_orientation.length() ? cfg.last_orientation : String("(空·首次开机)"));
  html += F("</b></p>");
  html += F("<p style='font-size:12px;color:#888;margin:0 0 12px'>");
  html += F("若舵机实际朝向与上面记录不符（如断电、结构卡死或人手拨动过），");
  html += F("目视选择舵机<b>当前真实</b>朝向后点校正：固件会刷新记录并重新拉取 photo_0，");
  html += F("舵机按需转到正确角度。</p>");
  html += F("<div class='group'><label>舵机当前真实朝向</label>");
  html += F("<select id='actual_ori'>");
  // 默认选中当前记录值，让"记录与实际不符"一眼可见
  html += F("<option value='portrait'");  if (cfg.last_orientation != "landscape") html += F(" selected");
  html += F(">竖屏 (portrait)</option>");
  html += F("<option value='landscape'"); if (cfg.last_orientation == "landscape") html += F(" selected");
  html += F(">横屏 (landscape)</option>");
  html += F("</select></div>");
  html += F("<button class='btn-save' style='background:linear-gradient(135deg,#f59e0b,#b45309)' ");
  html += F("onclick=\"syncPos()\">🧭 校正并重新渲染</button>");

  html += F("<a href='/' class='btn btn-secondary' style='margin-top:10px'>← 返回首页</a>");
  html += F("<a href='/servo_test' class='btn btn-secondary' style='margin-top:10px'>→ 简单舵机测试</a>");

  html += F("<div id='log' class='log' style='display:none'></div>");
  html += F("</div>");

  html += F("<script>");
  html += F("function fv(n){var e=document.querySelector(\"[name='\"+n+\"']\");return e?encodeURIComponent(e.value):'';}");
  html += F("function finv(){return document.getElementById('inv').checked?'1':'0';}");
  html += F("function frot(){return document.getElementById('rot180').checked?'1':'0';}");
  html += F("function testOri(o){");
  html += F("var q='p_deg='+fv('p_deg')+'&l_deg='+fv('l_deg')+'&spd='+fv('spd')+'&inv='+finv()+'&rot180='+frot();");
  html += F("var u='/servo?test='+o+'&'+q;");
  html += F("document.getElementById('log').style.display='block';");
  html += F("document.getElementById('log').textContent='正在执行：'+o+' ...（含下载标定卡+转舵机+刷屏，约 15-20 秒）';");
  html += F("fetch(u).then(r=>r.text()).then(t=>{document.getElementById('log').innerHTML=t;});");
  html += F("}");
  html += F("function syncPos(){");
  html += F("var a=document.getElementById('actual_ori').value;");
  html += F("if(!confirm('确认舵机当前真实朝向是「'+(a==='portrait'?'竖屏':'横屏')+'」？\\n将刷新记录并重新拉取 photo_0，约 15-20 秒。'))return;");
  html += F("var u='/servo_sync?actual='+a;");
  html += F("document.getElementById('log').style.display='block';");
  html += F("document.getElementById('log').textContent='正在校正 → '+a+'，并重新拉取 photo_0 ...（约 15-20 秒）';");
  html += F("fetch(u).then(r=>r.text()).then(t=>{document.getElementById('log').innerHTML=t;});");
  html += F("}");
  html += F("</script>");

  html += htmlFoot();
  return html;
}

// GET /servo?test=portrait|landscape|show
void handleServo() {
  g_requestHappened = true;
  String action = server.hasArg("test") ? server.arg("test") : String("show");
  DBG_PRINTF("[HTTP] GET /servo test=%s\n", action.c_str());

  if (action == "show") {
    server.send(200, "text/html; charset=utf-8", buildServoPage(g_cfg));
    return;
  }
  if (action != "portrait" && action != "landscape") {
    server.send(400, "text/plain; charset=utf-8", "test must be portrait|landscape|show");
    return;
  }

  // 从表单读当前值（缺省回退到已保存值）
  Config trial = g_cfg;
  trial.servo_portrait_deg  = parseServoArg("p_deg", g_cfg.servo_portrait_deg);
  trial.servo_landscape_deg = parseServoArg("l_deg", g_cfg.servo_landscape_deg);
  trial.servo_speed         = parseServoArg("spd",   g_cfg.servo_speed);
  trial.landscape_invert    = server.hasArg("inv")    && server.arg("inv")    == "1";
  trial.rotate180           = server.hasArg("rot180") && server.arg("rot180") == "1";

  String log;
  String ori = action;   // "portrait" or "landscape"
  log += String("[标定] 测试 ") + ori + "\n";
  log += String("  竖屏=") + trial.servo_portrait_deg + " 横屏=" + trial.servo_landscape_deg;
  log += String(" 速度=") + trial.servo_speed + " invert=" + (trial.landscape_invert ? "Y" : "N")
       + " rot180=" + (trial.rotate180 ? "Y" : "N") + "\n";

  // 1. 转舵机（用表单当前值；无论后续 fetch 是否成功都转，方便调机械角度）
  float target = (ori == "landscape") ? trial.servo_landscape_deg : trial.servo_portrait_deg;
  log += String("[舵机] 转 → ") + target + "° @ " + trial.servo_speed + "°/s\n";
  bool servOk = servo_rotate_to(target, trial.servo_speed, 3000);
  log += servOk ? "[舵机] 到位 ✅\n" : "[舵机] 超时（已 detach）\n";
  // 同步逻辑朝向状态：测试转过哪一边，last_orientation 就记哪一边，
  // 避免"测了横屏→人手拨回竖屏→状态仍卡在横屏→下次刷照片误判同朝向跳过"。
  if (servOk) {
    saveLastOrientation(ori);
    g_cfg.last_orientation = ori;   // 同步内存，本会话内后续 /fetch 立刻生效
  }

  // 2. 分配 EPD framebuffer（96KB，ESP32-L 可分配；不要 384KB 中转 buffer）
  size_t epd_array_size = (size_t)EPD_WIDTH * EPD_HEIGHT / 4;  // 96,000 bytes
  unsigned char* BlackImage = (unsigned char*)heap_caps_malloc(epd_array_size, MALLOC_CAP_8BIT | MALLOC_CAP_SPIRAM);
  if (!BlackImage) BlackImage = (unsigned char*)heap_caps_malloc(epd_array_size, MALLOC_CAP_8BIT);
  if (!BlackImage) {
    log += "[标定] framebuffer 分配失败，仅转舵机不刷屏\n";
  } else {
    // 3. 流式拉取标定卡直写 framebuffer，再刷屏
    String calibName = (ori == "landscape") ? String("calib_l.bin") : String("calib_p.bin");
    bool fetchOk = fetchCalibCardToFramebuffer(trial, calibName, ori, BlackImage, &log);
    if (fetchOk) {
      log += "[标定] 渲染标定卡 (" + ori + ")\n";
      displayFramebuffer(BlackImage, trial, ori);
      log += "[标定] 完成 ✅\n";
    } else {
      log += "[标定] 标定卡 fetch 失败，仅转舵机不刷屏（仍可调机械角度）\n";
    }
    heap_caps_free(BlackImage);
  }

  // 返回日志（fetch 回调里 innerHTML 显示）
  server.send(200, "text/html; charset=utf-8", logToHtml(log));
}

// POST /servo_save —— 写入 4 字段 + servo_calibrated=true
void handleServoSave() {
  g_requestHappened = true;
  DBG_PRINTLN("[HTTP] POST /servo_save");

  Config newCfg = g_cfg;
  newCfg.servo_portrait_deg  = parseServoArg("p_deg", g_cfg.servo_portrait_deg);
  newCfg.servo_landscape_deg = parseServoArg("l_deg", g_cfg.servo_landscape_deg);
  newCfg.servo_speed         = parseServoArg("spd",   g_cfg.servo_speed);
  newCfg.landscape_invert    = server.hasArg("inv")    && server.arg("inv")    == "1";
  newCfg.rotate180           = server.hasArg("rot180") && server.arg("rot180") == "1";
  newCfg.servo_calibrated    = true;   // 保存即视为已标定
  saveServoConfig(newCfg);
  g_cfg = newCfg;
  servo_set_default_speed(g_cfg.servo_speed);   // 立即生效（不必等下次重启）
  journalEvent(EV_HTTP, "ep=/servo_save p=%.1f l=%.1f spd=%.1f",
               g_cfg.servo_portrait_deg, g_cfg.servo_landscape_deg, g_cfg.servo_speed);

  String html = htmlHead(F("标定已保存 · InkTime"), "calib");
  html += F("<div class='card' style='text-align:center'>");
  html += F("<h3>✅ 标定已保存</h3>");
  html += F("<p style='color:#666;margin:12px 0'>舵机功能已启用，下次每日刷新将自动旋转。</p>");
  html += F("<a href='/servo?test=show' class='btn'>← 返回标定页</a>");
  html += F("<a href='/' class='btn btn-secondary' style='margin-top:10px'>返回首页</a>");
  html += F("</div>");
  html += htmlFoot();
  server.send(200, "text/html; charset=utf-8", html);
}

// GET /servo_sync?actual=portrait|landscape —— 位置校正
//   用途：断电/卡死/人手拨动后，舵机物理位置可能与 NVS 记录的 last_orientation 不符。
//   用户目视确认舵机当前真实朝向，选 portrait/landscape 提交：
//     1. 把 last_orientation 直接置为该值（不转舵机）；
//     2. 立即重新拉取 photo_0 渲染——applyServoForOrientation 会用新的 last_orientation
//        与 photo_0.json 的 orientation 对比，该跳就跳、该转就转，自愈到一致状态。
//   若此时设备离线，photo_0 拉取会失败，但 last_orientation 校正仍已落盘，下次刷新自愈。
void handleServoSync() {
  g_requestHappened = true;
  String actual = server.arg("actual");
  DBG_PRINTF("[HTTP] GET /servo_sync actual=%s\n", actual.c_str());
  journalEvent(EV_HTTP, "ep=/servo_sync actual=%s", actual.c_str());

  String log;
  if (actual != "portrait" && actual != "landscape") {
    log += "[校正] actual 参数非法（需 portrait|landscape），已中止\n";
    server.send(400, "text/html; charset=utf-8", logToHtml(log));
    return;
  }

  log += "[校正] 舵机物理位置 → " + actual + "\n";
  if (actual == g_cfg.last_orientation) {
    log += "[校正] 与记录一致（" + actual + "），无需修正 last_orientation\n";
  } else {
    log += "[校正] 记录原为 " + (g_cfg.last_orientation.length() ? g_cfg.last_orientation : String("(空)"))
         + "，已修正为 " + actual + "\n";
    saveLastOrientation(actual);
    g_cfg.last_orientation = actual;   // 同步内存，本次重新渲染立刻生效
  }

  log += "[校正] 重新拉取 photo_0 ...\n";
  bool ok = downloadAndRenderDailyPhoto(g_cfg, 0, &log);
  log += ok ? "[校正] 渲染完成 ✅\n" : "[校正] 渲染失败 ❌（last_orientation 校正仍已保存，下次刷新自愈）\n";

  server.send(200, "text/html; charset=utf-8", logToHtml(log));
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

  // 设备日志：先记 SLEEP（本周期最后一条），再趁 WiFi 在线把增量推给树莓派。
  // 所有休眠路径（定时/手动窗口结束/授时失败兜底）都汇聚到本函数，是唯一咽喉点。
  journalEvent(EV_SLEEP, "next_min=%u heap=%u", (unsigned)minutes, (unsigned)ESP.getFreeHeap());
  journalUpload();

  uint64_t us = (uint64_t)minutes * 60ULL * 1000000ULL;

  // 关闭外设
  powerDownEPD();
  servo_detach();   // 舵机停止 PWM 省电（运行态保持 attach 平顺，仅深睡前 detach）

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
  journalEvent(EV_AP, "ssid=%s ok=%d", apSsid.c_str(), (int)apOk);
  DBG_PRINTF("[CFG] SSID: %s, IP: %s\n", apSsid.c_str(), WiFi.softAPIP().toString().c_str());

  // AP 模式：hub + 网络配置 + 不依赖网络的本地模块（屏幕测试、舵机测试）。
  // 依赖网络的模块（fetch/servo 标定）路由也注册，但 handler 内部会检测并提示先配网。
  server.on("/",          HTTP_GET,  handleRoot);
  server.on("/network",   HTTP_GET,  handleNetwork);
  server.on("/save",      HTTP_POST, handleSave);
  server.on("/screen",    HTTP_GET,  handleScreen);
  server.on("/servo_test",HTTP_GET,  handleServoTest);
  server.on("/fetch",     HTTP_GET,  handleFetch);
  server.on("/servo",     HTTP_GET,  handleServo);
  server.on("/servo_save",HTTP_POST, handleServoSave);
  server.on("/servo_sync",HTTP_GET,  handleServoSync);
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
    journalEvent(EV_WIFI, "ok ip=%s", WiFi.localIP().toString().c_str());
  } else {
    DBG_PRINTLN("[WIFI] 连接失败");
    journalEvent(EV_WIFI, "fail ssid=%s timeout_ms=%u", cfg.wifi_ssid.c_str(), (unsigned)timeout_ms);
  }

  return ok;
}

// ============================================================
//  时间同步（局域网服务器授时优先，失败回退公网 NTP）
//
//  设备本来就要连本 server 拉照片，/time 授时（局域网）比公网 NTP
//  （pool.ntp.org 等，易因路由器/运营商抖动失败）更快更可靠，
//  因此先走 fetchServerTime；NTP 作为回退，重试从 30 次收紧到 5 次
//  （原 30 次 × 500ms 最多白等 15s）。
//  两层都失败才返回 false（调用方 setup() 的二次重试只调 fetchServerTime）。
// ============================================================
bool syncTime(const Config &cfg, struct tm &outLocal) {
  // 局域网服务器授时优先
  if (fetchServerTime(cfg, outLocal)) {
    return true;
  }

  DBG_PRINTLN("[TIME] 服务器授时失败，尝试公网 NTP 同步...");
  long offsetSec = (long)cfg.tz_offset_hours * 3600;
  configTime(offsetSec, 0, "pool.ntp.org", "time.nist.gov", "ntp.aliyun.com");

  for (int i = 0; i < 5; ++i) {
    if (getLocalTime(&outLocal)) {
      char buf[64];
      strftime(buf, sizeof(buf), "%Y-%m-%d %H:%M:%S", &outLocal);
      DBG_PRINTF("[TIME] 同步成功: %s\n", buf);

      time_t nowEpoch = time(nullptr);
      if (nowEpoch > 0) saveLastTimeEpoch(nowEpoch);
      journalEvent(EV_TIME, "src=ntp anchor=%lld", (long long)nowEpoch);
      return true;
    }
    delay(500);
  }

  DBG_PRINTLN("[TIME] NTP 同步失败，再试一次服务器授时...");
  // NTP 也失败 → 最后再给局域网授时一次机会（链路可能刚恢复）
  if (fetchServerTime(cfg, outLocal)) {
    return true;  // 服务器授时成功
  }

  DBG_PRINTLN("[TIME] 服务器授时与 NTP 均失败");
  journalEvent(EV_TIME, "fail lan+ntp");
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

// ============================================================
//  构建下载 URL（hostport + prefix + 名字）
// ============================================================
static String buildPhotoUrl(const Config &cfg, const String &suffix) {
  String hp = cfg.backend_hostport;
  hp.trim();
  if (hp.startsWith("http://") || hp.startsWith("https://")) {
    return hp + String(DAILY_PHOTO_PATH_PREFIX) + suffix;
  }
  return "http://" + hp + String(DAILY_PHOTO_PATH_PREFIX) + suffix;
}

// 构建服务器授时 URL：/static/inktime/<key>/time
// 与 DAILY_PHOTO_PATH_PREFIX 同前缀，但末尾是 /time 而非 photo_*
// DAILY_PHOTO_PATH_PREFIX = "/static/inktime/andyhome0203/photo_"
// 授时接口              = "/static/inktime/andyhome0203/time"
// 去掉末尾 "photo_"（6 字符）拼上 "time" 即可。
// 前缀剥离逻辑抽成 pathPrefixBase()，与 calib 卡 URL 共用（原先重复 3 处）。
static String pathPrefixBase() {
  String prefix = String(DAILY_PHOTO_PATH_PREFIX);
  prefix.remove(prefix.length() - 6);  // 去掉末尾 "photo_"
  return prefix;
}

static String buildServerTimeUrl(const Config &cfg) {
  String hp = cfg.backend_hostport;
  hp.trim();
  String base = hp.startsWith("http://") || hp.startsWith("https://")
                ? hp : ("http://" + hp);
  return base + pathPrefixBase() + String("time");
}

// 从服务器 /time 接口拉取 Unix epoch，用 settimeofday() 设系统时钟。
// 作为公网 NTP 失败时的回退授时源——设备本就要连本 server 拉照片，
// 局域网授时比公网 NTP 更可靠（不依赖外网）。
// 成功返回 true，outLocal 填入本地时间。
static bool fetchServerTime(const Config &cfg, struct tm &outLocal) {
  String url = buildServerTimeUrl(cfg);
  DBG_PRINTF("[TIME] 服务器授时 GET %s\n", url.c_str());

  HTTPClient http;
  http.begin(url);
  http.setTimeout(8000);
  int code = http.GET();
  if (code != HTTP_CODE_OK) {
    DBG_PRINTF("[TIME] 服务器授时 HTTP 失败 code=%d\n", code);
    http.end();
    return false;
  }
  String body = http.getString();
  http.end();
  body.trim();

  time_t epoch = (time_t)body.toInt();
  if (epoch <= 1700000000) {  // 2023 年之前的值视为非法
    DBG_PRINTF("[TIME] 服务器授时返回非法 epoch: %s\n", body.c_str());
    return false;
  }

  // 用 settimeofday 直接设系统时钟（UTC），再应用时区偏移
  struct timeval tv;
  tv.tv_sec = epoch;
  tv.tv_usec = 0;
  settimeofday(&tv, nullptr);
  // 重应用时区（settimeofday 会保留 tz 设置，但显式重设更稳妥）
  long offsetSec = (long)cfg.tz_offset_hours * 3600;
  configTime(offsetSec, 0, nullptr);  // 不带 NTP server，仅设 tz

  if (!getLocalTime(&outLocal, 2000)) {
    DBG_PRINTLN("[TIME] 服务器授时后 getLocalTime 仍失败");
    return false;
  }

  char buf[64];
  strftime(buf, sizeof(buf), "%Y-%m-%d %H:%M:%S", &outLocal);
  DBG_PRINTF("[TIME] 服务器授时成功: %s\n", buf);
  saveLastTimeEpoch(epoch);
  journalEvent(EV_TIME, "src=lan anchor=%lld", (long long)epoch);   // 锚点：服务器据此换算绝对时间
  return true;
}

// ============================================================
//  fetch photo_N.json sidecar，解析出 orientation
//    成功返回 true 并填 outOrientation（"portrait"/"landscape"）
//    失败返回 false（调用方负责降级）
// ============================================================
static bool fetchPhotoOrientation(const Config &cfg, int idx, String &outOrientation, String* logBuf) {
  String url = buildPhotoUrl(cfg, String(idx) + ".json");
  DBG_PRINTF("[HTTP] GET %s\n", url.c_str());
  pushLog(logBuf, String("[朝向] GET ") + url);

  HTTPClient http;
  http.begin(url);
  http.setTimeout(8000);
  int code = http.GET();
  if (code != HTTP_CODE_OK) {
    pushLog(logBuf, String("[朝向] HTTP 失败 code=") + code + "，将降级用 last_orientation");
    journalEvent(EV_PJSON, "fail idx=%d code=%d", idx, code);
    http.end();
    return false;
  }
  String body = http.getString();
  http.end();

  // 极简 JSON 解析：找 "orientation":"xxx"
  // 不引入 ArduinoJson，sidecar 就这一行
  int k = body.indexOf("\"orientation\"");
  if (k < 0) { pushLog(logBuf, "[朝向] json 无 orientation 字段"); journalEvent(EV_PJSON, "fail idx=%d no_field", idx); return false; }
  int c1 = body.indexOf('"', k + 13);
  int c2 = body.indexOf('"', c1 + 1);
  if (c1 < 0 || c2 < 0) { pushLog(logBuf, "[朝向] json 解析失败"); journalEvent(EV_PJSON, "fail idx=%d parse", idx); return false; }
  outOrientation = body.substring(c1 + 1, c2);
  bool valid = (outOrientation == "portrait" || outOrientation == "landscape");
  if (valid) journalEvent(EV_PJSON, "idx=%d ori=%s", idx, outOrientation.c_str());
  else       journalEvent(EV_PJSON, "fail idx=%d bad_ori=%s", idx, outOrientation.c_str());
  pushLog(logBuf, String("[朝向] orientation=") + outOrientation);
  return valid;
}

// ============================================================
//  计算写入 framebuffer 时 Paint_NewImage 应用的旋转值
//
//  关键事实（已通过 PIC_display 源码确认）：PIC_display 把 framebuffer 字节
//  按固定顺序原样推给屏幕，完全无视 Paint 的 rotate。因此画面朝向只由
//  【写入时】Paint_NewImage 的 rotate 决定（它影响 Paint_SetPixel 的坐标映射），
//  在刷屏阶段再设 rotate 是无效的。所有写入点都必须用本函数选 rotate。
//
//  物理屏 800×480。服务端 .bin 字节序：
//    portrait  → 480×800 行优先    landscape → 800×480 行优先
//  旋转选择（基于已验证的竖屏 ROTATE_90 正常工作反推）：
//    portrait: 基础 ROTATE_90（把 480×800 转成竖向）；rotate180 → ROTATE_270
//    landscape: 基础 ROTATE_0（数据本就是物理朝向）；invert → ROTATE_180
// ============================================================
static uint16_t computePaintRotate(const Config &cfg, const String &orientation) {
  if (orientation == "landscape") {
    return cfg.landscape_invert ? ROTATE_180 : ROTATE_0;
  }
  return cfg.rotate180 ? ROTATE_270 : ROTATE_90;
}

// ============================================================
//  把字节流（HTTP stream 或内存 buffer）按朝向写入 Paint framebuffer
//    orientation="portrait"  → 逻辑画布 480×800（currentX∈[0,480)）
//    orientation="landscape" → 逻辑画布 800×480（currentX∈[0,800)）
//  字节数都是 384000，颜色编码 0/1/2/3 不变（1 字节/像素）
//
//  写入路径分两级（输出 framebuffer 逐字节一致）：
//    1. 逐像素查表路径：256 项 LUT 替代原 4 路 switch，再 Paint_SetPixel。
//       对任意 rotate 都正确，是保守回退路径。
//    2. 直写快速路径：仅当 paintRotate==ROTATE_0（逻辑坐标==物理坐标）时，
//       连续 4 个输入字节（同一 4 像素组）直接合成一个 framebuffer 字节写入。
//       等价性依据：
//         - ROTATE_0 下 Paint_SetPixel(x,y,c) 等于对 BlackImage[x/4 + y*200]
//           的 bit[6-2*(x%4)] 起写 2bit 色值（4 像素/字节、行内从高到低打包）。
//           该打包约定与 ap_bg.h 的解码（bit_shift=6-(x%4)*2）及 PIC_display
//           的消费顺序（每字节先取 bit7:6）一致，已在真机验证。
//         - 色值仍取与原 switch 相同的常量（经 LUT），不做任何重映射。
//         - fbW=800 为 4 的倍数 → 绝对像素序号 p%4 == x%4，4 像素组不跨行。
//       rotate 非零（竖屏 ROTATE_90/ROTATE_270、横屏翻转 ROTATE_180）时不启用。
//
//  返回码（供上层决定是否重试）：
//    0 = 成功
//    1 = 链路中断（stream 模式：available()==0 持续超过 stallMs 且未读够；可重试）
//    2 = 整体超时（超过 overallMs；可重试）
//    3 = 内存模式长度不足（不可重试）
//
//  参数：
//    expectedLen  服务端声明的 Content-Length（权威）；<=0 或超过画布尺寸则退回 fbW*fbH
//    stallMs      stream 模式下，available() 持续为空超过该值且未读够，判定链路中断
//    overallMs    stream 模式整体超时兜底
//    paintRotate  调用方传给 Paint_NewImage 的旋转值（决定是否启用直写路径）
//    BlackImage   framebuffer 指针（直写路径需要；与 Paint_SelectImage 绑定的是同一块）
// ============================================================
// 256 项查表：.bin 字节值 → Paint 颜色常量。
// 与原逐像素 switch 完全同语义：0→BLACK0, 1→WHITE0, 2→RED0, 3→YELLOW0，
// 其余（>3）→WHITE0（原 switch default 分支）。惰性初始化一次。
static const uint8_t* binColorLUT() {
  static uint8_t lut[256];
  static bool inited = false;
  if (!inited) {
    for (int i = 0; i < 256; ++i) lut[i] = WHITE0;
    lut[0] = BLACK0; lut[1] = WHITE0; lut[2] = RED0; lut[3] = YELLOW0;
    inited = true;
  }
  return lut;
}

// 逐像素路径：查表 + Paint_SetPixel + 游标推进（与旧实现逐字节等价）
static inline void paintPixelFromBin(const uint8_t *lut, uint8_t binVal,
                                     int &curX, int &curY, int fbW) {
  Paint_SetPixel(curX, curY, lut[binVal]);
  if (++curX >= fbW) { curX = 0; ++curY; }
}

static int streamToPaintHelper(WiFiClient *stream, const uint8_t *memBuf, size_t memLen,
                               const String &orientation, size_t expectedLen,
                               uint32_t stallMs, uint32_t overallMs,
                               uint16_t paintRotate, unsigned char* BlackImage,
                               String* logBuf) {
  int fbW = (orientation == "landscape") ? FB_HEIGHT : FB_WIDTH;   // 横屏逻辑宽=800, 竖屏=480
  int fbH = (orientation == "landscape") ? FB_WIDTH  : FB_HEIGHT;  // 横屏逻辑高=480, 竖屏=800
  size_t targetBytes = (size_t)fbW * fbH;   // 384000
  // 以服务端 Content-Length 为准；未声明或异常则退回画布尺寸
  size_t want = (expectedLen > 0 && expectedLen <= targetBytes) ? expectedLen : targetBytes;

  const uint8_t *lut = binColorLUT();

  // 直写快速路径启用条件（保守，见函数头注释）
  const int fbRowBytes = EPD_WIDTH / 4;   // framebuffer 每物理行字节数 = 200
  bool directWrite = (paintRotate == ROTATE_0) && (fbW == EPD_WIDTH) &&
                     (fbW % 4 == 0) && (BlackImage != nullptr);

  size_t totalRecv = 0;
  uint32_t start = millis();
  uint32_t lastRecv = millis();

  const size_t bufSize = 512;
  uint8_t buf[bufSize];

  int curX = 0, curY = 0;

  // HTTP 流模式
  if (stream) {
    // 每次进入（含重试）都清空 framebuffer 并重置游标，避免脏数据/像素错位
    Paint_Clear(WHITE0);
    while (totalRecv < want) {
      uint32_t now = millis();
      if (now - start > overallMs) { pushLog(logBuf, "[拉取] 整体超时"); return 2; }
      size_t avail = stream->available();
      if (avail) {
        size_t toRead = avail; if (toRead > bufSize) toRead = bufSize;
        if (toRead > want - totalRecv) toRead = want - totalRecv;
        int r = stream->read(buf, toRead);
        if (r > 0) {
          size_t i = 0;
          if (directWrite) {
            // 头部：补齐到 4 像素组边界（x%4==0），保证组不跨行
            while (i < (size_t)r && ((totalRecv + i) & 3) != 0) {
              paintPixelFromBin(lut, buf[i++], curX, curY, fbW);
            }
            // 4 像素一组直写：不越过本次读到的数据和 want（尾部不足一组走逐像素）
            while (i + 4 <= (size_t)r && totalRecv + i + 4 <= want) {
              size_t addr = (size_t)curX / 4 + (size_t)curY * fbRowBytes;
              BlackImage[addr] = (uint8_t)((lut[buf[i]] << 6) | (lut[buf[i + 1]] << 4) |
                                           (lut[buf[i + 2]] << 2) | lut[buf[i + 3]]);
              curX += 4;
              if (curX >= fbW) { curX = 0; ++curY; }
              i += 4;
            }
          }
          // 尾部（未对齐 / 不足一组 / 非直写模式）逐像素
          while (i < (size_t)r) {
            paintPixelFromBin(lut, buf[i++], curX, curY, fbW);
          }
          totalRecv += r;
          lastRecv = now;
        }
      } else {
        // available()==0：若持续超过 stallMs 且未读够，判定链路中断（可重试）
        if (now - lastRecv > stallMs) {
          pushLog(logBuf, String("[拉取] 链路中断 ") + (int)totalRecv + "/" + (int)want);
          return 1;
        }
        delay(1);
      }
    }
    pushLog(logBuf, String("[拉取] 入 framebuffer ") + (int)totalRecv + " bytes (" + fbW + "x" + fbH + ")");
    return 0;
  }
  // 内存 buffer 模式（标定卡：forcedCalibBin）
  if (memBuf && memLen > 0) {
    size_t cap = (memLen < want) ? memLen : want;
    size_t i = 0;
    if (directWrite) {
      while (i < cap && (i & 3) != 0) {
        paintPixelFromBin(lut, memBuf[i++], curX, curY, fbW);
      }
      while (i + 4 <= cap) {
        size_t addr = (size_t)curX / 4 + (size_t)curY * fbRowBytes;
        BlackImage[addr] = (uint8_t)((lut[memBuf[i]] << 6) | (lut[memBuf[i + 1]] << 4) |
                                     (lut[memBuf[i + 2]] << 2) | lut[memBuf[i + 3]]);
        curX += 4;
        if (curX >= fbW) { curX = 0; ++curY; }
        i += 4;
      }
    }
    while (i < cap) {
      paintPixelFromBin(lut, memBuf[i++], curX, curY, fbW);
    }
    totalRecv = cap;
    if (totalRecv != want) {
      pushLog(logBuf, String("[拉取] 标定卡长度不足 ") + (int)totalRecv + "/" + (int)want);
      return 3;
    }
    pushLog(logBuf, String("[拉取] 入 framebuffer ") + (int)totalRecv + " bytes (" + fbW + "x" + fbH + ")");
    return 0;
  }
  // 既无 stream 也无 memBuf
  pushLog(logBuf, "[拉取] 无数据源");
  return 1;
}

// ============================================================
//  公共重试循环：HTTP GET → streamToPaintHelper 写 framebuffer
//
//  fetchPhotoBinToFramebuffer 与 fetchCalibCardToFramebuffer 的外层重试
//  逻辑逐字相同（仅 URL、日志前缀、HTTP 超时不同），抽成公共函数避免漂移。
//  每次重试重建连接、清空 framebuffer 重写；最多 MAX_RETRY 次。
//
//    tag         日志前缀（"[拉取] " / "[标定] "）
//    attemptCtx  首行日志的附加上下文（如 "idx=0 "，可为空串）
//  返回 true=成功。
// ============================================================
static bool fetchBinStreamWithRetry(const String &url, const char *tag, const String &attemptCtx,
                                    uint32_t httpTimeoutMs, const String &orientation,
                                    uint16_t paintRotate, unsigned char* BlackImage,
                                    String* logBuf) {
  const int MAX_RETRY = 3;
  for (int attempt = 1; attempt <= MAX_RETRY; ++attempt) {
    pushLog(logBuf, String(tag) + attemptCtx + "尝试 " + attempt + "/" + MAX_RETRY + " | GET " + url);

    HTTPClient http;
    http.begin(url);
    http.setTimeout(httpTimeoutMs);
    int code = http.GET();
    if (code != HTTP_CODE_OK) {
      pushLog(logBuf, String(tag) + "HTTP 失败 code=" + code);
      http.end();
      if (attempt < MAX_RETRY) { delay(500); continue; }
      return false;
    }

    size_t contentLen = (size_t)http.getSize();   // Content-Length（0 表示服务端未声明）
    WiFiClient *stream = http.getStreamPtr();
    int rc = streamToPaintHelper(stream, nullptr, 0, orientation, contentLen,
                                 5000 /*stallMs*/, 60000 /*overallMs*/,
                                 paintRotate, BlackImage, logBuf);
    http.end();

    if (rc == 0) return true;
    // rc==1 链路中断、rc==2 整体超时 → 均可重建连接重试
    if (attempt < MAX_RETRY) {
      pushLog(logBuf, String(tag) + "等待 500ms 后重试…");
      delay(500);
      continue;
    }
    pushLog(logBuf, String(tag) + "已达最大重试次数，放弃");
    return false;
  }
  return false;
}

// ============================================================
//  fetch photo_N.bin 到 Paint framebuffer
//    成功返回 true。Paint 已绑定到 BlackImage，调用方负责后续刷屏。
// ============================================================
static bool fetchPhotoBinToFramebuffer(const Config &cfg, int idx, const String &orientation,
                                       unsigned char* BlackImage, String* logBuf) {
  String url = buildPhotoUrl(cfg, String(idx) + ".bin");
  DBG_PRINTF("[HTTP] GET %s\n", url.c_str());

  // WiFi 偶发抖断会导致 384KB 流式读取中途断开（少几百字节），
  // 重试细节见 fetchBinStreamWithRetry（每次重建连接、清空 framebuffer 重写）。
  uint32_t t0 = millis();
  bool ok = fetchBinStreamWithRetry(url, "[拉取] ", "idx=" + String(idx) + " ",
                                    30000 /*httpTimeoutMs*/, orientation,
                                    computePaintRotate(cfg, orientation), BlackImage, logBuf);
  journalEvent(EV_PBIN, ok ? "ok idx=%d ori=%s ms=%u" : "fail idx=%d ori=%s ms=%u",
               idx, orientation.c_str(), (unsigned)(millis() - t0));
  return ok;
}

// ============================================================
//  根据朝向选 Paint 旋转方向并刷屏
// ============================================================
static void displayFramebuffer(unsigned char* BlackImage, const Config &cfg, const String &orientation) {
  (void)cfg; (void)orientation;   // 画面朝向已在写入阶段（Paint_NewImage）定型，刷屏无需再处理
  if (!g_epdPresent) {
    DBG_PRINTLN("[EPD] 无屏，跳过刷屏");
    journalEvent(EV_RENDER, "no_epd");
    return;
  }
  // 注意：PIC_display 把 framebuffer 字节原样推给屏幕，不读 Paint 的 rotate/状态。
  // 画面朝向（含 rot180/landscape_invert 翻转）已在写入 framebuffer 时由
  // computePaintRotate 选定的 Paint_NewImage 旋转决定，此处无需、也无法再改朝向。
  Paint_SelectImage(BlackImage);

  DBG_PRINTLN("[EPD] 初始化并开始显示画面");
  EPD_init();
  PIC_display(BlackImage);
  EPD_DeepSleep();
  DBG_PRINTLN("[EPD] 渲染完成，屏幕已休眠");
  journalEvent(EV_RENDER, "ok");
}

// ============================================================
//  舵机决策：根据朝向决定是否转、转到哪
//    规则（来自设计 grilling）：
//    a. !servo_calibrated  → 跳过（不挡渲染）
//    b. last_orientation 空 → 首次归位：转 portrait_deg，写 last_orientation=portrait
//    c. orientation == last_orientation → 跳过（省机械磨损）
//    d. 不同 → 转对应角度（3s 超时 detach 继续），成功后写 last_orientation
// ============================================================
static void applyServoForOrientation(const Config &cfg, const String &orientation, String* logBuf) {
  if (!cfg.servo_calibrated) {
    pushLog(logBuf, "[舵机] 未标定，跳过转动");
    journalEvent(EV_SUNCAL);
    return;
  }
  // 首次开机归位（last_orientation 为空）
  if (cfg.last_orientation.length() == 0) {
    pushLog(logBuf, "[舵机] 首次开机，归位竖屏 home");
    journalEvent(EV_SCMD, "home target=%.1f speed=%.1f", cfg.servo_portrait_deg, cfg.servo_speed);
    bool ok = servo_rotate_to(cfg.servo_portrait_deg, cfg.servo_speed, 3000);
    journalEvent(EV_SDONE, ok ? "ok home" : "timeout home");
    saveLastOrientation("portrait");
    return;
  }
  // 同朝向：只有舵机位置可信（本次开机后转过至少一次）才允许跳过。
  //   开机后 servo_init 会把舵机打到 0°（首帧 PWM 必须有确定脉宽），位置不可信；
  //   此时若跳过，相框会物理停在机械零点而屏幕仍显示原朝向——
  //   "舵机突然归零、显示没变"事故的根因（见设备日志 SERVO_SKIP ori=… last=…）。
  if (orientation == cfg.last_orientation && servo_position_known()) {
    pushLog(logBuf, String("[舵机] 同朝向（") + orientation + "），跳过转动");
    journalEvent(EV_SSKIP, "ori=%s last=%s", orientation.c_str(), cfg.last_orientation.c_str());
    return;
  }
  if (orientation == cfg.last_orientation) {
    // 开机恢复：从 0° 平滑转回当前朝向的标定角度（位置恢复可信后，后续照常跳过）
    float target = (orientation == "landscape") ? cfg.servo_landscape_deg : cfg.servo_portrait_deg;
    pushLog(logBuf, String("[舵机] 开机恢复 → ") + orientation + " (" + target + "°)");
    journalEvent(EV_SCMD, "restore ori=%s target=%.1f speed=%.1f", orientation.c_str(), target, cfg.servo_speed);
    bool ok = servo_rotate_to(target, cfg.servo_speed, 3000);
    journalEvent(EV_SDONE, ok ? "ok restore ori=%s" : "timeout restore ori=%s", orientation.c_str());
    pushLog(logBuf, ok ? "[舵机] 恢复到位 ✅" : "[舵机] 恢复超时（已 detach 继续）");
    return;
  }
  // 不同朝向，转
  float target = (orientation == "landscape") ? cfg.servo_landscape_deg : cfg.servo_portrait_deg;
  pushLog(logBuf, String("[舵机] 转 → ") + orientation + " (" + target + "°)");
  journalEvent(EV_SCMD, "to=%s target=%.1f speed=%.1f", orientation.c_str(), target, cfg.servo_speed);
  bool ok = servo_rotate_to(target, cfg.servo_speed, 3000);
  journalEvent(EV_SDONE, ok ? "ok ori=%s" : "timeout ori=%s", orientation.c_str());
  pushLog(logBuf, ok ? "[舵机] 到位 ✅" : "[舵机] 超时（已 detach 继续）");
  if (ok) saveLastOrientation(orientation);
}

// ============================================================
//  主流程：fetch json → fetch bin 到 framebuffer → 转舵机 → 刷屏
//    forcedIdx<0       → idx=0（正常每日）
//    forcedCalibBin!=0 → 跳过 bin 下载，直接渲染标定卡（标定台用，forcedCalibOri 指定朝向）
// ============================================================
bool downloadAndRenderDailyPhoto(const Config &cfg, int forcedIdx, String* logBuf,
                                 const char* forcedCalibBin, const char* forcedCalibOri) {
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

  // 标定卡模式：forcedCalibBin 是内存中的字节流指针 + forcedCalibOri 朝向
  if (forcedCalibBin != nullptr && forcedCalibOri != nullptr) {
    String ori = String(forcedCalibOri);
    pushLog(logBuf, String("[标定] 渲染标定卡 (") + ori + ")");

    // 写入时的 Paint 旋转决定画面朝向（含 rot180/landscape_invert 翻转开关）
    uint16_t paintRotate = computePaintRotate(cfg, ori);
    Paint_NewImage(BlackImage, EPD_WIDTH, EPD_HEIGHT, paintRotate, WHITE0);
    Paint_SetScale(4);
    Paint_SelectImage(BlackImage);
    Paint_Clear(WHITE0);

    // 标定卡字节数：竖屏 480*800=384000，横屏 800*480=384000（一致）
    // 内存模式：expectedLen=384000，stall/overall 对内存模式无意义，传 0
    int rc = streamToPaintHelper(nullptr, (const uint8_t*)forcedCalibBin, 384000, ori,
                                 384000, 0, 0, paintRotate, BlackImage, logBuf);
    if (rc != 0) {
      heap_caps_free(BlackImage);
      return false;
    }
    // 标定模式下舵机转动由调用方（标定台）按"表单当前值"处理，这里不重复转
    displayFramebuffer(BlackImage, cfg, ori);
    heap_caps_free(BlackImage);
    pushLog(logBuf, "[标定] 完成 ✅");
    return true;
  }

  // 正常流程：json → bin → 转舵机 → 刷屏
  if (cfg.backend_hostport.length() == 0) {
    pushLog(logBuf, "[拉取] 服务器地址为空，跳过");
    heap_caps_free(BlackImage);
    return false;
  }

  int idx = (forcedIdx >= 0 && forcedIdx < DAILY_PHOTO_COUNT) ? forcedIdx : 0;

  // ── 阶段 A1：fetch sidecar json 拿朝向（失败降级用 last_orientation，仍尝试下 bin）──
  String orientation;
  bool gotOri = fetchPhotoOrientation(cfg, idx, orientation, logBuf);
  if (!gotOri) {
    orientation = cfg.last_orientation.length() > 0 ? cfg.last_orientation : String("portrait");
    pushLog(logBuf, String("[朝向] 降级为 ") + orientation);
  }

  // ── 阶段 A2：fetch bin 到 framebuffer（失败 → 不转舵机、不刷屏、保留昨天姿态）──
  // 写入时的 Paint 旋转决定画面朝向（含 rot180/landscape_invert 翻转开关）。
  {
    uint16_t paintRotate = computePaintRotate(cfg, orientation);
    Paint_NewImage(BlackImage, EPD_WIDTH, EPD_HEIGHT, paintRotate, WHITE0);
  }
  Paint_SetScale(4);
  Paint_SelectImage(BlackImage);
  Paint_Clear(WHITE0);

  if (!fetchPhotoBinToFramebuffer(cfg, idx, orientation, BlackImage, logBuf)) {
    pushLog(logBuf, "[拉取] bin 下载失败，不转舵机不刷屏，保留昨天姿态");
    heap_caps_free(BlackImage);
    return false;
  }

  // ── 阶段 B：转舵机（下载成功后才转）──
  applyServoForOrientation(cfg, orientation, logBuf);

  // ── 阶段 C：刷屏 ──
  displayFramebuffer(BlackImage, cfg, orientation);

  heap_caps_free(BlackImage);
  pushLog(logBuf, "[拉取] 完成 ✅");
  return true;
}

// ============================================================
//  fetch 标定卡流式直写 framebuffer（ESP32-L 无 PSRAM 专用）
//    calibName  = "calib_p.bin" 或 "calib_l.bin"
//    BlackImage = 调用方已分配的 EPD framebuffer（EPD_WIDTH*EPD_HEIGHT/4 字节）
//
//  与照片 .bin 完全同构：384000 字节、1byte/像素、颜色编码 0/1/2/3。
//  因此复用照片下载的全套健壮读取逻辑：
//    HTTPClient + streamToPaintHelper 流模式（含 stall/整体超时、重试），
//    重试循环抽在公共的 fetchBinStreamWithRetry 中，
//    直接把 HTTP 流写进 96KB framebuffer。
//
//  不经过 384KB 中转 buffer——那在 ESP32-L 无 PSRAM 的设备上
//  heap_caps_malloc(384000) 必然失败。
//
//  返回 true=成功（已写入 framebuffer，调用方负责刷屏）；false=失败。
// ============================================================
static bool fetchCalibCardToFramebuffer(const Config &cfg, const String &calibName,
                                        const String &orientation,
                                        unsigned char* BlackImage, String* logBuf) {
  // URL 构造：DAILY_PHOTO_PATH_PREFIX 去掉末尾 "photo_" 得到 calib 基路径
  String base = pathPrefixBase();   // → "/static/inktime/<key>/"
  String hp = cfg.backend_hostport;
  hp.trim();
  String url = (hp.startsWith("http") ? hp : ("http://" + hp)) + base + calibName;

  DBG_PRINTF("[HTTP] GET %s\n", url.c_str());

  // 写入时的 Paint 旋转决定画面朝向（PIC_display 原样发送，刷屏阶段再设 rotate 无效）。
  // computePaintRotate 综合 orientation + cfg.rotate180/landscape_invert 给出最终旋转。
  uint16_t paintRotate = computePaintRotate(cfg, orientation);
  Paint_NewImage(BlackImage, EPD_WIDTH, EPD_HEIGHT, paintRotate, WHITE0);
  Paint_SetScale(4);
  Paint_SelectImage(BlackImage);
  Paint_Clear(WHITE0);

  return fetchBinStreamWithRetry(url, "[标定] ", "",
                                 15000 /*httpTimeoutMs*/, orientation,
                                 paintRotate, BlackImage, logBuf);
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
  if (!g_epdPresent) {
    DBG_PRINTLN("[EPD] 无屏，跳过网络信息页面");
    return;
  }
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
  //    ap_bg_data 本就是 2bpp 打包格式（480×800 逻辑画布，每字节 4 像素、
  //    行内从高到低排列，即 bit_shift=6-(x%4)*2，与原逐像素解码一致）。
  //    注意：本屏 rotate 为 ROTATE_90/ROTATE_270（竖屏），逻辑行映射为物理列，
  //    不能按行 memcpy 直写 framebuffer（会把背景整体旋转 90°），仍逐像素
  //    Paint_SetPixel；仅把每像素的 byte_idx 乘加/移位计算降为每字节一次，
  //    写入序列（坐标与颜色值）与原实现完全一致。
  for (int y = 0; y < FB_HEIGHT; ++y) {
    const uint8_t* row = &ap_bg_data[(size_t)y * (FB_WIDTH / 4)];
    for (int gx = 0; gx < FB_WIDTH / 4; ++gx) {
      uint8_t packed = row[gx];
      int x = gx * 4;
      Paint_SetPixel(x + 0, y, (packed >> 6) & 0x03);
      Paint_SetPixel(x + 1, y, (packed >> 4) & 0x03);
      Paint_SetPixel(x + 2, y, (packed >> 2) & 0x03);
      Paint_SetPixel(x + 3, y, packed & 0x03);
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
//  EPD 屏幕存在检测（开机一次性探测）
//
//  原理：真屏在 RST 下降沿后会主动把 BUSY 拉低（忙），几十~几百 ms 后释放回高（idle）。
//        无屏时 RST 操作对 BUSY 引脚无任何驱动效果——BUSY 只被外部上拉决定。
//  判据（对上拉免疫）：RST 脉冲后 300ms 内采样多次，若 BUSY 曾经出现低电平 → 有屏；
//                       若全程高电平 → 无屏。
//  这避免"加 INPUT_PULLUP 后无屏也读 1=idle"的误判：我们检测的是"面板有没有驱动过 BUSY"，
//  而不是 BUSY 的静止电平。
// ============================================================
static bool detectEpdPresent() {
  // BUSY 引脚上拉：真屏会强驱动它，无屏时被上拉到 1（idle）。上拉本身不会产生误判，
  // 因为下面的判据看的是"RST 后 BUSY 有没有被拉低过"。
  pinMode(PIN_EPD_BUSY, INPUT_PULLUP);
  // RST/DC/CS 已在 setup() 主流程里配成 OUTPUT（detectEpdPresent 在 SPI 初始化之后调用）
  delay(10);

  // 记录 RST 前的 BUSY 静止电平（参考用）
  int idleBefore = digitalRead(PIN_EPD_BUSY);

  // 发一个 RST 脉冲（与 EPD_init 一致：拉低 ≥10ms 再拉高）
  digitalWrite(PIN_EPD_RST, LOW);
  delay(15);
  digitalWrite(PIN_EPD_RST, HIGH);

  // 采样 300ms，看 BUSY 是否被面板拉低过
  bool sawLow = false;
  uint32_t start = millis();
  while (millis() - start < 300) {
    if (digitalRead(PIN_EPD_BUSY) == LOW) { sawLow = true; break; }
    delay(5);
  }

  bool present = sawLow;
  DBG_PRINTF("[EPD] detectEpdPresent: BUSY idleBefore=%d, sawLow=%d → present=%d\n",
             idleBefore, (int)sawLow, (int)present);
  return present;
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

  // 3.5 设备日志（黑盒）：先于一切挂载 FS、开机序号+1、记录上次复位原因+本次唤醒原因。
  //     即使后面崩溃/掉电，这条也已落盘。设计见 docs/adr/0001-esp32-flash-device-journal.md
  journalBegin();

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

  // 6.1 注入设备日志的上传目标（hostport + 照片 key 作设备标识）
  {
    String prefix = String(DAILY_PHOTO_PATH_PREFIX);            // "/static/inktime/<key>/photo_"
    String base   = prefix.substring(0, prefix.length() - 6);   // "/static/inktime/<key>/"
    base.remove(base.length() - 1);                             // 去掉末尾 '/' → "/static/inktime/<key>"
    journalSetServer(g_cfg.backend_hostport, base.substring(base.lastIndexOf('/') + 1));  // → "<key>"
  }

  // 6.5 初始化墨水屏 SPI 引脚 (必须在 showNetworkInfoScreen 之前调用)
  pinMode(PIN_EPD_BUSY, INPUT_PULLUP);  // BUSY（上拉：无屏时读 idle，配合 detectEpdPresent 判据）
  pinMode(PIN_EPD_RST, OUTPUT);  // RES
  pinMode(PIN_EPD_DC, OUTPUT);   // DC
  pinMode(PIN_EPD_CS, OUTPUT);   // CS
  SPI.beginTransaction(SPISettings(10000000, MSBFIRST, SPI_MODE0));
  // 注意：SPI.begin() 不传 ss 参数！PIN_EPD_CS(27) 由 EPD 驱动手动控制
  // （EPD_W21_CS_0/1 宏）。若把 27 作为 ss 传给 SPI.begin，硬件会抢着管理
  // 片选，与驱动冲突，导致 EPD_init 卡死在 BUSY 等待。
  SPI.begin();

  // 6.55 探测屏幕是否接入（必须在任何 EPD_init 之前；无屏时设 g_epdPresent=false
  //      让后续 showNetworkInfoScreen/displayFramebuffer 静默跳过，避免 lcd_chkstatus 卡 30s）
  g_epdPresent = detectEpdPresent();

  // 6.6 初始化舵机（IO32，与 EPD SPI 引脚无冲突，避开 DAC 脚 25/26）
  //     未标定时 servo_rotate_to 不会被调用，但 attach 本身开销极小，先就位。
  //     注意：不再立即 servo_detach()——舵机需保持 attach 才平顺（参考 servo_serial_cmd）。
  //     深睡时由 goDeepSleepMinutes 统一 detach 省电。
  servo_init();
  servo_set_default_speed(g_cfg.servo_speed);   // 把 NVS 里的速度注入舵机模块（全局共享）

  // 7. 无有效配置 → 进入 AP 配网
  if (!g_cfg.valid) {
    DBG_PRINTLN("[BOOT] 无有效配置 → AP 配网模式");
    showNetworkInfoScreen(true, false);
    startConfigPortal();
    // startConfigPortal() 不返回
  }

  // 8. 连接 WiFi（最多 3 次尝试，线性退避；全失败才进 AP 配网门户）
  DBG_PRINTLN("[BOOT] 尝试连接 WiFi...");
  bool wifiOk = false;
  for (int attempt = 1; attempt <= 3; ++attempt) {
    if (connectWiFi(g_cfg)) { wifiOk = true; break; }
    if (attempt < 3) {
      DBG_PRINTF("[BOOT] WiFi 第 %d 次尝试失败，退避 %ds 后重试...\n", attempt, attempt);
      delay(1000 * attempt);   // 退避 1s / 2s，等路由器/ DHCP 恢复
    }
  }
  if (!wifiOk) {
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

  // 9.5 上传设备日志增量（拉照片之前先传）。
  //     入睡前那次上传（goDeepSleepMinutes）只覆盖常驻 server 场景；若 server
  //     是"检测到拉取即关闭"的临时模式，睡前上传必然扑空。两处都传，靠服务端
  //     按 ls 去重，重复无害。本周期的新事件（照片/舵机/刷屏）下次唤醒补传。
  journalUpload();

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
    server.on("/",          HTTP_GET,  handleRoot);
    server.on("/network",   HTTP_GET,  handleNetwork);
    server.on("/save",      HTTP_POST, handleSave);
    server.on("/fetch",     HTTP_GET,  handleFetch);
    server.on("/log",       HTTP_GET,  handleLog);
    server.on("/debug",     HTTP_GET,  handleDebug);
    server.on("/screen",    HTTP_GET,  handleScreen);
    server.on("/test",      HTTP_GET,  handleScreen);   // 向后兼容别名
    server.on("/servo_test",HTTP_GET,  handleServoTest);
    server.on("/servo",     HTTP_GET,  handleServo);
    server.on("/servo_save", HTTP_POST, handleServoSave);
    server.on("/servo_sync", HTTP_GET,  handleServoSync);
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
    // 二次重试：只再调局域网服务器授时（不再全量重跑 syncTime 的 NTP 流程）
    struct tm tmp;
    if (fetchServerTime(g_cfg, tmp)) {
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
