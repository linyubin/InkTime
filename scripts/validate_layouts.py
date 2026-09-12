#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
版式选型 pipeline 批量验证：随机抽 N 张照片走完整渲染链路
（选型 → cover 裁切 → 文字层 → gamma → 四色抖动），产出 HTML 报告。

用法：
    venv/bin/python scripts/validate_layouts.py [--count 50] [--seed 20260912] [--out docs/layout-validation]

报告内容：每张照片的原图缩略 / 成品渲染 / 抖动近似、选中版式、信号与打分明细、
否决项；页首汇总版式分布与耗时。随机种子写入报告，可复现。
"""

from __future__ import annotations

import argparse
import html
import json
import random
import shutil
import time
from pathlib import Path

from PIL import Image

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import render_daily_photo as rdp  # noqa: E402
from layout_strategy import LAYOUT_NAMES  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=50)
    ap.add_argument("--seed", type=int, default=20260912)
    ap.add_argument("--out", default="docs/layout-validation")
    args = ap.parse_args()

    out_dir = Path(args.out).resolve()
    img_dir = out_dir / "img"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    img_dir.mkdir(parents=True, exist_ok=True)

    items = rdp.load_sim_rows()
    rng = random.Random(args.seed)
    rng.shuffle(items)
    print(f"[INFO] 库内候选 {len(items)} 张，随机种子 {args.seed}，目标 {args.count} 张")

    results, failures = [], []
    t_all = time.time()
    for item in items:
        if len(results) >= args.count:
            break
        idx = len(results)
        t0 = time.time()
        try:
            canvas = rdp.render_image(item)  # auto 朝向 + 选型
            layout = canvas.info.get("layout", "?")
            info = canvas.info.get("layout_info", {})
            orientation = canvas.info.get("render_orientation", "portrait")

            gamma, g_mode = rdp.choose_gamma(canvas)
            dithered = rdp.quantize_four_color(rdp.apply_gamma(canvas, gamma)).convert("RGB")

            # 缩略保存（报告引用）
            rgb_thumb = canvas.copy(); rgb_thumb.thumbnail((320, 534))
            dither_thumb = dithered.copy(); dither_thumb.thumbnail((320, 534))
            name = f"{idx:02d}"
            rgb_thumb.save(img_dir / f"{name}_rgb.jpg", quality=82)
            dither_thumb.save(img_dir / f"{name}_dither.jpg", quality=82)

            g = info.get("signals", {})
            results.append({
                "idx": idx, "layout": layout, "orientation": orientation,
                "path": item["path"], "date": item.get("date", ""),
                "city": item.get("city", ""), "caption": item.get("side", ""),
                "seconds": time.time() - t0,
                "gamma": round(gamma, 3), "gamma_mode": g_mode,
                "signals": {
                    "white_ratio": round(g.get("white_ratio", 0), 3),
                    "S": round(g.get("S", 0), 3),
                    "cx": round(g.get("cx", .5), 2), "cy": round(g.get("cy", .5), 2),
                    "has_subject": g.get("has_subject", False),
                    "tags": [k for k, v in (g.get("tags") or {}).items() if v],
                },
                "scores": info.get("scores", {}),
                "final": info.get("final", {}),
                "vetoed": info.get("vetoed", {}),
                "tie": info.get("tie", ""),
            })
            print(f"[{idx + 1}/{args.count}] {layout} {orientation} "
                  f"| I={results[-1]['signals']['white_ratio']:.2f} "
                  f"| {results[-1]['seconds']:.1f}s | {Path(item['path']).name}")
        except Exception as e:
            failures.append({"path": item["path"], "error": repr(e)[:200]})
            print(f"[SKIP] {Path(item['path']).name}: {repr(e)[:100]}")

    total_t = time.time() - t_all
    write_report(out_dir, results, failures, args, total_t)
    print(f"[DONE] 成功 {len(results)} / 失败 {len(failures)} | 总耗时 {total_t:.0f}s")
    print(f"[DONE] 报告: {out_dir / 'index.html'}")


def write_report(out_dir: Path, results, failures, args, total_t: float) -> None:
    # 版式分布
    dist: dict = {}
    for r in results:
        dist[r["layout"]] = dist.get(r["layout"], 0) + 1
    max_dist = max(dist.values()) if dist else 1
    dist_rows = "".join(
        f'<tr><td class="mono">{html.escape(k)}</td><td>{LAYOUT_NAMES.get(k, "")}</td>'
        f'<td>{v}</td><td><span class="bar" style="width:{40 * v / max_dist}px"></span>'
        f'{100 * v / max(1, len(results)):.0f}%</td></tr>'
        for k, v in sorted(dist.items()))

    cards = []
    for r in results:
        g = r["signals"]

        def _score_cell(k, v, r=r):
            if k in r["vetoed"]:
                return f'<tr class="veto"><td class="mono">{k}</td><td>否决</td></tr>'
            val = r["final"].get(k, v)
            return f'<tr><td class="mono">{k}</td><td>{val:+.2f}</td></tr>'

        score_rows = "".join(_score_cell(k, v) for k, v in
                             sorted(r["scores"].items(), key=lambda kv: -kv[1]))
        tags = "、".join(g["tags"]) or "—"
        subj = f'({g["cx"]},{g["cy"]})' if g["has_subject"] else "无"
        cards.append(f"""
