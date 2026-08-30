/*
 * servo_rotate.cpp — 极简舵机驱动实现
 *
 * 设计参考 esp32/servo_serial_cmd/motion.cpp（已验证平顺）：
 *  - 舵机全程保持 attach，不在每次转动后 detach。ESP32Servo 的 attach() 会立即
 *    输出 PWM 且首帧脉宽不可控，反复 attach/detach 是抖动的根源。
 *  - 只在主流程深睡前调 servo_detach() 一次（省电，PWM 停止）。
 *  - servo_rotate_to() 假定已 attach（servo_init 时 attach 一次后不再 detach），
 *    内部不 attach/detach，纯按速度推进角度，与 motion.tick() 的推进逻辑一致。
 *  - 阻塞小循环推进（主流程无 loop()），millis() 算 dt，每 20ms 写一次。
 *
 * 位置跟踪：
 *  - servo_init(init_deg) 首帧即驱动到"上次已知角度"并置 _angleKnown=true：
 *    断电期间舵机机械保持，物理位置≈上次指令角度，开机零动作（写 0 会全速
 *    甩到机械零点，挂框状态会损伤结构——历史上"开机归零"事故的根源）。
 *  - servo_detach() 不动 _angleKnown（机械保持，detach 后物理姿态不变）。
 *  - 之后所有转动都从 _curAngle 平滑走（40°/s），杜绝任何全速甩动。
 */
#include "servo_rotate.h"
#include <ESP32Servo.h>

static Servo   _servo;
static bool    _attached   = false;
static float   _curAngle   = 0.0f;
static bool    _angleKnown = false;  // 仅开机首次 false
static float   _defaultSpeed = SERVO_DEFAULT_SPEED;

void servo_init(float init_deg) {
  // attach 前先 write：ESP32Servo attach 瞬间会输出 PWM，先 write 让首帧脉宽确定。
  // 首帧必须写"上次已知角度"而不是 0：深睡/reset 期间舵机断电靠机械保持，
  // 物理位置 ≈ 上次指令角度，首帧写同角度 → 正常开机零动作、零冲击。
  // 写 0 会让挂着相框的舵机每次开机全速甩到机械零点——大转动惯量下会损伤结构。
  _servo.write(init_deg);
  ESP32PWM::allocateTimer(0);
  _servo.setPeriodHertz(50);
  _servo.attach(SERVO_PIN, 500, 2400);
  _attached   = true;
  _curAngle   = init_deg;
  _angleKnown = true;   // 位置按指令角主动驱动，视为可信（"同朝向跳过"因此成立）
}

void servo_set_default_speed(float speed_deg_s) {
  if (speed_deg_s >= 1.0f) _defaultSpeed = speed_deg_s;
}

void servo_attach() {
  // 深睡前 detach 过、现在要恢复：重新 attach。先 write 当前已知角度避免跳。
  if (_attached) return;
  float w = _angleKnown ? _curAngle : 0.0f;
  _servo.write(w);
  ESP32PWM::allocateTimer(0);
  _servo.setPeriodHertz(50);
  _servo.attach(SERVO_PIN, 500, 2400);
  _attached = true;
}

void servo_detach() {
  if (!_attached) return;
  _servo.detach();
  _attached = false;
  // 不动 _angleKnown：相框靠机械保持，detach 后物理姿态不变，_curAngle 仍可信。
}

bool servo_position_known() {
  return _angleKnown;
}

bool servo_at_angle(float target_deg, float tol_deg) {
  return fabs(_curAngle - target_deg) <= tol_deg;
}

bool servo_rotate_to(float target_deg, float speed_deg_s, uint32_t timeout_ms) {
  // 确保已 attach（深睡恢复后 / 兜底）
  if (!_attached) servo_attach();

  // 速度：<0 用全局默认；<1 兜底防 0/负值卡死
  if (speed_deg_s < 0.0f) speed_deg_s = _defaultSpeed;
  if (speed_deg_s < 1.0f) speed_deg_s = 1.0f;

  // 起点：已知用 _curAngle；首次开机未知用 0°
  float startAngle = _angleKnown ? _curAngle : 0.0f;
  _curAngle = startAngle;

  float diff = target_deg - startAngle;
  if (fabs(diff) < 0.5f) {
    // 已到位：写一次定位（位移极小，不甩）
    _servo.write(target_deg);
    delay(100);
    _curAngle = target_deg;
    _angleKnown = true;
    return true;   // 不 detach，保持 attach
  }

  // 平滑推进（与 motion.tick 同款逻辑，阻塞版）
  uint32_t start = millis();
  uint32_t lastTick = millis();

  while (true) {
    uint32_t now = millis();

    if (now - start >= timeout_ms) {
      return false;   // 超时不 detach，保持 attach（深睡时统一 detach）
    }

    if (now - lastTick >= 20) {
      float dt = (now - lastTick) / 1000.0f;
      lastTick = now;

      float step = speed_deg_s * dt;
      diff = target_deg - _curAngle;

      if (fabs(diff) <= step) {
        _curAngle = target_deg;
        _servo.write(target_deg);
        delay(150);   // 最后就位时间
        _angleKnown = true;
        return true;   // 不 detach
      } else {
        _curAngle += (diff > 0 ? step : -step);
        _servo.write(_curAngle);
      }
    }

    delay(2);
  }
}
