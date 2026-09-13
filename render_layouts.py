#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
14 款文字版式的 PIL 渲染实现（2026-09-12 定稿）。

几何坐标与 docs/layout-showcase-2026-09-11/index.html 逐一对应：
- 展示页 mockup 按真实画布 1:1 标注，竖屏 480×800 / 横屏 800×480；
- 竖屏 y 坐标按相框遮挡换算：y_canvas = y_mockup - 40 + FRAME_MASK_INSET_PX；
- 横屏 x 坐标同理：x_canvas = x_mockup - 40 + FRAME_MASK_INSET_PX；
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
VEIL_RGB = (250, 247, 240)       # 浅色渐变米白（P03/P08/P10/L04/L07 底托）
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

_FONT_CACHE: Dict[Tuple[str, int], ImageFont.FreeTypeFont] = {}
_SEAL_GLYPH_DIR = Path(__file__).resolve().parent / "seal_glyphs"


def _get_font(size: int) -> ImageFont.FreeTypeFont:
    key = ("lxgw", size)
    if key not in _FONT_CACHE:
        font = None
        if _FONT_PATH and Path(_FONT_PATH).exists():
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
               align: str = "l", cx: float = None, bold: bool = False) -> None:
    """深色文字（毛玻璃/实白/浅色渐变底上），同色描边模拟加粗。"""
    d = ImageDraw.Draw(canvas)
    font = _get_font(size)
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
def _p01(canvas, item) -> None:
    """底部玻璃卡·基线：卡片自适应文案行数，锚进可视区。"""
    caption = item.get("side") or ""
    font_cap = _get_font(22)
    lines = _wrap_lines(canvas, caption, font_cap, 432, 2) if caption else []
    card_h = 15 + len(lines) * 30 + 27 + 14 if lines else 55
    y1 = _py(750)
    y0 = y1 - card_h
    _glass(canvas, (10, y0, 470, y1), radius=16, alpha=184, blur=12)
    d = ImageDraw.Draw(canvas)
    y = y0 + 14
    for ln in lines:
        d.text((24, y), ln, font=font_cap, fill=BLACK,
               stroke_width=0.3, stroke_fill=BLACK)
        y += 30
    row_y = y1 - 36
    left_meta = " · ".join(x for x in (_date_parts(item)[0], weekday_cn(item.get("date", ""))) if x)
    _draw_dark(canvas, 24, row_y, left_meta, 19)
    city = _city(item)
    if city:
        cw = d.textlength(city, font=_get_font(19))
        _draw_dark(canvas, 456 - cw, row_y, city, 19)


def _p03(canvas, item) -> None:
    """角落手记：左对齐黑字加粗 + 底部米白渐变（514→800，文字顶到位）。"""
    _scrim(canvas, 0, int(_py(514)), 480, 800 - int(_py(514)), 0, 237,
           ramp_px=170, rgb=VEIL_RGB)
    caption = item.get("side") or ""
    if caption:
        lines = _wrap_lines(canvas, caption, _get_font(22), 420, 2)
        y = _py(684)
        for ln in lines:
            _draw_dark(canvas, 28, y, ln, 22, bold=True)
            y += 30
    _draw_dark(canvas, 28, _py(724), _meta_segments(item), 15,
               fill=META_GRAY, stroke=0)


def _p05(canvas, item) -> None:
    """竖排题签·钤印：右侧半透明签面竖排文案，等宽横排小篆印章。"""
    _glass(canvas, (382, int(_py(56)), 454, int(_py(352))), radius=8, alpha=184, blur=10)
    caption = item.get("side") or ""
    font_v = _get_font(20)
    d = ImageDraw.Draw(canvas)
    if caption:
        col_h, pitch = 280, 28
        per_col = col_h // pitch
        cols = [caption[i:i + per_col] for i in range(0, min(len(caption), per_col * 2), per_col)]
        for ci, col in enumerate(cols):
            col_cx = 428 - ci * 28          # 传统竖排：从右往左列
            y0 = _py(64)
            for j, ch in enumerate(col):
                d.text((col_cx, y0 + pitch / 2 + j * pitch), ch,
                       font=font_v, fill=BLACK, anchor="mm")
    _seal(canvas, 382, int(_py(356)), 72, 44, _city(item),
          vertical=False, glyph_px=26)
    _draw_white(canvas, [{"x": 24, "y": _py(724),
                          "text": _meta_segments(item, with_weekday=True), "size": 15, "dim": True}])