<figure class="card">
  <div class="head"><span class="badge {'p' if r['layout'].startswith('P') else 'l'}">{r['layout']}</span>
    <span class="lname">{LAYOUT_NAMES.get(r['layout'], '')}</span>
    <span class="ori">{r['orientation']}</span><span class="meta">{r['seconds']:.1f}s · γ{r['gamma']}</span></div>
  <div class="imgs">
    <div><img src="img/{r['idx']:02d}_rgb.jpg"><figcaption>渲染成品</figcaption></div>
    <div><img src="img/{r['idx']:02d}_dither.jpg"><figcaption>四色抖动</figcaption></div>
  </div>
  <div class="detail">
    <p class="cap">「{html.escape(r['caption'] or '（无文案）')}」</p>
    <p class="sub">{html.escape(r['date'])} · {html.escape(r['city'] or '无城市')} · I(白率)={g['white_ratio']} · 主体 {subj} · 标签 {tags}</p>
    <table class="scores">{score_rows}</table>
    <p class="sub">tie: {r['tie']}</p>
  </div>
</figure>""")

    fail_html = ""
    if failures:
        rows = "".join(f"<li class='mono'>{html.escape(f['path'])}<br>{html.escape(f['error'])}</li>"
                       for f in failures)
        fail_html = f"<h2>失败 {len(failures)} 张</h2><ul class='fails'>{rows}</ul>"

    page = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<title>版式选型 pipeline 验证报告 · {len(results)} 张</title><style>
  body {{ font-family: 'LXGW','Noto Serif CJK SC',serif; background:#f5f2ec; color:#2b2620; margin:0; padding-bottom:60px; }}
  header {{ background:#211d18; color:#f2ede4; padding:30px 5vw; }}
  header h1 {{ font-size:24px; margin:0 0 8px; }}
  header p {{ color:#b9b0a2; font-size:13.5px; line-height:1.8; margin:4px 0; }}
  .stats {{ display:flex; flex-wrap:wrap; gap:24px; margin-top:14px; font-size:13px; color:#d8d0c2; }}
  .stats b {{ color:#dcb400; font-size:17px; }}
  main {{ padding:26px 5vw; }}
  h2 {{ font-size:18px; margin:10px 0 12px; }}
  table.dist {{ border-collapse:collapse; font-size:13px; }}
  .dist td,.dist th {{ border:1px solid #e4ddd0; padding:5px 12px; text-align:left; }}
  .bar {{ display:inline-block; height:10px; background:#c80000; margin-right:8px; vertical-align:middle; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(560px,1fr)); gap:22px; margin-top:18px; }}
  .card {{ background:#fff; border-radius:12px; padding:14px 16px; box-shadow:0 4px 16px rgba(43,38,32,.12); margin:0; }}
  .head {{ display:flex; align-items:center; gap:10px; margin-bottom:10px; }}
  .badge {{ color:#fff; border-radius:6px; padding:2px 10px; font-weight:700; font-size:14px; }}
  .badge.p {{ background:#c80000; }} .badge.l {{ background:#211d18; }}
  .lname {{ font-weight:700; }} .ori {{ color:#6f675c; font-size:12px; }}
  .meta {{ margin-left:auto; color:#9a9184; font-size:12px; }}
  .imgs {{ display:flex; gap:12px; }}
  .imgs div {{ flex:1; text-align:center; }}
  .imgs img {{ max-width:100%; border-radius:6px; }}
  figcaption {{ font-size:11.5px; color:#9a9184; margin-top:4px; }}
  .detail {{ font-size:12.5px; line-height:1.7; margin-top:8px; }}
  .cap {{ margin:2px 0; }} .sub {{ color:#6f675c; margin:2px 0; }}
  .scores {{ border-collapse:collapse; font-size:11.5px; margin:4px 0; }}
  .scores td {{ border:1px solid #eee5d8; padding:1px 8px; }}
  .scores tr.veto td {{ color:#c80000; text-decoration:line-through; }}
  .mono {{ font-family:Consolas,monospace; font-size:11.5px; word-break:break-all; }}
  ul.fails {{ font-size:12px; color:#8a3b12; }}
  footer {{ text-align:center; color:#9a9184; font-size:12px; margin-top:36px; }}
</style></head><body>
<header><h1>版式选型 pipeline 验证报告</h1>
<p>随机抽样 {len(results)} 张（seed={args.seed}，可复现）· 完整链路：选型 → cover 裁切（含版式避让）→ 文字层 → gamma → 四色抖动</p>
<div class="stats">
  <span>成功 <b>{len(results)}</b></span><span>失败 <b>{len(failures)}</b></span>
  <span>竖屏 <b>{sum(1 for r in results if r['orientation']=='portrait')}</b> /
        横屏 <b>{sum(1 for r in results if r['orientation']=='landscape')}</b></span>
  <span>平均 <b>{total_t / max(1, len(results)):.1f}s</b> / 张</span>
  <span>总耗时 <b>{total_t:.0f}s</b></span>
</div></header>
<main>
<h2>版式分布</h2>
<table class="dist"><tr><th>版式</th><th>名称</th><th>张数</th><th>占比</th></tr>{dist_rows}</table>
<h2>逐张明细</h2>
<div class="grid">{''.join(cards)}</div>
{fail_html}
<footer>InkTime 版式选型验证 · seed={args.seed} · 渲染 {total_t:.0f}s · 抖动为四色墨水屏近似</footer>
</main></body></html>"""
    (out_dir / "index.html").write_text(page, encoding="utf-8")
    # 摘要 json 便于后续统计
    (out_dir / "summary.json").write_text(json.dumps(
        {"seed": args.seed, "count": len(results), "failures": failures,
         "distribution": dist, "total_seconds": round(total_t, 1)},
        ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
