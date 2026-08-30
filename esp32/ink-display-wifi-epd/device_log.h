/*
 * device_log.h — 设备日志（黑盒）：LittleFS 滚动存储 + 联网上传树莓派
 *
 * 设计决策见 docs/adr/0001-esp32-flash-device-journal.md：
 *  - 每次开机先记一条 BOOT（上次复位原因 + 本次唤醒原因），跨重启可回查事故
 *  - 事件即时落盘（崩溃前的最后一条也留得住），双段滚动，本地容量 ~64KB
 *  - 时间戳 = 开机序号 + 相对毫秒；授时成功后记锚点，绝对时间由服务器换算
 *  - 上传在 goDeepSleepMinutes() 里触发（入睡前咽喉点），收到 200 才推进游标
 *
 * 事件类型（14 种，清单见 docs/esp32-device-log.md）：
 *   BOOT / SLEEP / WIFI / AP_PORTAL / TIME_SYNC / PHOTO_JSON / PHOTO_BIN /
 *   RENDER / SERVO_CMD / SERVO_SKIP / SERVO_DONE / SERVO_SKIP_UNCAL /
 *   HTTP_HIT / LOG_UP
 */
#ifndef DEVICE_LOG_H
#define DEVICE_LOG_H

#include <Arduino.h>

#define EV_BOOT           "BOOT"
#define EV_SLEEP          "SLEEP"
#define EV_WIFI           "WIFI"
#define EV_AP             "AP_PORTAL"
#define EV_TIME           "TIME_SYNC"
#define EV_PJSON          "PHOTO_JSON"
#define EV_PBIN           "PHOTO_BIN"
#define EV_RENDER         "RENDER"
#define EV_SCMD           "SERVO_CMD"
#define EV_SSKIP          "SERVO_SKIP"
#define EV_SDONE          "SERVO_DONE"
#define EV_SUNCAL         "SERVO_SKIP_UNCAL"
#define EV_HTTP           "HTTP_HIT"
#define EV_UPLOAD         "LOG_UP"

// setup() 最早调用：挂载 LittleFS、开机序号 +1、恢复行号游标、记 BOOT 事件
void journalBegin();

// 注入上传目标（loadConfig 之后调用；hostport 为空时上传自动跳过）
void journalSetServer(const String& hostport, const String& key);

// 追加一条事件（printf 风格 detail，即时落盘）。未挂载 FS 时静默丢弃。
void journalEvent(const char* type, const char* fmt = "", ...);

// 把上传游标之后的所有事件分批 POST 到树莓派；200 才推进游标。
// 需要 WiFi 已连接；断网/未配置时直接返回。
void journalUpload();

uint32_t journalBootSeq();

#endif   // DEVICE_LOG_H
