/*
 * webui.h — 网页 HTML 字符串 + JSON 序列化/解析
 */
#pragma once
#include <Arduino.h>

// 返回完整的单页 HTML（前端），用 raw string literal，避免一堆转义
const char* getIndexHtml();           // 控制台主页（已配网后用）
const char* getSetupHtml();           // 配网页面（AP 模式下用，仅设 WiFi）

// 把当前状态序列化成 JSON 字符串（响应 GET /api/state）
// ipStr: WiFi IP 字符串，连不上时传空串
// outAngle: 舵机实际输出角度（逻辑角度+offset）
String buildStateJson(float curAngle, bool running, const String& ipStr, float outAngle);

// 从 POST body 解析控制指令，直接更新 g_cfg（成功返回 true）
// 解析失败/非法返回 false，errMsg 填错误描述
bool parseControl(const String& body, String& errMsg);
