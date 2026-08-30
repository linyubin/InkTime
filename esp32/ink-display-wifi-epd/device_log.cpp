/*
 * device_log.cpp — 设备日志（黑盒）实现
 *
 * 存储模型（双段滚动，避免大内存重写文件）：
 *   /devlog_a.log + /devlog_b.log 各上限 32KB；写满当前段就删掉另一段、
 *   切到空段继续写 → 本地最多保留 ~64KB（约 500 条），最旧的先消失。
 *
 * 行格式（JSONL，一行一条）：
 *   {"ls":行号,"b":开机序号,"ms":相对毫秒,"t":"类型","d":"明细"}
 *   ls 全局递增，从文件末行恢复；绝对时间由服务器用 TIME_SYNC 锚点换算。
 *
 * 游标语义：NVS 存"已上传到的 ls"；上传 = 发送所有 ls > 游标的行，
 *   收到 200 才推进。断网期间事件留在 Flash 里，下次唤醒重传。
 */
#include "device_log.h"
#include <LittleFS.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <WiFi.h>
#include <stdarg.h>
#include "esp_system.h"
#include "esp_sleep.h"

// ── 可调参数 ──
static const size_t  SEG_MAX_BYTES = 32UL * 1024UL;   // 单段上限（双段 → 本地 ~64KB）
static const size_t  UPLOAD_CHUNK  = 6UL * 1024UL;    // 单次 POST 体上限（内存友好）
static const int     UPLOAD_MAX_ROUNDS = 8;           // 单次上传最多批数（防御性上限）
static const char*   NVS_NS  = "devlog";
static const char*   FILE_A  = "/devlog_a.log";
static const char*   FILE_B  = "/devlog_b.log";

static bool     s_mounted = false;
static bool     s_activeA = true;
static uint32_t s_bootSeq = 0;
static uint32_t s_lineSeq = 0;    // 全局行号，跨深睡从文件恢复
static uint32_t s_cursor  = 0;    // 已上传到的行号（NVS 持久化）
static String   s_hostport;
static String   s_key;
static String   s_lastUp = "never";   // 最近一次上传尝试的结果（诊断页/串口可见）

// 模块自诊断输出（Serial 已由 .ino 的 DBG_BEGIN() 初始化）
#define JLOG(...) Serial.printf(__VA_ARGS__)

static const char* activeFile() { return s_activeA ? FILE_A : FILE_B; }
static const char* otherFile()  { return s_activeA ? FILE_B : FILE_A; }

// ── 复位原因 / 唤醒原因 → 可读名（枚举名取两代 core 都有的稳定子集）──
static void resetReasonName(char* buf, size_t n) {
  switch (esp_reset_reason()) {
    case ESP_RST_POWERON:    snprintf(buf, n, "POWERON");    break;
    case ESP_RST_SW:         snprintf(buf, n, "SW_REBOOT");  break;
    case ESP_RST_PANIC:      snprintf(buf, n, "PANIC");      break;
    case ESP_RST_INT_WDT:    snprintf(buf, n, "INT_WDT");    break;
    case ESP_RST_TASK_WDT:   snprintf(buf, n, "TASK_WDT");   break;
    case ESP_RST_WDT:        snprintf(buf, n, "OTHER_WDT");  break;
    case ESP_RST_DEEPSLEEP:  snprintf(buf, n, "DEEPSLEEP_WAKE"); break;
    case ESP_RST_BROWNOUT:   snprintf(buf, n, "BROWNOUT");   break;
    case ESP_RST_SDIO:       snprintf(buf, n, "SDIO");       break;
    default:                 snprintf(buf, n, "RST_%d", (int)esp_reset_reason()); break;
  }
}

static const char* wakeCauseName(esp_sleep_wakeup_cause_t c) {
  switch (c) {
    case ESP_SLEEP_WAKEUP_TIMER:     return "TIMER";
    case ESP_SLEEP_WAKEUP_UNDEFINED: return "POWERON";   // 非深睡唤醒 = 上电/复位键
    case ESP_SLEEP_WAKEUP_EXT0:      return "EXT0";
    case ESP_SLEEP_WAKEUP_EXT1:      return "EXT1";
    case ESP_SLEEP_WAKEUP_TOUCHPAD:  return "TOUCH";
    case ESP_SLEEP_WAKEUP_ULP:       return "ULP";
    default:                         return "WAKE_UNKNOWN";
  }
}

