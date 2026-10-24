#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""版式覆盖区×主体冲突选型回归（2026-10-09 二轮泛化版）。

覆盖：glass/veil 覆盖区遮挡、渐变不透明度、least-harm 回退、50 张随机回归。
用法: /home/andy/inktime/venv/bin/python scripts/test_coverage_veto_20261009.py
"""
import sys, json, random, hashlib
sys.path.insert(0, '/home/andy/inktime')
import render_daily_photo as rdp
import layout_strategy as ls

W, H = 6528, 4896  # 4:3 横屏代表尺寸


def subj(label, conf, cx, cy, bw, bh):
    """构造单主体 subjects_json（bbox: cx, cy, w, h 归一化）。"""
    return json.dumps([{'label': label, 'conf': conf,
                        'bbox': [cx, cy, bw, bh]}])


def item_with(subjects, w=W, h=H):
    return {'path': 'x', 'date': '2026-10-09', 'city': '苏州',
            'width': w, 'height': h, 'subjects_json': subjects}


def unit_tests():
    cases = [
        # (名称, item, layout, 期望 None/'hard'/'soft')
        ('主体在上、L01 底卡', item_with(subj('person', 0.9, 0.5, 0.2, 0.3, 0.2)), 'L01', None),
        ('中心入实白铭牌 L10', item_with(subj('person', 0.9, 0.5, 0.85, 0.2, 0.15)), 'L10', 'hard'),
        ('主体沉入底卡 L01', item_with(subj('person', 0.9, 0.5, 0.85, 0.4, 0.4)), 'L01', 'hard'),
        ('轻度重叠 L01→soft', item_with(subj('person', 0.9, 0.5, 0.62, 0.5, 0.35)), 'L01', 'soft'),
        ('无主体', item_with('[]'), 'L01', None),
        ('无尺寸不误杀', item_with(subj('person', 0.9, 0.5, 0.85, 0.2, 0.15), w=0, h=0), 'L01', None),
        ('veil 顶部轻微(P03 上部)', item_with(subj('person', 0.9, 0.5, 0.2, 0.3, 0.2)), 'P03', None),
        ('veil 底部重遮(P03 下部)', item_with(subj('person', 0.9, 0.5, 0.85, 0.25, 0.2)), 'P03', 'hard'),
        ('L02 左栏压主体', item_with(subj('person', 0.9, 0.15, 0.5, 0.2, 0.4)), 'L02', 'hard'),
        ('L02 主体在右不冲突', item_with(subj('person', 0.9, 0.7, 0.5, 0.3, 0.4)), 'L02', None),
        ('L06 顶签压上部主体', item_with(subj('person', 0.9, 0.25, 0.15, 0.3, 0.3)), 'L06', 'hard'),
        ('P10 veil 强渐变段重遮', item_with(subj('person', 0.9, 0.5, 0.85, 0.25, 0.2)), 'P10', 'hard'),
    ]
    fails = 0
    for name, it, lid, expect in cases:
        got = ls.coverage_conflict(it, lid, it['width'] or None, it['height'] or None)
        kind = got[0] if got else None
        ok = kind == expect
        fails += 0 if ok else 1
        detail = f"occ={got[1]:.0%} deg={got[1]:.2f}" if got else ''
        print(f"  [{'✓' if ok else '✗'}] {name}: expect={expect} got={kind} {detail}")
    return fails


def test_least_harm():
    """全版式被否时的最小伤害回退：主体铺满底缘+左侧，理想版式不存在。"""
    it = item_with(subj('person', 0.9, 0.4, 0.8, 0.9, 0.35))
    layout, info = ls.pick_layout(it, 'landscape', 0.45, img_size=(W, H))
    degs = {k: ls.coverage_conflict(it, k, W, H) for k in
            ('L01', 'L02', 'L04', 'L05', 'L06', 'L07', 'L10')}
    hard = {k for k, v in degs.items() if v and v[0] == 'hard'}
    picked_deg = degs.get(layout)
    print(f"  least-harm: 选型={layout} tie={info['tie']} hard集={sorted(hard)}")
    print(f"  各版式: { {k: (v[0], round(v[1], 2)) if v else None for k, v in degs.items()} }")
    if picked_deg is None:
        ok = True
    else:
        min_hard = min((v[1] for k, v in degs.items() if v and v[0] == 'hard'), default=9)
        ok = picked_deg[0] != 'hard' or abs(picked_deg[1] - min_hard) < 0.02
    print(f"  [{'✓' if ok else '✗'}] least-harm 回退符合最小伤害")
    return 0 if ok else 1


def regression(n=50, seed=20260912):
    random.seed(seed)
    rows = rdp.load_sim_rows()
    sample = random.sample(rows, min(n, len(rows)))
    from collections import Counter
    old_dist, new_dist = Counter(), Counter()
    changes, errors, lh_count = [], 0, 0
    for it0 in sample:
        item = dict(it0)
        try:
            w, h = int(item.get('width') or 0), int(item.get('height') or 0)
            orient = 'portrait' if h > w else 'landscape'
            img_size = (w, h) if w and h else None
            wr = 0.45
            new_pick, info = ls.pick_layout(item, orient, wr, img_size=img_size)
            # 复原旧选型（几何否决保留、覆盖区判定摘除）
            g = ls.extract_signals(item, wr)
            scores = ls._score_landscape(g) if orient == 'landscape' else ls._score_portrait(g)
            final, vetoed = dict(scores), {}
            for k, fn in (ls._VETO_LANDSCAPE if orient == 'landscape' else ls._VETO_PORTRAIT).items():
                if fn(g):
                    final[k] = float('-inf')
            for k in scores:
                if final.get(k) == float('-inf'):
                    continue
                if ls.geometry_veto_reason(item, k, *(img_size or (None, None))):
                    final[k] = float('-inf')
            top = max(final.values())
            if top == float('-inf') or top <= 0:
                old_pick = 'P01' if orient == 'portrait' else 'L01'
            else:
                best = sorted(k for k, v in final.items() if v == top)
                old_pick = best[0] if len(best) == 1 else best[
                    int(hashlib.md5(f"{item.get('path','')}|{item.get('date','')}"
                                    .encode()).hexdigest(), 16) % len(best)]
            old_dist[old_pick] += 1
            new_dist[new_pick] += 1
            if info.get('tie') == 'least-harm':
                lh_count += 1
            if new_pick != old_pick:
                changes.append((item['path'].split('\\')[-1][:44], old_pick, new_pick,
                                info.get('tie'),
                                ls.coverage_conflict(item, old_pick, w, h) or
                                ls.geometry_veto_reason(item, old_pick, w, h) or 'rule-veto'))
        except Exception as e:
            errors += 1
            print(f"  [ERR] {item.get('path','?')}: {type(e).__name__}: {e}")
    print(f"\n样本={len(sample)} 错误={errors} least-harm 回退={lh_count}")
    print(f"选型变化: {len(changes)}/{len(sample)}")
    for p, o, nw, tie, why in changes:
        print(f"  {o}→{nw} ({tie}) | {p} | 旧版式判定={why}")
    print(f"\n旧分布: {dict(sorted(old_dist.items()))}")
    print(f"新分布: {dict(sorted(new_dist.items()))}")
    # 不变量：被 coverage/geometry 否决的版式绝不能出现在新选型——
    # 例外：tie=least-harm（全版式冲突时的设计内最小伤害降级）
    bad = 0
    for it0 in sample:
        item = dict(it0)
        w, h = int(item.get('width') or 0), int(item.get('height') or 0)
        orient = 'portrait' if h > w else 'landscape'
        try:
            pick, info = ls.pick_layout(item, orient, 0.45,
                                        img_size=(w, h) if w and h else None)
            if info.get('tie') == 'least-harm':
                continue
            for k in info.get('vetoed', {}):
                if info['vetoed'][k].startswith(('coverage:', 'geometry:')) and pick == k:
                    bad += 1
                    print(f"  [INVARIANT✗] {k} 被否决却被选中: {item['path']}")
        except Exception:
            pass
    print(f"不变量检查(否决≠选中, least-harm 豁免): 违例={bad}")
    return changes, errors, bad


if __name__ == '__main__':
    print("== 覆盖区冲突单测 ==")
    f = unit_tests()
    f += test_least_harm()
    print("\n== 50 张随机回归 (seed=20260912) ==")
    changes, errors, bad = regression()
    print(f"\n结论: 单测/不变量失败={f + bad} 回归错误={errors} 选型变化={len(changes)}")
    sys.exit(1 if (f or errors or bad) else 0)