def _p08(canvas, item) -> None:
    """大字日期：日期即图形（右侧竖组黑字），文案小字左下；米白渐变 486→800。"""
    _scrim(canvas, 0, int(_py(486)), 480, 800 - int(_py(486)), 0, 237,
           ramp_px=170, rgb=VEIL_RGB)
    full, year, rest = _date_parts(item)
    _draw_dark(canvas, 452, _py(540), year, 20, align="r", cx=452, bold=True)
    _draw_dark(canvas, 456, _py(562), rest, 84, align="r", cx=456,
               bold=True, stroke=1.2)
    _draw_dark(canvas, 452, _py(656),
               " · ".join(x for x in (weekday_cn(item.get("date", "")), _city(item)) if x),
               14, fill=META_GRAY, stroke=0, align="r", cx=452)
    caption = item.get("side") or ""
    if caption:
        lines = _wrap_lines(canvas, caption, _get_font(18), 230, 2)
        y = _py(700)
        for ln in lines:
            _draw_dark(canvas, 24, y, ln, 18, bold=True)
            y += 27


def _p10(canvas, item) -> None:
    """全幅字幕：中轴对称黑字收在底部米白渐变上（506→800）。"""
    _scrim(canvas, 0, int(_py(506)), 480, 800 - int(_py(506)), 0, 237,
           ramp_px=170, rgb=VEIL_RGB)
    caption = item.get("side") or ""
    if caption:
        lines = _wrap_lines(canvas, caption, _get_font(22), 400, 2)
        y = _py(676)
        for ln in lines:
            _draw_dark(canvas, 40, y, ln, 22, bold=True, align="c", cx=240)
            y += 30
    _draw_dark(canvas, 40, _py(718), _meta_segments(item, with_weekday=False),
               15, fill=META_GRAY, stroke=0, align="c", cx=240)


# ==================================================================
# 横屏 8 款（800×480，展示页 x - 40 + INSET）
# ==================================================================
def _l01(canvas, item) -> None:
    """底部玻璃卡·基线（现行方案的 3D 玻璃卡，坐标=展示页）。"""
    _glass(canvas, (_lx(54), 366, _lx(746), 466), radius=18, alpha=184, blur=14,
           shadow=True, highlight=True)
    caption = item.get("side") or ""
    d = ImageDraw.Draw(canvas)
    x0 = _lx(68)
    if caption:
        lines = _wrap_lines(canvas, caption, _get_font(22), 664, 2)
        y = 376
        for ln in lines:
            d.text((x0, y), ln, font=_get_font(22), fill=BLACK,
                   stroke_width=0.3, stroke_fill=BLACK)
            y += 28
    left_meta = " · ".join(x for x in (_date_parts(item)[0], weekday_cn(item.get("date", ""))) if x)
    _draw_dark(canvas, x0, 436, left_meta, 18)
    city = _city(item)
    if city:
        cw = d.textlength(city, font=_get_font(18))
        _draw_dark(canvas, _lx(736) - cw, 436, city, 18)


def _l02(canvas, item) -> None:
    """左侧信息栏：通高毛玻璃栏，字段纵向排布。"""
    x0 = int(_lx(40))
    _glass(canvas, (x0, 40, x0 + 176, 440), radius=14, alpha=184, blur=12)
    full, _, _ = _date_parts(item)
    _draw_dark(canvas, _lx(58), 62, full, 24, stroke=0.5)
    wd = weekday_cn(item.get("date", ""))
    if wd:
        _draw_dark(canvas, _lx(58), 96, wd, 13, fill=GRAY_TEXT, stroke=0)
    ImageDraw.Draw(canvas).rectangle((_lx(58), 122, _lx(58) + 28, 125), fill=SEAL_RED)
    caption = item.get("side") or ""
    if caption:
        lines = _wrap_lines(canvas, caption, _get_font(18), 142, 9)
        y = 138
        for ln in lines:
            _draw_dark(canvas, _lx(58), y, ln, 18)
            y += 27
    _draw_dark(canvas, _lx(58), 412, _city(item), 14, fill=GRAY_TEXT, stroke=0)


def _l04(canvas, item) -> None:
    """电影字幕：居中黑字 + 底部米白渐变（212→480）。"""
    _scrim(canvas, 0, 212, 800, 480 - 212, 0, 237, ramp_px=170, rgb=VEIL_RGB)
    caption = item.get("side") or ""
    if caption:
        lines = _wrap_lines(canvas, caption, _get_font(22), 640, 2)
        y = 382
        for ln in lines:
            _draw_dark(canvas, 80, y, ln, 22, bold=True, align="c", cx=_lx(400))
            y += 30
    _draw_dark(canvas, 80, 424, _meta_segments(item, with_weekday=False),
               14, fill=META_GRAY, stroke=0, align="c", cx=_lx(400))