// ── JSON 辅助 ──
// detail 里的控制字符压成空格（保证 JSONL 一行一条），引号/反斜杠转义
static String jsonEscape(const char* s) {
  String out;
  for (const char* p = s; *p; ++p) {
    char c = *p;
    if (c == '"' || c == '\\') { out += '\\'; out += c; }
    else if ((uint8_t)c < 0x20) { out += ' '; }
    else { out += c; }
  }
  return out;
}

static uint32_t extractLs(const String& line) {
  int p = line.indexOf("\"ls\":");
  if (p < 0) return 0;
  return (uint32_t)line.substring(p + 5).toInt();
}

// ── 文件辅助 ──
static String readLastLine(const char* path) {
  File f = LittleFS.open(path, FILE_READ);
  if (!f) return "";
  size_t sz = f.size();
  if (sz == 0) { f.close(); return ""; }
  const size_t chunk = 256;
  f.seek(sz > chunk ? sz - chunk : 0);
  String tail = f.readString();
  f.close();
  tail.trim();
  int nl = tail.lastIndexOf('\n');
  if (nl >= 0) tail = tail.substring(nl + 1);
  return tail;
}

static uint32_t lastLs(const char* path)  { return extractLs(readLastLine(path)); }

static uint32_t firstLs(const char* path) {
  File f = LittleFS.open(path, FILE_READ);
  if (!f) return 0;
  String line = f.readStringUntil('\n');
  f.close();
  return extractLs(line);
}

static void appendLine(const String& line) {
  File f = LittleFS.open(activeFile(), FILE_APPEND);
  if (!f) return;
  f.println(line);
  f.close();

  File st = LittleFS.open(activeFile(), FILE_READ);
  size_t sz = st ? st.size() : 0;
  if (st) st.close();
  if (sz >= SEG_MAX_BYTES) {
    LittleFS.remove(otherFile());   // 双段滚动：删掉旧段，切到空段
    s_activeA = !s_activeA;
  }
}

// ── 公开接口 ──
void journalBegin() {
  s_mounted = LittleFS.begin(true);   // 首次烧录后 FS 分区为空，自动格式化（NVS 不受影响）
  if (!s_mounted) {
    JLOG("[DEVLOG] LittleFS 挂载失败，设备日志停用！\n");
    return;
  }
  JLOG("[DEVLOG] LittleFS 挂载 ok（已用 %u/%u KB）\n",
       (unsigned)(LittleFS.usedBytes() / 1024), (unsigned)(LittleFS.totalBytes() / 1024));

  Preferences p;
  p.begin(NVS_NS, false);
  s_bootSeq = p.getUInt("boot", 0) + 1;
  s_cursor  = p.getUInt("cur", 0);
  p.putUInt("boot", s_bootSeq);
  p.end();

  // 行号从文件末行恢复；若 FS 被格式化过而 NVS 游标还在，行号续到游标之上，
  // 否则新事件的 ls ≤ 游标会被上传过滤器永久跳过
  s_lineSeq = lastLs(FILE_A);
  uint32_t lsB = lastLs(FILE_B);
  if (lsB > s_lineSeq) s_lineSeq = lsB;
  if (s_lineSeq < s_cursor) s_lineSeq = s_cursor;

  JLOG("[DEVLOG] boot=%u 行号=%u 游标=%u\n", (unsigned)s_bootSeq, (unsigned)s_lineSeq, (unsigned)s_cursor);

  char d[110];
  char rst[16];
  resetReasonName(rst, sizeof(rst));
  snprintf(d, sizeof(d), "reset=%s wake=%s heap=%u",
           rst, wakeCauseName(esp_sleep_get_wakeup_cause()), (unsigned)ESP.getFreeHeap());
  journalEvent(EV_BOOT, "%s", d);
}

void journalSetServer(const String& hostport, const String& key) {
  s_hostport = hostport;
  s_hostport.trim();
  s_key = key;
  s_key.trim();
}

