#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""脸框零容忍选型回归（2026-10-24）。

背景翻车照：布丁特写（P1030723.JPG），人脸占满画幅，L10 实白铭牌压住嘴
下巴却只遮主体 bbox 12%，不触发 25% 主体级硬线。修复：分析期 yolov8n-pose
派生脸框（subjects_json 的 face 字段），选型期覆盖区在脸框上的有效不透明度
≥ COVERAGE_FACE_VETO_OPACITY 即硬否决。

覆盖：脸框×glass/veil 冲突、渐变按行积分、老数据无 face 行为不变、
50 张随机回归。
用法: /home/andy/inktime/venv/bin/python scripts/test_face_veto_20261024.py
"""
import sys, json, random
sys.path.insert(0, '/home/andy/inktime')
import render_daily_photo as rdp
import layout_strategy as ls

W, H = 6528, 4896  # 4:3 横屏代表尺寸


def subj(label, conf, cx, cy, bw, bh, face=None):
    """构造单主体 subjects_json（bbox/face: cx, cy, w, h 归一化）。"""
    d = {'label': label, 'conf': conf, 'bbox': [cx, cy, bw, bh]}
    if face is not None:
        d['face'] = face
    return json.dumps([d])


def item_with(subjects, w=W, h=H):
    return {'path': 'x', 'date': '2026-10-24', 'city': '苏州',
            'width': w, 'height': h, 'subjects_json': subjects}


def unit_tests():
    # 翻车照几何：4592×3448 特写，人物框占满画幅（bbox 面 82%），
    # 脸在画面中上部（眼约 canvas y 0.35，下巴约 0.78）
    crash = item_with(subj('person', 0.886, 0.4214, 0.5011, 0.8385, 0.9789,
                           face=[0.42, 0.45, 0.55, 0.62]),
                      w=4592, h=3448)
    # 同几何但无 face 字段（老数据口径）
    crash_noface = item_with(subj('person', 0.886, 0.4214, 0.5011, 0.8385, 0.9789),
                             w=4592, h=3448)
    cases = [
        # (名称, item, layout, 期望 None/'hard'/'soft', 期望来源或 None)
        ('翻车照: 脸在 L10 铭牌带', crash, 'L10', 'hard', 'face'),
        ('翻车照: 脸碰 L01 毛玻璃卡', crash, 'L01', 'hard', 'face'),
        # 脸框大部分在渐变浅端(眼睛嘴可读)不拦；探进深端才拦
        ('翻车照: L04 脸多在浅端→不硬否', crash, 'L04', 'soft', 'subject'),
        ('同一张无 face 数据: 退回主体级(仅 soft)', crash_noface, 'L10', 'soft', 'subject'),
        ('脸在铭牌上方, 不误杀', item_with(subj('person', 0.9, 0.5, 0.2, 0.3, 0.2,
                                                face=[0.5, 0.12, 0.18, 0.16])), 'L10', None, None),
        ('脸碰 L01 卡上缘', item_with(subj('person', 0.9, 0.5, 0.62, 0.5, 0.35,
                                           face=[0.5, 0.75, 0.14, 0.13])), 'L01', 'hard', 'face'),
        ('脸在 L01 卡上方', item_with(subj('person', 0.9, 0.5, 0.45, 0.4, 0.3,
                                           face=[0.5, 0.38, 0.13, 0.12])), 'L01', None, None),
        ('L04 渐变浅端不拦(小脸在上)', item_with(subj('person', 0.9, 0.5, 0.3, 0.3, 0.25,
                                                      face=[0.5, 0.24, 0.13, 0.12])), 'L04', None, None),
        ('L04 脸探进渐变深端→拦(底部渐变必然连身, subject 先触发)',
         item_with(subj('person', 0.9, 0.5, 0.62, 0.35, 0.6,
                        face=[0.5, 0.85, 0.2, 0.16])), 'L04', 'hard', 'subject'),
        # subject 级硬线先触发时来源记 subject（两层防护都在，标签取先触发者）
        ('P10 底部渐变深端压脸', item_with(subj('person', 0.9, 0.5, 0.85, 0.3, 0.25,
                                                face=[0.5, 0.9, 0.2, 0.18]),
                                           w=3456, h=4608), 'P10', 'hard', 'subject'),
        ('多人: 第二张脸在铭牌内', item_with(json.dumps([
            {'label': 'person', 'conf': 0.9, 'bbox': [0.2, 0.5, 0.2, 0.4],
             'face': [0.2, 0.35, 0.08, 0.07]},
            {'label': 'person', 'conf': 0.85, 'bbox': [0.55, 0.85, 0.25, 0.22],
             'face': [0.55, 0.82, 0.09, 0.08]}])), 'L10', 'hard', 'subject'),
        # 宽构图多人：第二人 bbox 大被稀释+中心在牌外(主体级不触发)但脸在铭牌内
        ('多人: 仅脸冲突(主体级不触发)', item_with(json.dumps([
            {'label': 'person', 'conf': 0.9, 'bbox': [0.15, 0.5, 0.2, 0.4],
             'face': [0.15, 0.35, 0.08, 0.07]},
            {'label': 'person', 'conf': 0.85, 'bbox': [0.42, 0.55, 0.95, 0.85],
             'face': [0.42, 0.78, 0.09, 0.08]}])), 'L10', 'hard', 'face'),
        # 顶部小悬挂签：脸入签内、身体在签外(主体级不触发)——脸框独有拦截
        ('L06 脸入顶签(身体在签外)', item_with(subj('person', 0.9, 0.6, 0.45, 0.3, 0.7,
                                                    face=[0.27, 0.22, 0.15, 0.12])), 'L06', 'hard', 'face'),
        ('猫无脸框: 走主体级', item_with(subj('cat', 0.9, 0.5, 0.85, 0.3, 0.3)), 'L01', 'hard', 'subject'),
        ('无尺寸不误杀', item_with(subj('person', 0.9, 0.5, 0.5, 0.8, 0.8,
                                        face=[0.5, 0.4, 0.5, 0.4]), w=0, h=0), 'L10', None, None),
    ]
    fails = 0
    for name, it, lid, expect, src in cases:
        got = ls.coverage_conflict(it, lid, it['width'] or None, it['height'] or None)
        kind = got[0] if got else None
        got_src = got[3] if got and len(got) > 3 else None
        ok = kind == expect and (src is None or got_src == src)
        fails += 0 if ok else 1
        detail = f"kind={kind} src={got_src} occ={got[1]:.0%}" if got else "None"
        print(f"  [{'✓' if ok else '✗'}] {name}: expect={expect}/{src} got {detail}")
    return fails


def test_crash_pick():
    """端到端：翻车照在新脸框数据下不得再选 L10。"""
    crash = item_with(subj('person', 0.886, 0.4214, 0.5011, 0.8385, 0.9789,
                           face=[0.42, 0.45, 0.55, 0.62]),
                      w=4592, h=3448)
    crash['side'] = '笑得像刚偷吃糖果的小熊'
    crash['memory'] = 92.5
    pick, info = ls.pick_layout(crash, 'landscape', 0.546, img_size=(4592, 3448))
    v10 = info['vetoed'].get('L10', '')
    ok = pick != 'L10' and v10.startswith('coverage:')
    print(f"  [{'✓' if ok else '✗'}] 翻车照端到端: pick={pick} tie={info['tie']} L10否决={v10!r}")
    return 0 if ok else 1


def regression(n=50, seed=20261024):
    """老数据（无 face 字段）行为必须与 10-09 版完全一致。"""
    random.seed(seed)
    rows = rdp.load_sim_rows()
    sample = random.sample(rows, min(n, len(rows)))
    errors, vetoed_face = 0, 0
    for it0 in sample:
        item = dict(it0)
        try:
            w, h = int(item.get('width') or 0), int(item.get('height') or 0)
            orient = 'portrait' if h > w else 'landscape'
            pick, info = ls.pick_layout(item, orient, 0.45,
                                        img_size=(w, h) if w and h else None)
            if any(v.startswith('coverage:') and '人脸' in v
                   for v in info.get('vetoed', {}).values()):
                vetoed_face += 1
        except Exception as e:
            errors += 1
            print(f"  [ERR] {item.get('path','?')}: {type(e).__name__}: {e}")
    ok = errors == 0 and vetoed_face == 0
    print(f"  [{'✓' if ok else '✗'}] {len(sample)} 张老数据回归: 错误={errors} "
          f"误触发脸否决={vetoed_face}（老数据应全部不触发）")
    return 0 if ok else 1


if __name__ == '__main__':
    print("== 脸框×覆盖区单测 ==")
    f1 = unit_tests()
    print("== 翻车照端到端 ==")
    f2 = test_crash_pick()
    print("== 老数据（无 face）行为不变回归 ==")
    f3 = regression()
    total = f1 + f2 + f3
    print(f"\n结论: 失败={total}")
    sys.exit(1 if total else 0)