def _l05(canvas, item) -> None:
    """报纸横条：64px 实白条，日期—竖线—文案—城市从左到右。"""
    x0 = int(_lx(40))
    _glass(canvas, (x0, 404, x0 + 720, 468), radius=10, alpha=255, blur=1,
           shadow=True, highlight=False)
    full, _, _ = _date_parts(item)
    _draw_dark(canvas, _lx(64), 422, full, 21, stroke=0.5)
    ImageDraw.Draw(canvas).rectangle((_lx(180), 422, _lx(180) + 1, 450), fill=GRAY_LINE)
    caption = item.get("side") or ""
    if caption:
        fitted = _fit_text(ImageDraw.Draw(canvas), caption, _get_font(15), 470)
        _draw_dark(canvas, _lx(196), 427, fitted, 15, fill=(34, 34, 34), stroke=0)
    city = _city(item)
    if city:
        d = ImageDraw.Draw(canvas)
        cw = d.textlength(city, font=_get_font(13))
        _draw_dark(canvas, _lx(736) - cw, 429, city, 13, fill=GRAY_TEXT, stroke=0)


def _l06(canvas, item) -> None:
    """顶签悬挂：可视区顶边垂下挂签。"""
    x0 = int(_lx(40))
    _glass(canvas, (x0, 40, x0 + 330, 178), radius=14, alpha=184, blur=12)
    full, _, _ = _date_parts(item)
    _draw_dark(canvas, _lx(60), 56, full, 25, stroke=0.5)
    wd = weekday_cn(item.get("date", ""))
    if wd:
        _draw_dark(canvas, _lx(60), 92, wd, 12, fill=GRAY_TEXT, stroke=0)
    ImageDraw.Draw(canvas).rectangle((_lx(60), 114, _lx(60) + 30, 117), fill=SEAL_RED)
    caption = item.get("side") or ""
    if caption:
        lines = _wrap_lines(canvas, caption, _get_font(16), 290, 2)
        y = 126
        for ln in lines:
            _draw_dark(canvas, _lx(60), y, ln, 16)
            y += 24


def _l07(canvas, item) -> None:
    """落款钤印：黑字文案/日期右对齐收在右下 + 底部米白渐变，末尾竖排小篆印。"""
    _scrim(canvas, 0, 168, 800, 480 - 168, 0, 237, ramp_px=170, rgb=VEIL_RGB)
    seal_x1 = int(_lx(736))
    text_r = seal_x1                            # 无城市→无印章，文字直接顶右缘
    if _city(item):
        text_r = seal_x1 - 42 - 10              # 文字给竖排印章让位
    text_l = int(_lx(40)) + 10                 # 文字左界
    font_cap = _get_font(21)
    d = ImageDraw.Draw(canvas)
    caption = item.get("side") or ""
    if caption:
        fitted = _fit_text(d, caption, font_cap, text_r - text_l)
        _draw_dark(canvas, 0, 338, fitted, 21, bold=True, align="r", cx=text_r)
    meta = " · ".join(x for x in (_date_parts(item)[0], weekday_cn(item.get("date", ""))) if x)
    if meta:
        _draw_dark(canvas, 0, 380, meta, 14, fill=META_GRAY, stroke=0,
                   align="r", cx=text_r)
    if _city(item):
        _seal(canvas, seal_x1 - 42, 338, 42, 61, _city(item),
              vertical=True, glyph_px=20)


def _l10(canvas, item) -> None:
    """中央铭牌：底部居中实白铭牌（博物馆仪式感）。"""
    _glass(canvas, (_lx(260), 374, _lx(540), 470), radius=14, alpha=255, blur=1,
           shadow=True, highlight=False)
    full, _, _ = _date_parts(item)
    cx = _lx(400)
    d = ImageDraw.Draw(canvas)
    fw = d.textlength(full, font=_get_font(19))
    _draw_dark(canvas, cx - fw / 2, 383, full, 19, stroke=0.5)
    d.rectangle((cx - 15, 418, cx + 15, 420), fill=SEAL_RED)
    caption = item.get("side") or ""
    if caption:
        lines = _wrap_lines(canvas, caption, _get_font(14), 256, 2)
        y = 428
        for ln in lines:
            lw = d.textlength(ln, font=_get_font(14))
            _draw_dark(canvas, cx - lw / 2, y, ln, 14, fill=(51, 51, 51), stroke=0)
            y += 21


PORTRAIT_RENDERERS = {"P01": _p01, "P03": _p03, "P05": _p05,
                      "P08": _p08, "P10": _p10}
LANDSCAPE_RENDERERS = {"L01": _l01, "L02": _l02, "L04": _l04, "L05": _l05,
                       "L06": _l06, "L07": _l07, "L10": _l10}


def render_layout(canvas: Image.Image, layout: str, item: Dict[str, Any]) -> None:
    """按版式 ID 在已裁切好的画布上绘制文字层。"""
    if layout in PORTRAIT_RENDERERS:
        PORTRAIT_RENDERERS[layout](canvas, item)
    elif layout in LANDSCAPE_RENDERERS:
        LANDSCAPE_RENDERERS[layout](canvas, item)
    else:
        raise ValueError(f"未知版式: {layout}")
