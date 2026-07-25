/*
 * config.h — 全局参数定义 + NVS(Preferences) 持久化
 *
 * 所有可配置参数集中在这里，避免散落各处。运动状态(当前位置/方向)不在此处，
 * 它们是运行态，断电不保存。
 */
#pragma once
#include <Arduino.h>

// ---- 工作模式 ----
enum Mode : uint8_t {
  MODE_IDLE     = 0,   // 不动，保持当前位置
  MODE_POSITION = 1,   // 位置控制：走到 target 停
  MODE_VELOCITY = 2,   // 速度控制：按 velocity 持续旋转 / 往复
};

// ---- 全局配置（可被 NVS 保存）----
struct Config {
  Mode   mode      = MODE_IDLE;   // 当前/上次模式
  float  range     = 360.0f;      // 可转动范围(°)：360=连续旋转舵机；<360=位置舵机
  float  offset    = 0.0f;        // 零位偏移校正(°)，最终输出 = 逻辑角度 + offset
  // 位置模式参数
  float  target    = 0.0f;        // 目标角度(°)，范围 [0, range]
  float  speed     = 60.0f;       // 移动速度(°/s)，仅大小
  // 速度模式参数
  float  velocity  = 0.0f;        // 角速度(°/s)，正=正转，负=反转，0=停止
  // WiFi 配置（首次用 AP 配网模式设置，之后存这里自动连）
  char   ssid[33]  = {0};         // SSID，最长 32 字节 + \0
  char   pass[65]  = {0};         // 密码，最长 64 字节 + \0
};

extern Config g_cfg;               // 全局单例（在 config.cpp 定义）

// NVS 读写
void configLoad();                 // 开机时读，读不到用默认值
void configSave();                 // 任何参数变更后调用

// WiFi 配置专用（字符串读写需要单独处理）
bool hasWifiConfig();              // ssid 是否非空
void setWifiConfig(const String& ssid, const String& pass);  // 写 NVS
