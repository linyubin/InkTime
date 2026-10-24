#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
版式数据化迁移验收（ADR 0002）：layouts/*.json + style_runtime vs 旧逐款函数。

对每款在册版式 × 每张压力照片 × 每组文案样本，新旧两套实现各画一份，
ImageChops.difference 逐像素比对，任何差异即失败（复用同一批 Pillow 原语，
理论上应零差异）。首个失败样本输出并排对照图便于定位。

用法：venv/bin/python scripts/validate_style_migration.py [--keep-failures]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageChops

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import render_layouts  # noqa: E402
import style_registry  # noqa: E402
import style_runtime   # noqa: E402

STRESS_DIR = Path(__file__).resolve().parent.parent / "static" / "style_editor" / "stress"
OUT_DIR = Path(__file__).resolve().parent.parent / "docs" / "style-migration-validation"

# 文案样本：覆盖 空文案 / 短句 / 中长 / 超长（触发折行与省略号截断）、
# 有无城市（钤印绘制与否、L07 让位）、有/无小篆字形的城市（钤印回退路径）。
ITEMS = [
    {"side": "山间的清晨雾气还未散去", "date": "2026-09-24", "city": "厦门市"},
    {"side": "短句", "date": "2026-09-24", "city": "厦门市"},
    {"side": "", "date": "2026-01-01", "city": ""},
    {"side": "这是一条特别长的文案用来测试折行与省略号截断在两套渲染器之间保持逐像素一致的边界场景再加几个字", "date": "2025-12-31", "city": ""},
    {"side": "古镇的石桥与灯笼", "date": "2026-09-24", "city": "泉州市"},
    {"side": "海边的风", "date": "2026-03-08", "city": "木渎镇"},
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-failures", action="store_true",
                    help="把失败样本的并排对照图写入 docs/style-migration-validation/")
    args = ap.parse_args()

    stress = sorted(STRESS_DIR.glob("*.jpg"))
    if not stress:
        print("[ERROR] 没有压力照片，先跑 scripts/generate_stress_photos.py")
        sys.exit(2)

    old_renderers = {**getattr(render_layouts, "PORTRAIT_RENDERERS", {}),
                     **getattr(render_layouts, "LANDSCAPE_RENDERERS", {})}
    if not old_renderers:
        print("[SKIP] 旧逐款函数已退役（阶段②切换完成）。迁移验收已于 2026-09-24 "
              "通过：864/864 逐像素一致。本脚本仅在回退到旧实现时需要重跑。")
        sys.exit(0)
    styles = style_registry.all_styles()
    missing = set(old_renderers) - set(styles)
    if missing:
        print(f"[ERROR] layouts/ 缺少版式: {sorted(missing)}")
        sys.exit(2)

    failures = []
    n_cases = 0
    for lid in sorted(old_renderers):
        style = styles[lid]
        w, h = (480, 800) if style["orientation"] == "portrait" else (800, 480)
        for photo in stress:
            base = Image.open(photo).convert("RGB")
            if base.size != (w, h):
                base = base.resize((w, h), Image.LANCZOS)
            for i, item in enumerate(ITEMS):
                n_cases += 1
                a = base.copy(); old_renderers[lid](a, item)
                b = base.copy(); style_runtime.render_style(b, style, item)
                bbox = ImageChops.difference(a, b).getbbox()
                if bbox is not None:
                    failures.append((lid, photo.name, i, bbox))
                    print(f"[DIFF] {lid} × {photo.name} × item#{i} → bbox={bbox}")
                    if args.keep_failures and len(failures) <= 6:
                        OUT_DIR.mkdir(parents=True, exist_ok=True)
                        side = Image.new("RGB", (w * 2 + 10, h), (255, 0, 255))
                        side.paste(a, (0, 0)); side.paste(b, (w + 10, 0))
                        side.save(OUT_DIR / f"fail_{lid}_{photo.stem}_i{i}.png")
                else:
                    print(f"[ OK ] {lid} × {photo.stem} × item#{i}")

    print(f"\n[RESULT] {n_cases - len(failures)}/{n_cases} 一致，"
          f"失败 {len(failures)} 组" + (f" → {OUT_DIR}" if failures and args.keep_failures else ""))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
