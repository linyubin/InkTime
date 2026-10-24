#!/usr/bin/env python3
"""构图完整性惩罚(compute_crop_penalty)单元测试 — 2026-10-16"""
import sys, json
sys.path.insert(0, '/home/andy/inktime')
import render_daily_photo as rdp

cases = []
def case(name, got, want):
    ok = abs(got - want) < 1e-6
    cases.append((name, ok, got, want))

# 1. 今日无头照: person 贴顶 h=0.96 → 罚(切头)
p_headless = json.dumps([{"label":"person","bbox":[0.5,0.48,0.9,0.96],"conf":0.8}])
case("无头照(贴顶h0.96)", rdp.compute_crop_penalty(p_headless), 0.85)

# 2. 孩子睡照: 仅贴底 h=0.92 → 轻罚(切脚=半身/特写)
p_feetcut = json.dumps([{"label":"person","bbox":[0.5,0.54,0.9,0.92],"conf":0.42}])
case("切脚照(贴底h0.92)", rdp.compute_crop_penalty(p_feetcut), 0.96)

# 2b. 满幅特写豁免: 双贴边(黑白人像类) → 不罚
p_full = json.dumps([{"label":"person","bbox":[0.501,0.5002,0.983,0.9958],"conf":0.782}])
case("满幅特写双贴边豁免", rdp.compute_crop_penalty(p_full), 1.0)

# 2c. 满幅特写真实数据(#10 黑白人像)
p_real10 = json.dumps([{"label":"person","bbox":[0.501,0.5002,0.983,0.9958],"conf":0.782}])
case("黑白人像#10实拍bbox", rdp.compute_crop_penalty(p_real10), 1.0)

# 2d. 满幅特写真实数据(#11 黑白人像)
p_real11 = json.dumps([{"label":"person","bbox":[0.425,0.5009,0.844,0.991],"conf":0.786}])
case("黑白人像#11实拍bbox", rdp.compute_crop_penalty(p_real11), 1.0)

# 3. #7 三人照: 全部框在画面中部 → 不罚
p_ok = json.dumps([
    {"label":"person","bbox":[0.3,0.44,0.2,0.19],"conf":0.88},
    {"label":"person","bbox":[0.5,0.61,0.3,0.32],"conf":0.88},
    {"label":"person","bbox":[0.6,0.75,0.3,0.35],"conf":0.86},
    {"label":"cat","bbox":[0.4,0.20,0.3,0.15],"conf":0.79},
])
case("三人玩猫(完整)", rdp.compute_crop_penalty(p_ok), 1.0)

# 4. 远景小人贴顶: 矮框不罚
p_far = json.dumps([{"label":"person","bbox":[0.5,0.02,0.05,0.04],"conf":0.5}])
case("远景小人贴顶(h0.04)", rdp.compute_crop_penalty(p_far), 1.0)

# 5. 双切头叠加: 0.85^2
p_two = json.dumps([
    {"label":"person","bbox":[0.3,0.48,0.9,0.96],"conf":0.8},
    {"label":"person","bbox":[0.7,0.48,0.9,0.96],"conf":0.8},
])
case("双切头叠加", rdp.compute_crop_penalty(p_two), 0.85**2)

# 6. 横向贴边不罚
p_side = json.dumps([{"label":"person","bbox":[0.98,0.5,0.05,0.5],"conf":0.9}])
case("横向贴边", rdp.compute_crop_penalty(p_side), 1.0)

# 7. 空/脏数据向后兼容
case("空字符串", rdp.compute_crop_penalty(""), 1.0)
case("空列表", rdp.compute_crop_penalty("[]"), 1.0)
case("脏数据", rdp.compute_crop_penalty("not json"), 1.0)

# 8. 猫切脚 (cat 也在重要主体表, 切脚轻罚)
p_cat = json.dumps([{"label":"cat","bbox":[0.5,0.56,0.8,0.82],"conf":0.67}])
case("猫切脚(h0.82)", rdp.compute_crop_penalty(p_cat), 0.96)

# 9. 风景照无主体
case("风景照无主体", rdp.compute_crop_penalty(''), 1.0)

fails = [c for c in cases if not c[1]]
for name, ok, got, want in cases:
    print(f"{'✓' if ok else '✗'} {name}: got={got:.4f} want={want:.4f}")
print(f"\n{len(cases)-len(fails)}/{len(cases)} passed")
sys.exit(1 if fails else 0)
