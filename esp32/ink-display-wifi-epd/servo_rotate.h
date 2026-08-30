/*
 * servo_rotate.h — 极简舵机驱动（相框旋转专用）
 *
 * 设计参考 esp32/servo_serial_cmd/motion.h（已验证平顺）：
 *  - 舵机全程保持 attach，不在每次转动后 detach。ESP32Servo 的 attach() 会立即
 *    输出 PWM 且首帧脉宽不可控，反复 attach/detach 是抖动的根源。
 *  - 只在主流程深睡前调 servo_detach() 一次（省电，PWM 停止）。
 *  - servo_rotate_to() 不 attach/detach，纯按速度推进角度，阻塞小循环实现。
 *
 * 全局速度（servo_set_default_speed）：
 *  - 所有转动路径（首次归位 / 普通转动 / "已在目标"重写）统一走全局速度，
 *    杜绝裸 write() 全速甩动。主流程从 NVS 读 servo_speed 后调一次同步。
 *
 * 与 ink-display-wifi-epd 主流程的集成：
 *  - servo_init() 在 setup() 早期调用一次（attach）。
 *  - servo_rotate_to(target, speed, timeout) 在"下 bin 完成后、刷屏前"调用。
 *  - servo_detach() 在 goDeepSleepMinutes() 深睡前调用（停止 PWM 省电）。
 */
#pragma once
#include <Arduino.h>

// 舵机信号脚（避开 EPD SPI 占用的 13/12/14/27/18/23 + strapping 0/2/12/15 + LED 2，
//              且避开 DAC 脚 25/26）
#define SERVO_PIN 32

// 全局默认转动速度的兜底（°/s）；主流程应从 NVS 读 servo_speed 后用
// servo_set_default_speed() 覆盖。带载重时调小保扭矩。
static const float SERVO_DEFAULT_SPEED = 40.0f;

// 初始化舵机：分配 timer、设 50Hz、attach 到 SERVO_PIN。开机调一次。
void servo_init();

// 设置全局默认转动速度（°/s）。主流程从 NVS 读 servo_speed 后调一次。
void servo_set_default_speed(float speed_deg_s);

// 重新 attach（深睡恢复后）。先 write 当前已知角度避免 attach 瞬间跳。
// 已 attach 时是空操作。servo_rotate_to 内部会自动调，通常无需手动调。
void servo_attach();

// 转到目标绝对角度（阻塞，转完或超时返回）
//   target_deg    - 目标角度（°，由标定写入；本函数不夹量程）
//   speed_deg_s   - 转动速度（°/s）；传 <0（默认）则用全局默认速度
//   timeout_ms    - 超时（默认 3000ms）
// 返回值：true=到位，false=超时
// 注意：本函数不 detach——舵机保持 attach 平顺。深睡前由主流程统一 servo_detach()。
bool servo_rotate_to(float target_deg, float speed_deg_s = -1.0f, uint32_t timeout_ms = 3000);

// 立即停止 PWM 输出（仅深睡前调；运行态保持 attach）
void servo_detach();

// 舵机当前位置是否可信（开机后 false；首次 servo_rotate_to 成功后 true；
// servo_detach 不改变它）。主流程的"同朝向跳过"必须以此为前提——
// 开机后 servo_init 把舵机打回 0°，此时跳过转动会让相框停在零点。
bool servo_position_known();
