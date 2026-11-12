#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全图大框零容忍 + 渲染端惰性补脸回归（2026-11-12）。

背景翻车照：IMG_20171112_083911_705.jpg（贴纸胡子娘俩合影）。
YOLO 把两人贴脸合影检成全图大框（bbox cx/cy/w/h = 0.5/0.5/0.99/1.0，
conf 0.392），主体级遮挡率被画布稀释到 0.119 << 0.25 硬线；且为
2026-10-24 之前的分析数据、person 无 face 字段，脸框零容忍形同虚设。
L10 实白铭牌压住孩子嘴下巴。

修复：① fullframe 规则——person 框 w、h ≥ COVERAGE_FULLFRAME_MIN_SPAN(0.55)
时，eff ≥ COVERAGE_FACE_VETO_OPACITY 的覆盖区硬否决；② 渲染端惰性补脸
（FACE_BACKFILL_ON_RENDER，pose 现场派生 face 字段，内存态不落库）。

用法: /home/andy/inktime/venv/bin/python scripts/test_fullframe_face_20261112.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config as cfg  # noqa: E402
from inktime.render import render_daily_photo as rdp  # noqa: E402
from inktime.render import layout_strategy as ls  # noqa: E402

W, H = 2576, 1449  # 翻车照真实尺寸（landscape）


def subj(label, conf, cx, cy, bw, bh, face=None):
    d = {'label': label, 'conf': conf, 'bbox': [cx, cy, bw, bh]}
    if face is not None:
        d['face'] = face
    return json.dumps([d])


def item_with(subjects, w=W, h=H):
    return {'path': 'x', 'date': '2026-11-12', 'city': '',
            'width': w, 'height': h, 'subjects_json': subjects}


def unit_tests():
    today = item_with(subj('person', 0.392, 0.4999, 0.5, 0.9948, 1.0))
    mixed = json.dumps([
        # 成人有脸框（pose 成功）+ 孩子合并大框无脸（贴纸脸 pose 失败）
        {'label': 'person', 'conf': 0.8, 'bbox': [0.78, 0.3, 0.2, 0.3],
         'face': [0.78, 0.22, 0.07, 0.06]},
        {'label': 'person', 'conf': 0.392, 'bbox': [0.4999, 0.5, 0.9948, 1.0]},
    ])
    cases = [
        # (名称, item, layout, 期望 None/'hard'/'soft', 期望来源或 None)
        ('翻车照: L10 铭牌 → fullframe 硬否决', today, 'L10', 'hard', 'fullframe'),
        ('翻车照: L01 毛玻璃卡 → fullframe 硬否决', today, 'L01', 'hard', 'fullframe'),
        # 老数据全图大框下 veil/glass 全部强遮区都不可用（脸位置盲区）
        ('翻车照: L04 渐变 → fullframe 否决', today, 'L04', 'hard', 'fullframe'),
        ('翻车照: L05 → fullframe 否决', today, 'L05', 'hard', 'fullframe'),
        ('翻车照: L06 顶签 → fullframe 否决', today, 'L06', 'hard', 'fullframe'),
        # 常规单人小框不受影响：脸部有 face 字段且不在覆盖区 → 不否决
        ('常规小框+脸在上: L10 不否决',
         item_with(subj('person', 0.9, 0.5, 0.3, 0.25, 0.4, face=[0.5, 0.2, 0.1, 0.1])),
         'L10', None, None),
        # 全图大框带真实脸框 → 脸框零容忍精确接管，fullframe 不重复触发；
        # 但大框本身与铭牌带的重叠仍走主体级 soft（正常扣分，非否决）
        ('全图大框+带脸在上: 脸框接管, 主体级 soft',
         item_with(subj('person', 0.9, 0.5, 0.5, 0.9, 0.95, face=[0.5, 0.15, 0.08, 0.07])),
         'L10', 'soft', 'subject'),
        # 混合人群：有人有脸、有人无脸的全图大框 → 保护无脸盲区
        ('混合人群: 无脸大框 → fullframe 否决', item_with(mixed), 'L10', 'hard', 'fullframe'),
        # 风景照无主体 → 一切照旧
        ('无主体: 不否决', item_with(json.dumps([])), 'L10', None, None),
        # 竖版大框：P10 底部渐变峰值 0.85 ≥ 0.5 → fullframe 否决
        ('竖版大框: P10 → fullframe 否决',
         item_with(subj('person', 0.8, 0.5, 0.5, 0.9, 0.95), w=3456, h=4608),
         'P10', 'hard', 'fullframe'),
        # 竖版全身小框（w<0.55）→ 不触发 fullframe，走原主体级
        ('竖版全身小框: P10 走原主体级',
         item_with(subj('person', 0.8, 0.5, 0.6, 0.35, 0.75), w=3456, h=4608),
         'P10', 'soft', 'subject'),
    ]
    fails = 0
    for name, it, lid, expect, src in cases:
        got = ls.coverage_conflict(it, lid, it['width'] or None, it['height'] or None)
        kind = got[0] if got else None
        got_src = got[3] if got and len(got) > 3 else None
        ok = kind == expect and (src is None or got_src == src)
        fails += 0 if ok else 1
        detail = f"kind={kind} src={got_src} deg={got[1]:.0%}" if got else "None"
        print(f"  [{'✓' if ok else '✗'}] {name}: expect={expect}/{src} got {detail}")
    return fails


