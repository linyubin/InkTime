#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
文字版式绘制原语 + 数据化版式入口（2026-09-24 版式数据化迁移，ADR 0002）。

本模块保留所有绘制原语（_glass/_scrim/_seal/_draw_dark/_fit_text…）供
style_runtime 解释 layouts/*.json 时复用；入口 render_layout 按版式 ID 从
注册表取样式数据交 style_runtime 绘制。12 款在册版式的转写以
scripts/validate_style_migration.py 逐像素验收（864/864 一致）。

几何坐标与 docs/layout-showcase-2026-09-11/index.html 逐一对应：
- 展示页 mockup 按真实画布 1:1 标注，竖屏 480×800 / 横屏 800×480；
- 竖屏 y 坐标按相框遮挡换算：y_canvas = y_mockup - 40 + FRAME_MASK_INSET_PX；
- 横屏 x 坐标同理：x_canvas = x_mockup - 40 + FRAME_MASK_INSET_PX；
- layouts/*.json 存的是换算后的画布坐标（FRAME_MASK_INSET_PX=40 时与展示页同值）；
- 遵循展示页「抖动三条铁律」：半透明白 alpha≥184、无深色描边、
   立体感 = 外投影 + 白高光边。

入口：render_layout(canvas, layout_id, item)
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageFont

import config as cfg
from layout_strategy import LAYOUT_NAMES, seal_glyphs_for_city

# ---------- 常量 ----------
INSET = int(getattr(cfg, "FRAME_MASK_INSET_PX", 40) or 0)

WHITE = (255, 255, 255)
WHITE_DIM = (228, 226, 222)      # ≈88% 白，压在深色 scrim 上
VEIL_RGB = (246, 241, 231)       # 浅色渐变米白（P03/P08/P10/L04/L07 底托）
# 渐变峰值不透明度（config: TEXT_VEIL_OPACITY），1.0=实色；0.85=留 15% 照片透出
VEIL_ALPHA = int(255 * float(getattr(cfg, "TEXT_VEIL_OPACITY", 0.85)))
META_GRAY = (96, 90, 82)         # 浅底上的次要文字（日期/地点）
BLACK = (0, 0, 0)
INK_BAR = (12, 12, 12)           # 墨条黑
SEAL_RED = (200, 0, 0)           # 与 PALETTE 红 #C80000 一致
ACCENT_YELLOW = (220, 180, 0)    # 与 PALETTE 黄 #DCB400 一致
GRAY_LINE = (216, 216, 216)      # 分隔浅灰（深色竖线抖动会变黑实线，禁用）
GRAY_TEXT = (85, 85, 85)

_FONT_PATH = Path(str(getattr(cfg, "FONT_PATH", "") or "")).expanduser()
if str(_FONT_PATH) and not _FONT_PATH.is_absolute():
    _FONT_PATH = (Path(__file__).resolve().parent / _FONT_PATH).resolve()

_FONT_CACHE: Dict[Tuple[Optional[str], int], ImageFont.FreeTypeFont] = {}
_SEAL_GLYPH_DIR = Path(__file__).resolve().parent / "seal_glyphs"

# 版式 JSON 的 font 字段可选字体（None=跟随 config.FONT_PATH，存量版式均为 None）
_FONT_FILES = {"smiley": "SmileySans-Oblique.ttf", "lxgw": "LXGWHeartSerifMN.ttf"}


def _get_font(size: int, font_id: Optional[str] = None) -> ImageFont.FreeTypeFont:
    key = (font_id, size)
    if key not in _FONT_CACHE:
        font = None
        if font_id in _FONT_FILES:
            p = Path(__file__).resolve().parent / "font" / _FONT_FILES[font_id]
            if p.exists():
                try:
                    font = ImageFont.truetype(str(p), size)
                except Exception:
                    font = None
        if font is None and _FONT_PATH and Path(_FONT_PATH).exists():
            try:
                font = ImageFont.truetype(str(_FONT_PATH), size)
            except Exception:
                font = None
        if font is None:
            import os
            if os.name == "nt":
                for fb in (r"C:\Windows\Fonts\msyh.ttc",
                           r"C:\Windows\Fonts\simsun.ttc",
                           r"C:\Windows\Fonts\simhei.ttf"):
                    if os.path.exists(fb):
                        try:
                            font = ImageFont.truetype(fb, size)
                            break
                        except Exception:
                            continue
        if font is None:
            font = ImageFont.load_default()
        _FONT_CACHE[key] = font
    return _FONT_CACHE[key]


# ---------- 小工具 ----------
def _py(y: float) -> float:
    """竖屏展示页 y → 画布 y（顶部锚定相框遮挡换算）。"""
    return y - 40 + INSET


def _lx(x: float) -> float:
    """横屏展示页 x → 画布 x（左侧锚定相框遮挡换算）。"""
    return x - 40 + INSET


def weekday_cn(date_str: str) -> str:
    try:
        y, m, d = (int(v) for v in str(date_str).split("-")[:3])
        return "星期" + "日一二三四五六"[dt.date(y, m, d).weekday()]
    except Exception:
        return ""


def _date_parts(item: Dict[str, Any]) -> Tuple[str, str, str]:
    """(完整日期 2024.9.22, 年 2024, 月日 9.22)"""
    from render_daily_photo import format_date_display
    full = format_date_display(item.get("date", ""))
    year, _, rest = full.partition(".")
    return full, year, rest or full


def _meta_segments(item: Dict[str, Any], with_weekday: bool = True) -> str:
    """'2023.6.1 · 星期四 · 厦门市'，空字段自动省略。"""
    from render_daily_photo import format_date_display
    segs = [format_date_display(item.get("date", ""))]
    if with_weekday:
        wd = weekday_cn(item.get("date", ""))
        if wd:
            segs.append(wd)
    city = str(item.get("city") or "").strip()
    if city:
        segs.append(city)
    return " · ".join(segs)


def _fit_text(draw: ImageDraw.ImageDraw, text: str, font, max_w: float) -> str:
    """超宽截断加省略号（单行纪律）。"""
    if not text:
        return ""
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text + "…"


def _wrap_lines(canvas, text: str, font, max_w: float, max_lines: int) -> List[str]:
    from render_daily_photo import wrap_text_chinese
    return wrap_text_chinese(ImageDraw.Draw(canvas), text, font, max_w, max_lines)


def _draw_dark(canvas, x: float, y: float, s: str, size: int,
               fill=BLACK, stroke: float = 0.3,
               align: str = "l", cx: float = None, bold: bool = False,
               font_id: Optional[str] = None) -> None:
    """深色文字（毛玻璃/实白/浅色渐变底上），同色描边模拟加粗。"""
    d = ImageDraw.Draw(canvas)
    font = _get_font(size, font_id)
    w = d.textlength(s, font=font)
    if align == "c":
        x = (cx if cx is not None else x) - w / 2
    elif align == "r":
        x = (cx if cx is not None else x) - w
    if bold:
        stroke = max(stroke, 0.6)
    d.text((x, y), s, font=font, fill=fill,
           stroke_width=stroke, stroke_fill=fill)


def _draw_white(canvas, specs: List[Dict[str, Any]]) -> None:
    """
    白字批量绘制（一次合成，带柔和投影替代 CSS text-shadow）。
    spec: x, y, text, size, align(l/c/r), dim, cx(居中/右对齐参考线)
    """
    if not specs:
        return
    ov = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    for sp in specs:
        s = sp["text"]
        if not s:
            continue
        font = _get_font(sp.get("size", 18))
        w = d.textlength(s, font=font)
        align = sp.get("align", "l")
        x = sp["x"]
        if align == "c":
            x = sp.get("cx", x) - w / 2
        elif align == "r":
            x = sp.get("cx", x) - w
        fill = WHITE_DIM if sp.get("dim") else WHITE
        d.text((x, sp["y"] + 2), s, font=font, fill=(0, 0, 0, 120))
        d.text((x, sp["y"]), s, font=font, fill=fill + (255,))
    canvas.paste(ov.convert("RGB"), (0, 0), ov)


def _scrim(canvas, x: int, y: int, w: int, h: int,
           a0: int = 0, a1: int = 235, ramp_px: int = None,
           rgb: Tuple[int, int, int] = (0, 0, 0)) -> None:
    """
    竖向黑色渐变，a 取 0–255。
    ramp_px: 自顶部起用 smoothstep 曲线在 ramp_px 内升至 a1，其后平推到底。
    smoothstep 两端导数为零——斜坡与平台交接处无折角、起始处无突跳，
    消除线性斜坡在暗照片上的"生硬感"（2026-09-12 二次调整）。
    """
    ov = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    p = ramp_px if ramp_px else h
    for yy in range(h):
        if yy < p:
            t = yy / p
            k = t * t * (3.0 - 2.0 * t)
        else:
            k = 1.0
        a = a0 + (a1 - a0) * k
        if a > 0:
            d.line([(0, yy), (w, yy)], fill=(rgb[0], rgb[1], rgb[2], int(a)))
    canvas.paste(ov.convert("RGB"), (x, y), ov)


def _glass(canvas, box, radius: int = 14, alpha: int = 184, blur: int = 12,
           shadow: bool = False, highlight: bool = True) -> None:
    """毛玻璃圆角卡：外投影 + 模糊 + 白色叠加 + 白高光边（铁律：无深色描边）。"""
    x0, y0, x1, y1 = (int(v) for v in box)
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0:
        return
    if shadow:
        shl = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        ImageDraw.Draw(shl).rounded_rectangle(
            (x0 + 6, y0 + 6, x1 + 6, y1 + 6), radius=radius, fill=(0, 0, 0, 95))
        shl = shl.filter(ImageFilter.GaussianBlur(8))
        canvas.paste(shl.convert("RGB"), (0, 0), shl)
    crop = canvas.crop((x0, y0, x1, y1)).filter(ImageFilter.GaussianBlur(blur))
    ov = Image.new("RGBA", (w, h), (255, 255, 255, alpha))
    glass = Image.alpha_composite(crop.convert("RGBA"), ov)
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=255)
    canvas.paste(glass.convert("RGB"), (x0, y0), mask)
    if highlight:
        ImageDraw.Draw(canvas).rounded_rectangle(
            (x0, y0, x1, y1), radius=radius, outline=WHITE, width=1)


def _seal(canvas, x: int, y: int, w: int, h: int, city: str,
          vertical: bool = False, glyph_px: int = 26, radius: int = 6) -> None:
    """钤印：红底圆角块 + 小篆字形（缺篆形回退为字体文字；无城市不画）。"""
    if not str(city or "").strip():
        return
    d = ImageDraw.Draw(canvas)
    d.rounded_rectangle((x, y, x + w, y + h), radius=radius, fill=SEAL_RED)
    name = str(city or "").strip()
    for suf in ("市", "镇", "县", "区"):
        if name.endswith(suf) and len(name) > len(suf):
            name = name[:-len(suf)]
            break
    hexes = seal_glyphs_for_city(city)
    if hexes:
        gap = max(2, glyph_px // 8)
        total = glyph_px * len(hexes) + gap * (len(hexes) - 1)
        if vertical:
            pos = (x + (w - glyph_px) / 2, y + (h - total) / 2)
        else:
            pos = (x + (w - total) / 2, y + (h - glyph_px) / 2)
        for i, hx in enumerate(hexes):
            g = Image.open(_SEAL_GLYPH_DIR / f"{hx}.png").convert("RGBA")
            g = g.resize((glyph_px, glyph_px), Image.LANCZOS)
            gx, gy = pos
            if vertical:
                canvas.paste(g, (int(gx), int(gy + i * (glyph_px + gap))), g)
            else:
                canvas.paste(g, (int(gx + i * (glyph_px + gap)), int(gy)), g)
    else:
        font = _get_font(max(12, glyph_px - 4))
        if vertical and len(name) > 1:
            pitch = h / (len(name) + 0.4)
            for i, ch in enumerate(name[:4]):
                d.text((x + w / 2, y + pitch * (i + 0.55)), ch, font=font,
                       fill=WHITE, anchor="mm")
        else:
            s = name if len(name) <= 3 else name[:2]
            f2 = font
            while d.textlength(s, font=f2) > w - 8 and f2.size > 11:
                f2 = _get_font(f2.size - 1)
            d.text((x + w / 2, y + h / 2), s, font=f2, fill=WHITE, anchor="mm")


def _city(item: Dict[str, Any]) -> str:
    return str(item.get("city") or "").strip()


# ==================================================================
# 竖屏 6 款（480×800，展示页 y - 40 + INSET）
# ==================================================================
# 版式注册表与入口（ADR 0002：版式数据化，layouts/*.json + style_runtime 解释渲染）
# ==================================================================
def render_layout(canvas: Image.Image, layout: str, item: Dict[str, Any]) -> None:
    """按版式 ID 在已裁切好的画布上绘制文字层。

    版式定义为 layouts/<ID>.json（声明式数据），由 style_runtime 复用本模块的
    绘制原语解释渲染；旧"一版式一函数"实现于 2026-09-24 迁移退役，
    迁移验收：scripts/validate_style_migration.py 864/864 逐像素一致。
    """
    import style_registry
    import style_runtime
    style = style_registry.get_style(layout)
    if style is None:
        raise ValueError(f"未知版式: {layout}")
    style_runtime.render_style(canvas, style, item)