void journalEvent(const char* type, const char* fmt, ...) {
  if (!s_mounted) return;

  char detail[160];
  detail[0] = '\0';
  if (fmt && fmt[0]) {
    va_list args;
    va_start(args, fmt);
    vsnprintf(detail, sizeof(detail), fmt, args);
    va_end(args);
  }

  ++s_lineSeq;
  String line = "{\"ls\":";
  line += s_lineSeq;
  line += ",\"b\":";
  line += s_bootSeq;
  line += ",\"ms\":";
  line += (uint32_t)millis();
  line += ",\"t\":\"";
  line += type;
  line += "\"";
  if (detail[0]) { line += ",\"d\":\""; line += jsonEscape(detail); line += "\""; }
  line += "}";

  appendLine(line);
}

void journalUpload() {
  if (!s_mounted) { s_lastUp = "skip: FS 未挂载"; return; }
  if (s_hostport.length() == 0) { s_lastUp = "skip: hostport 为空"; return; }
  if (s_key.length() == 0)      { s_lastUp = "skip: key 为空"; return; }
  if (WiFi.status() != WL_CONNECTED) { s_lastUp = "skip: WiFi 未连接"; return; }

  String url = s_hostport;
  if (!url.startsWith("http")) url = "http://" + url;
  url.trim();
  url += "/api/device_log/" + s_key;

  JLOG("[DEVLOG] 上传开始 cur=%u -> %s\n", (unsigned)s_cursor, url.c_str());

  // 两段按首行行号升序（小的是旧段），保证服务器收到的顺序 = 发生顺序
  const char* order[2];
  if (firstLs(FILE_A) <= firstLs(FILE_B)) { order[0] = FILE_A; order[1] = FILE_B; }
  else                                    { order[0] = FILE_B; order[1] = FILE_A; }

  uint32_t sentTotal = 0;
  for (int round = 0; round < UPLOAD_MAX_ROUNDS; ++round) {
    String payload;
    uint32_t maxLs = 0, count = 0;
    for (int i = 0; i < 2 && payload.length() < UPLOAD_CHUNK; ++i) {
      File f = LittleFS.open(order[i], FILE_READ);
      if (!f) continue;
      while (f.available() && payload.length() < UPLOAD_CHUNK) {
        String line = f.readStringUntil('\n');
        line.trim();
        if (!line.length()) continue;
        uint32_t ls = extractLs(line);
        if (ls == 0 || ls <= s_cursor) continue;   // 游标之后的才是增量
        payload += line;
        payload += '\n';
        if (ls > maxLs) maxLs = ls;
        ++count;
      }
      f.close();
    }
    if (count == 0) break;

    JLOG("[DEVLOG] 批次 n=%u %uB\n", (unsigned)count, (unsigned)payload.length());
    HTTPClient http;
    http.begin(url);
    http.setTimeout(8000);
    http.addHeader("Content-Type", "application/x-ndjson");
    int code = http.POST(payload);
    http.end();
    JLOG("[DEVLOG] 上传响应 code=%d\n", code);

    if (code != 200) {
      s_lastUp = String("fail code=") + code;
      journalEvent(EV_UPLOAD, "fail code=%d sent=%u", code, (unsigned)sentTotal);
      return;   // 失败保留游标，下次唤醒重传
    }
    s_cursor = maxLs;
    Preferences p;
    p.begin(NVS_NS, false);
    p.putUInt("cur", s_cursor);
    p.end();
    sentTotal += count;
  }

  if (sentTotal > 0) {
    s_lastUp = String("ok n=") + sentTotal + " cur=" + s_cursor;
    journalEvent(EV_UPLOAD, "ok n=%u cur=%u", (unsigned)sentTotal, (unsigned)s_cursor);
  } else {
    s_lastUp = "ok n=0（无增量）";
  }
}

// 诊断状态行（.ino 的 /log 页会展示）：挂载/行号/游标/两段占用/最近上传结果
String journalStatus() {
  String st = "mounted=" + String(s_mounted ? 1 : 0);
  st += " boot=" + String(s_bootSeq);
  st += " ls=" + String(s_lineSeq);
  st += " cur=" + String(s_cursor);
  if (s_mounted) {
    File fa = LittleFS.open(FILE_A, FILE_READ);
    size_t sa = fa ? fa.size() : 0; if (fa) fa.close();
    File fb = LittleFS.open(FILE_B, FILE_READ);
    size_t sb = fb ? fb.size() : 0; if (fb) fb.close();
    st += " segA=" + String((unsigned)sa) + "B";
    st += " segB=" + String((unsigned)sb) + "B";
  }
  st += " lastUp=" + s_lastUp;
  return st;
}

uint32_t journalBootSeq() { return s_bootSeq; }
