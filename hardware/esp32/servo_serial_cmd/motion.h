/*
 * motion.h — 非阻塞运动规划器
 *
 * 设计要点：
 *  - 完全非阻塞，靠 tick(dt) 推进，绝不用 delay()。
 *  - 逻辑角度 curAngle 始终在 [0, range] 内（range=360 时允许超过 360 表示已转圈数? 不，
 *    为了简单和 UI 友好，curAngle 也限制在 [0, range]——360° 时"连续旋转"表现为
 *    角度在 0~360 之间循环回绕）。
 *  - offset 在最后一层 applyOutput() 套用，逻辑层不感知。
 *  - 连续旋转舵机的"持续转"通过 curAngle 在 0~range 循环回绕实现。
 */
#pragma once
#include <Arduino.h>
#include <ESP32Servo.h>
#include "config.h"

class Motion {
public:
  void begin(Servo* servo, int pin);   // 绑定舵机和引脚
  void tick();                         // loop() 每 tick 调一次，内部用 millis() 算 dt
  void applyConfigChange();            // 模式/参数被网页修改后调用，重置规划状态

  // 舵机使能控制（detach 后必须禁用 tick 的输出，否则 write() 会自动重连）
  void enable();                       // 重新启用：attach + 恢复输出
  void disable();                      // 停止输出：detach 舵机，tick 不再调 write
  bool isEnabled() const { return _enabled; }

  // 运行态查询（给 UI 用）
  float getLogicalAngle() const { return _curAngle; }   // 逻辑角度 [0, range]
  float getOutputAngle()  const { return _outAngle; }   // 实际写到舵机的角度（逻辑+offset）
  bool  isRunning()       const { return _cfg->mode != MODE_IDLE; }

private:
  Servo* _servo = nullptr;
  int    _pin   = -1;
  Config* _cfg  = nullptr;
  bool   _enabled = true;     // 舵机是否启用（false 时 tick 不输出，避免 write 自动重连）

  float  _curAngle    = 0.0f;   // 逻辑当前角度
  float  _outAngle    = 0.0f;   // 实际输出角度（给UI显示用）
  uint32_t _lastTickMs = 0;     // 上次 tick 的时间戳
  int    _velDir      = 1;      // 速度模式下往复时的当前方向(+1/-1)

  void applyOutput();           // 把 _curAngle(+offset) 写到舵机
};