def test_today_pick():
    """端到端：翻车照 L10 铭牌必须被 fullframe 规则否决，不得再入选。"""
    it = item_with(subj('person', 0.392, 0.4999, 0.5, 0.9948, 1.0))
    it['side'] = '贴纸胡子一贴，娘俩秒变马戏团搭档。'
    pick, info = ls.pick_layout(it, 'landscape', 0.45, img_size=(W, H))
    v10 = info['vetoed'].get('L10', '')
    ok = pick != 'L10' and '人像充满画面' in v10
    print(f"  [{'✓' if ok else '✗'}] 翻车照端到端: pick={pick} tie={info['tie']} "
          f"L10否决={v10[:36]!r}")
    return 0 if ok else 1


def test_backfill_real_photo():
    """集成：真实翻车照（老数据无 face）经惰性补脸后 face 字段出现。"""
    rows = rdp.load_sim_rows()
    it = None
    for r in rows:
        if 'IMG_20171112_083911' in str(r.get('path', '')):
            it = dict(r)
            break
    if it is None:
        print("  [SKIP] 未在库中找到翻车照")
        return 0
    faces_before = ls._face_canvas_boxes(it, 'L10', 2576, 1449)
    print(f"  补脸前 face 框: {faces_before}")
    # _face_canvas_boxes 内部触发 _ensure_face_data → pose 推导
    faces = ls._face_canvas_boxes(it, 'L10', 2576, 1449)
    subs = json.loads(it.get('subjects_json') or '[]')
    n_face = sum(1 for s in subs if s.get('label') == 'person' and s.get('face'))
    print(f"  补脸后 face 字段数: {n_face} / persons="
          f"{sum(1 for s in subs if s.get('label') == 'person')} → 画布脸框 {faces}")
    if n_face == 0:
        print("  [WARN] pose 未派生出脸框（贴纸脸/侧脸可能失败），依赖 fullframe 规则兜底")
    conflict = ls.coverage_conflict(it, 'L10', 2576, 1449)
    ok = conflict is not None and conflict[0] == 'hard'
    print(f"  [{'✓' if ok else '✗'}] 真实照 L10 conflict={conflict}")
    return 0 if ok else 1


if __name__ == '__main__':
    print("== fullframe×补脸 单测 ==")
    f1 = unit_tests()
    print("== 翻车照端到端选型 ==")
    f2 = test_today_pick()
    print("== 真实照惰性补脸集成（含 pose 推理，稍慢）==")
    f3 = test_backfill_real_photo()
    total = f1 + f2 + f3
    print(f"\n结论: 失败={total}")
    sys.exit(1 if total else 0)
