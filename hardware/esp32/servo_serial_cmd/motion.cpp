/*
 * motion.cpp — 运动规划器实现
 *
 * tick() 用 millis() 计算真实 dt(秒)，按当前 mode 推进 _curAngle。
 *  - 位置模式：朝 target 走，速度 = speed(°/s)，到位切到 IDLE。
 *  - 速度模式：朝 _velDir 方向以 |velocity| 推进；
 *              range<360 时撞端点反向(往复)；range==360 时回绕(连续旋转)。
 * 最后 applyOutput() 把 _curAngle + offset 写到舵机（不夹量程，交给库处理）。
 */
#include "motion.h"

static const uint32_t TICK_INTERVAL_MS = 20;   // 50Hz，和舵机一致

void Motion::begin(Servo* servo, int pin) {
  _servo = servo;
  _pin   = pin;
  _cfg   = &g_cfg;
  _curAngle = 0.0f;
  _velDir   = 1;
  _lastTickMs = millis();
}

void Motion::applyConfigChange() {
  // 网页改了参数后，根据新模式重置内部状态
  if (_cfg->mode == MODE_VELOCITY) {
    // 速度模式：方向由 velocity 正负决定
    _velDir = (_cfg->velocity >= 0) ? 1 : -1;
  }
  _lastTickMs = millis();   // 重置时间，避免一次大 dt 跳变
}

void Motion::enable() {
  if (!_servo) return;
  _servo->attach(_pin, 500, 2400);
  _enabled = true;
  _lastTickMs = millis();
  applyOutput();   // 立刻输出一次，恢复保持
}

void Motion::disable() {
  if (!_servo) return;
  _servo->detach();          // 停止 LEDC PWM 输出
  _enabled = false;          // 关键：tick 不再调 write，避免 write 自动重连
}

void Motion::tick() {
  uint32_t now = millis();
  if (now - _lastTickMs < TICK_INTERVAL_MS) return;   // 还没到 20ms
  float dt = (now - _lastTickMs) / 1000.0f;            // 秒
  _lastTickMs = now;

  if (!_servo) return;
  if (!_enabled) return;          // 关键：禁用时完全不输出，避免 write() 自动重连舵机

  switch (_cfg->mode) {
    case MODE_POSITION: {
      float target = _cfg->target;
      // 限幅 target 到 [0, range]
      if (target < 0) target = 0;
      if (target > _cfg->range) target = _cfg->range;

      float diff = target - _curAngle;
      float maxStep = _cfg->speed * dt;        // 这一 tick 最多走多少度
      if (fabs(diff) <= maxStep) {
        _curAngle = target;                    // 到位
        _cfg->mode = MODE_IDLE;                // 自动切空闲
        // 注意：这里不改 NVS 里存的 mode，只是运行态切到 IDLE。
        // 用户下次开机仍按 NVS 里上次的 mode；如果想要"到位后持久化切IDLE"可以 configSave()，
        // 但频繁写 Flash 不好，所以这里只在内存里切。
      } else {
        _curAngle += (diff > 0 ? maxStep : -maxStep);
      }
      break;
    }
    case MODE_VELOCITY: {
      float v = fabs(_cfg->velocity) * _velDir;   // 带符号速度
      _curAngle += v * dt;

      if (_cfg->range >= 360.0f) {
        // 连续旋转：回绕到 [0,360)
        while (_curAngle >= 360.0f) _curAngle -= 360.0f;
        while (_curAngle < 0.0f)    _curAngle += 360.0f;
      } else {
        // 位置舵机：到端点反向(往复)
        if (_curAngle > _cfg->range) { _curAngle = _cfg->range; _velDir = -1; }
        if (_curAngle < 0.0f)        { _curAngle = 0.0f;        _velDir =  1; }
      }
      // velocity==0 时实际上不动，但 mode 仍是 VELOCITY——视为"暂停"
      break;
    }
    case MODE_IDLE:
    default:
      // 保持 _curAngle 不动，但仍要 applyOutput()：
      // 因为 offset 可能被网页修改了，IDLE 时也要让舵机重新输出到新位置
      break;
  }

  applyOutput();   // 任何模式都执行输出（IDLE 时是"原地重新定位"，会套用最新 offset）
}

void Motion::applyOutput() {
  if (!_servo) return;
  // 输出 = 逻辑角度 + offset（offset≥0，表示舵机物理零点与外部零点的距离）
  // 不在这里夹 [0,180]：舵机物理量程可能 >180（连续旋转舵机），
  // 超出 ESP32Servo 库内部量程的部分由库自行处理。
  _outAngle = _curAngle + _cfg->offset;
  _servo->write(_outAngle);
}
