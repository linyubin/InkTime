/*
 * config.cpp — NVS(Preferences) 持久化实现
 *
 * Preferences 是 Arduino-ESP32 自带库（基于 ESP-IDF 的 NVS），无需额外安装。
 * 把整个 Config 结构按字段存，方便以后加字段。
 */
#include "config.h"
#include <Preferences.h>

Config g_cfg;          // 全局配置单例

static const char* NVS_NAMESPACE = "servo";

void configLoad() {
  Preferences prefs;
  prefs.begin(NVS_NAMESPACE, true);   // 只读

  // 用 get(key, default)：key 不存在时返回默认值，所以首次开机会用默认值
  g_cfg.range    = prefs.getFloat ("range",    g_cfg.range);
  g_cfg.offset   = prefs.getFloat ("offset",   g_cfg.offset);
  g_cfg.target   = prefs.getFloat ("target",   g_cfg.target);
  g_cfg.speed    = prefs.getFloat ("speed",    g_cfg.speed);
  g_cfg.velocity = prefs.getFloat ("velocity", g_cfg.velocity);
  g_cfg.mode     = (Mode)prefs.getUChar("mode", (uint8_t)MODE_IDLE);

  // WiFi：字符串用 getString，读到 g_cfg.ssid/pass
  prefs.getString("ssid", g_cfg.ssid, sizeof(g_cfg.ssid));
  prefs.getString("pass", g_cfg.pass, sizeof(g_cfg.pass));

  prefs.end();
}

void configSave() {
  Preferences prefs;
  prefs.begin(NVS_NAMESPACE, false);  // 读写
  prefs.putFloat ("range",    g_cfg.range);
  prefs.putFloat ("offset",   g_cfg.offset);
  prefs.putFloat ("target",   g_cfg.target);
  prefs.putFloat ("speed",    g_cfg.speed);
  prefs.putFloat ("velocity", g_cfg.velocity);
  prefs.putUChar ("mode",     (uint8_t)g_cfg.mode);
  // WiFi 不在这里存（字符串单独由 setWifiConfig 存，避免每次参数变更都重写大块）
  prefs.end();
}

bool hasWifiConfig() {
  return g_cfg.ssid[0] != '\0';
}

void setWifiConfig(const String& ssid, const String& pass) {
  // 先更新内存里的副本
  strncpy(g_cfg.ssid, ssid.c_str(), sizeof(g_cfg.ssid) - 1);
  g_cfg.ssid[sizeof(g_cfg.ssid) - 1] = '\0';
  strncpy(g_cfg.pass, pass.c_str(), sizeof(g_cfg.pass) - 1);
  g_cfg.pass[sizeof(g_cfg.pass) - 1] = '\0';

  // 单独写 NVS（字符串键）
  Preferences prefs;
  prefs.begin(NVS_NAMESPACE, false);
  prefs.putString("ssid", ssid);
  prefs.putString("pass", pass);
  prefs.end();
}
