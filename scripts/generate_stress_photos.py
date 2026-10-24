#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成编辑器/迁移验证用的压力测试照片（合成、确定性、无隐私负担）。

六种难例 × 横竖两向：亮底/暗底/中灰/彩噪/暖渐变/冷渐变，
输出到 static/style_editor/stress/，尺寸即画布尺寸（480×800 / 800×480）。

用法：venv/bin/python scripts/generate_stress_photos.py
"""

from __future__ import annotations

import random
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "static" / "style_editor" / "stress"


def _bright(w: int, h: int) -> Image.Image:
    img = Image.new("RGB", (w, h), (245, 243, 238))
    d = ImageDraw.Draw(img)
    for i in range(w + h):
        v = 245 - 18 * i / (w + h)
        d.line([(0, i), (w, i)], fill=(int(v), int(v), int(v - 3)))
    return img


def _dark(w: int, h: int) -> Image.Image:
    img = Image.new("RGB", (w, h), (22, 22, 26))
    d = ImageDraw.Draw(img)
    for i in range(w + h):
        v = 22 + 20 * i / (w + h)
        d.line([(0, i), (w, i)], fill=(int(v), int(v), int(v + 4)))
    return img


def _gray(w: int, h: int) -> Image.Image:
    img = Image.new("RGB", (w, h), (128, 128, 128))
    rng = random.Random(42)
    px = img.load()
    for y in range(h):
        for x in range(w):
            n = rng.randint(-10, 10)
            px[x, y] = (128 + n, 128 + n, 128 + n)
    return img


def _noise(w: int, h: int) -> Image.Image:
    img = Image.new("RGB", (w, h))
    rng = random.Random(7)
    px = img.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = (rng.randint(0, 255), rng.randint(0, 255), rng.randint(0, 255))
    return img


def _warm(w: int, h: int) -> Image.Image:
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / h
        d.line([(0, y), (w, y)], fill=(int(240 - 60 * t), int(180 - 90 * t), int(90 - 60 * t)))
    return img


def _cool(w: int, h: int) -> Image.Image:
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / h
        d.line([(0, y), (w, y)], fill=(int(30 + 30 * t), int(50 + 50 * t), int(90 + 100 * t)))
    return img


PATTERNS = {"bright": _bright, "dark": _dark, "gray": _gray,
            "noise": _noise, "warm": _warm, "cool": _cool}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, fn in PATTERNS.items():
        for o, (w, h) in (("p", (480, 800)), ("l", (800, 480))):
            fn(w, h).save(OUT / f"{name}_{o}.jpg", quality=92)
    print(f"[DONE] 压力照片 {len(PATTERNS) * 2} 张 → {OUT}")


if __name__ == "__main__":
    main()
