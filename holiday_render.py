#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
节假日庆祝层渲染（2026-09-13 改版，方案出自 docs/holiday-template-showcase-2026-09-13
用户选定：T1-A / T2-B / T2-E / T3-A / T3-B / T4-A / T4-D / T5-B / T5-E）。

在 12 款版式文字层（render_layouts.render_layout）之后、gamma/抖动之前调用：
    holiday_render.apply_holiday_layer(canvas, layout_id, orientation, holiday)

三项纪律（本轮改版强化）：
1. 对齐：文字一律用 PIL 锚点（lm/rm/ma/mm）定位，横幅内光学垂直居中，
   不再手估像素偏移；
2. 主体遮挡：实色顶带模板（横幅/旗语/波浪/刊头/心形旗语）把占位高度并入
   裁切避让区（holiday_top_reserve → compute_crop_window 的 top_reserve），
   悬浮元素（书法大字/双戒）渲染期读 canvas.info["subject_box"]，
   在左/中/右三个锚位里选与主体 + 版式占位区重叠最小的槽位（_dodge_x）；
3. 相框遮罩：横屏左右各被遮 INSET 像素——所有贴边元素按可视区
   （_vis_x）锚定；竖屏顶边按可视顶（_top）锚定。

铁律不变：只用调色板纯色（SEAL_RED/ACCENT_YELLOW/WHITE/WHITE_DIM），
元素全不透明、无深色描边，文字用「黑偏移 2px + 主色」模拟阴影。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw

from render_layouts import (ACCENT_YELLOW, INSET, SEAL_RED, WHITE, WHITE_DIM,
                            _fit_text, _get_font)

CANVAS_W_P, CANVAS_H_P = 480, 800    # 竖屏
CANVAS_W_L, CANVAS_H_L = 800, 480    # 横屏

BANNER_H_P, BANNER_H_L = 72, 56      # 通用/长辈/成人生日横幅高
BANNER_RADIUS = 16
YELLOW_LINE_H = 6                    # 横幅下沿黄色饰线
WHITE_GAP_H = 2                      # 黄饰线上的白色分隔线

# 版式冲突时的匹配式变体（2026-09-13 二次改版：废除位移/截短式让位）。
# 横幅族在 L02/L06 上改为与版式顶边齐平的通栏细横带（strip），
# 在 P05 上改为与右侧题签对称的左侧竖排节日带（p05_band）；
# 书法大字/双戒走 _dodge_x 真避让，不在此列。
LAYOUT_FIT: Dict[str, str] = {"L02": "strip", "L06": "strip", "P05": "p05_band"}
BANNER_FAMILY = {"general_ribbon", "elder_ribbon", "adult_cake",
                 "child_wave", "anniversary_hearts"}

# 版式自身占用的顶部区域（启发式，画布坐标；供悬浮元素槽位避让）
LAYOUT_ZONES: Dict[str, List[Tuple[int, int, int, int]]] = {
    "P05": [(382, 56, 454, 400)],
    "L02": [(40, 40, 216, 440)],
    "L06": [(40, 40, 370, 178)],
}


# ========== 几何助手 ==========

def _canvas_w(o: str) -> int:
    return CANVAS_W_P if o == "portrait" else CANVAS_W_L


def _canvas_h(o: str) -> int:
    return CANVAS_H_P if o == "portrait" else CANVAS_H_L


def _top(o: str) -> int:
    """可视区顶边：竖屏被相框遮 INSET，横屏顶边不遮。"""
    return INSET if o == "portrait" else 0


def _vis_x(o: str, W: int) -> Tuple[int, int]:
    """可视区水平范围：横屏左右各遮 INSET。"""
    return (0, W) if o == "portrait" else (INSET, W - INSET)


def holiday_reserve(template: str, orientation: str, layout_id: str = "") -> Dict[str, int]:
    """模板 + 版式的裁切避让区（画布坐标系）。返回 {"top": px, "left": px} 子集。
    实色顶带占顶部（冲突版式上变细横带后只占 44px）；P05 竖排带占左侧；
    书法大字/双戒不占裁切区（渲染期槽位避让）。"""
    fit = LAYOUT_FIT.get(layout_id) if template in BANNER_FAMILY else None
    if fit == "strip":
        return {"top": 44}
    if fit == "p05_band":
        return {"left": 96} if orientation == "portrait" else {}
    top = _top(orientation)
    if template in ("general_ribbon", "elder_ribbon", "adult_cake"):
        h = BANNER_H_P if orientation == "portrait" else BANNER_H_L
        return {"top": top + h + YELLOW_LINE_H + 2}
    if template == "child_wave":
        return {"top": top + 64 + 13}
    if template == "child_bunting":
        return {"top": top + 36}
    if template == "adult_editorial":
        return {"top": top + 60}
    if template == "anniversary_hearts":
        return {"top": top + 60 + 18}
    return {}  # elder_calligraphy / anniversary_rings


# ========== 绘制原语 ==========

def _shadow_text(canvas: Image.Image, xy: Tuple[float, float], s: str, size: int,
                 fill: Tuple[int, int, int], anchor: Optional[str] = None) -> None:
    """文字一次绘制：黑色偏移 2px 模拟投影 + 主色（无深色描边铁律）。"""
    d = ImageDraw.Draw(canvas)
    x, y = xy
    d.text((x, y + 2), s, font=_get_font(size), fill=(0, 0, 0), anchor=anchor)
    d.text((x, y), s, font=_get_font(size), fill=fill, anchor=anchor)


def _heart(canvas: Image.Image, cx: float, cy: float, r: float,
           color: Tuple[int, int, int]) -> None:
    """实心心形：两圆 + 下探三角，同色叠加成心。"""
    d = ImageDraw.Draw(canvas)
    d.ellipse((cx - r, cy - r * 0.9, cx, cy + r * 0.1), fill=color)
    d.ellipse((cx, cy - r * 0.9, cx + r, cy + r * 0.1), fill=color)
    d.polygon([(cx - r * 0.94, cy - r * 0.18), (cx + r * 0.94, cy - r * 0.18),
               (cx, cy + r)], fill=color)


_SEAL_GLYPH_DIR = Path(__file__).resolve().parent / "seal_glyphs"


def _glyph(canvas: Image.Image, hexcode: str, cx: float, cy: float,
           size: int, color: Tuple[int, int, int]) -> bool:
    """贴小篆字形（seal_glyphs/{hex}.png，透明底白字形）；缺文件返回 False
    由调用方回退字体。color≠WHITE 时重着色（白字形→指定色，alpha 不变）。"""
    p = _SEAL_GLYPH_DIR / f"{hexcode}.png"
    if not p.exists():
        return False
    g = Image.open(p).convert("RGBA").resize((size, size), Image.LANCZOS)
    if color != WHITE:
        r, g2, b, a = g.split()
        solid = Image.new("RGBA", g.size, color + (0,))
        solid.putalpha(a)
        g = solid
    canvas.paste(g, (int(cx - size / 2), int(cy - size / 2)), g)
    return True


def _seal_shou(canvas: Image.Image, x1: int, y0: int, banner_h: int) -> None:
    """「壽」白文印：白底圆角方章 + 小篆壽字形（红，缺字形回退字体），贴横幅右端。"""
    side = max(44, banner_h - 12)
    x0, y0 = x1 - 12 - side, y0 + (banner_h - side) // 2
    d = ImageDraw.Draw(canvas)
    d.rounded_rectangle((x0, y0, x0 + side, y0 + side), radius=8, fill=WHITE)
    if not _glyph(canvas, "58FD", x0 + side / 2, y0 + side / 2,
                  int(side * 0.78), SEAL_RED):
        d.text((x0 + side / 2, y0 + side / 2), "寿", font=_get_font(int(side * 0.62)),
               fill=SEAL_RED, anchor="mm")


def _cake(canvas: Image.Image, x1: int, y0: int, banner_h: int) -> None:
    """三层蛋糕剪影（白色糕体 + 黄色蜡烛火苗），底边锚横幅下沿上方 10px。"""
    w = 46
    x0 = x1 - 12 - w
    base = y0 + banner_h - 10
    d = ImageDraw.Draw(canvas)
    tiers = ((w, 12), (w - 12, 11), (w - 24, 10))
    ty = base
    for tw, th in tiers:
        tx = x0 + (w - tw) / 2
        d.rounded_rectangle((tx, ty - th, tx + tw, ty), radius=4, fill=WHITE)
        ty -= th + 1
    top_w = tiers[-1][0]
    for i in range(3):
        cx = x0 + (w - top_w) / 2 + top_w * (i + 0.5) / 3
        d.rectangle((cx - 1.5, ty - 9, cx + 1.5, ty), fill=WHITE)
        d.ellipse((cx - 3, ty - 16, cx + 3, ty - 9), fill=ACCENT_YELLOW)


# ========== 主体/版式占位避让 ==========

def _overlap_area(a: Tuple[int, int, int, int],
                  b: Tuple[int, int, int, int]) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return max(0.0, w) * max(0.0, h)


def _dodge_x(canvas: Image.Image, o: str, layout_id: str,
             item_w: int, y0: int, y1: int,
             alt_slot: bool = False) -> int:
    """在可视区的左/中/右三个锚位里选落位：
    - 版式文本占位区（LAYOUT_ZONES）为硬否决——任何重叠直接出局（文本永不遮挡）；
    - 主体框为软优先分（重叠越小越好）；
    - 全部被否决时退回与占位区重叠最小者；平局确定性：中 > 左 > 右；
    - alt_slot=True 时取次优（AI-QA 换位重试用）。
    item_w 应取该模板在 (y0,y1) 内最宽元素的宽度（含下方胶囊）。"""
    W = _canvas_w(o)
    xl, xr = _vis_x(o, W)
    cands = [(xl + xr) // 2, xl + 18 + item_w // 2, xr - 18 - item_w // 2]
    layout_zones = LAYOUT_ZONES.get(layout_id, [])
    subject = canvas.info.get("subject_box") if hasattr(canvas, "info") else None

    ranked = []
    for i, cx in enumerate(cands):
        rect = (int(cx - item_w / 2), y0, int(cx + item_w / 2), y1)
        veto = any(_overlap_area(rect, z) > 1 for z in layout_zones)
        subj = _overlap_area(rect, tuple(subject)) if subject else 0.0
        ranked.append((veto, subj, i, cx))
    ranked.sort(key=lambda t: (t[0], t[1], t[2]))
    pick = ranked[1] if (alt_slot and len(ranked) > 1) else ranked[0]
    if pick[0]:
        print(f"[HOLIDAY] ⚠️ {layout_id} 三个锚位均与文本区冲突，取重叠最小者")
    cx = pick[3]
    return max(xl + item_w // 2, min(xr - item_w // 2, cx))


# ========== 横幅族（通用 / 长辈 / 成人生日共用骨架） ==========

def _draw_banner(canvas: Image.Image, box: Tuple[int, int, int, int],
                 color: Tuple[int, int, int],
                 dual_underline: bool = False) -> None:
    """顶部横幅：顶角方角贴屏、底角圆角；dual_underline 时下沿为黄 6px+白 2px 双色。"""
    x0, y0, x1, y1 = box
    d = ImageDraw.Draw(canvas)
    d.rounded_rectangle(box, radius=BANNER_RADIUS, fill=color)
    d.rectangle((x0, y0, x1, y0 + BANNER_RADIUS), fill=color)
    if dual_underline:
        d.rectangle((x0, y1 - YELLOW_LINE_H, x1, y1), fill=ACCENT_YELLOW)
        d.rectangle((x0, y1 - YELLOW_LINE_H - WHITE_GAP_H, x1,
                     y1 - YELLOW_LINE_H), fill=WHITE)
    else:
        d.rectangle((x0, y1 - YELLOW_LINE_H, x1, y1), fill=ACCENT_YELLOW)


def _banner_text_pair(canvas: Image.Image, box: Tuple[int, int, int, int],
                      name: str, greeting: str, ns: int, gs: int,
                      name_fill: Tuple[int, int, int],
                      greet_fill: Tuple[int, int, int],
                      ornament_w: int = 0) -> None:
    """横幅文字：锚点 lm/rm 定位，光学垂直居中；右侧锚点吃进相框遮罩；
    ornament_w 为右端装饰物（寿章/蛋糕）占宽，祝福语右锚避让它。"""
    x0, y0, x1, y1 = box
    ymid = (y0 + y1) // 2 - 2
    xl, xr = _vis_x("portrait" if canvas.width == CANVAS_W_P else "landscape", canvas.width)
    tx0 = max(x0, xl) + 22
    tx1 = min(x1, xr) - 22 - ornament_w
    _shadow_text(canvas, (tx0, ymid), name, ns, name_fill, anchor="lm")
    if greeting:
        d = ImageDraw.Draw(canvas)
        max_w = (tx1 - tx0) - 20 - d.textlength(name, font=_get_font(ns))
        if max_w > 60:
            greeting = _fit_text(d, greeting, _get_font(gs), max_w)
            _shadow_text(canvas, (tx1, ymid + 1), greeting, gs, greet_fill, anchor="rm")


def _banner_zone(o: str, placement: Dict[str, int],
                 h_p: int = BANNER_H_P, h_l: int = BANNER_H_L) -> Tuple[int, int, int, int]:
    W, top = _canvas_w(o), _top(o)
    h = h_p if o == "portrait" else h_l
    x0, x1 = 0, W
    mode = placement.get("mode", "default")
    if mode == "right":
        x0 = placement.get("x0", x0)
    elif mode == "left":
        x1 = placement.get("x1", x1)
    return (x0, top, x1, top + h)


# ========== 九套模板（统一签名 canvas, o, layout_id, holiday） ==========

def _t_general_ribbon(canvas: Image.Image, o: str, layout_id: str,
                      holiday: Dict[str, Any]) -> None:
    """T1-A 刊头横幅：红横幅 + 黄白双色饰线 + 名左/祝福右。"""
    box = _banner_zone(o, {"mode": "default"})
    ns, gs = (30, 17) if o == "portrait" else (26, 15)
    _draw_banner(canvas, box, SEAL_RED, dual_underline=True)
    _banner_text_pair(canvas, box, holiday["name"], holiday.get("greeting", ""),
                      ns, gs, WHITE, WHITE_DIM)


def _t_child_bunting(canvas: Image.Image, o: str, layout_id: str,
                     holiday: Dict[str, Any]) -> None:
    """T2-B 彩旗派对：顶部红黄三角旗一排 + 白底名牌（L02/L06 上名牌翻右侧）。"""
    W, top = _canvas_w(o), _top(o)
    d = ImageDraw.Draw(canvas)
    d.rectangle((0, top + 2, W, top + 6), fill=WHITE)
    n = max(8, W // 30)
    seg = W / n
    fw, fh = 24, 22
    for i in range(n):
        color = SEAL_RED if i % 2 == 0 else ACCENT_YELLOW
        x = i * seg + (seg - fw) / 2
        d.polygon([(x, top + 6), (x + fw, top + 6), (x + fw / 2, top + 6 + fh)],
                  fill=color)
    xl, xr = _vis_x(o, W)
    name, greet = holiday["name"], holiday.get("greeting", "")
    tag_w = 32 + max(d.textlength(name, font=_get_font(20)),
                     d.textlength(f"{greet} · 又一岁啦", font=_get_font(13))
                     if greet else 0)
    # L02 左栏 / L06 顶签占位时名牌翻到右侧（旗帜本身不与其冲突）
    tx = (xr - 18 - tag_w) if layout_id in ("L02", "L06") else (xl + 18)
    d.rounded_rectangle((tx, top + 40, tx + tag_w, top + 106), radius=12, fill=WHITE)
    d.text((tx + 16, top + 50), name, font=_get_font(20), fill=SEAL_RED, anchor="la")
    if greet:
        d.text((tx + 16, top + 78), f"{greet} · 又一岁啦", font=_get_font(13),
               fill=(0, 0, 0), anchor="la")


def _t_child_wave(canvas: Image.Image, o: str, layout_id: str,
                  holiday: Dict[str, Any]) -> None:
    """T2-E 波浪黄幅：黄色横幅 + 底边一圈半圆波浪（像蛋糕花边），居中两行。"""
    W, top = _canvas_w(o), _top(o)
    h = 64
    box = (0, top, W, top + h)
    d = ImageDraw.Draw(canvas)
    d.rounded_rectangle(box, radius=BANNER_RADIUS, fill=ACCENT_YELLOW)
    d.rectangle((0, top, W, top + BANNER_RADIUS), fill=ACCENT_YELLOW)
    for cx in range(0, W + 22, 22):
        d.ellipse((cx - 11, top + h - 11, cx + 11, top + h + 11), fill=ACCENT_YELLOW)
    _shadow_text(canvas, (W // 2, top + 7), holiday["name"], 28, SEAL_RED, anchor="ma")
    if holiday.get("greeting"):
        _shadow_text(canvas, (W // 2, top + 42), holiday["greeting"], 14,
                     WHITE, anchor="ma")


def _t_elder_ribbon(canvas: Image.Image, o: str, layout_id: str,
                    holiday: Dict[str, Any]) -> None:
    """T3-A 横幅寿印：红横幅 + 右端白底红文寿章（右缘锚可视区）。"""
    W, top = _canvas_w(o), _top(o)
    box = _banner_zone(o, {"mode": "default"})
    ns, gs = (30, 16) if o == "portrait" else (26, 15)
    _draw_banner(canvas, box, SEAL_RED)
    _, xr = _vis_x(o, W)
    bh = box[3] - box[1]
    _seal_shou(canvas, xr if o == "landscape" else box[2], top, bh)
    _banner_text_pair(canvas, box, holiday["name"], holiday.get("greeting", ""),
                      ns, gs, WHITE, WHITE_DIM, ornament_w=max(44, bh - 12) + 24)


def _t_elder_calligraphy(canvas: Image.Image, o: str, layout_id: str,
                         holiday: Dict[str, Any]) -> None:
    """T3-B 大字中堂：书法大「壽」白影压图 + 下方白胶囊对联句。
    槽位避让：主体框/版式占位重叠最小的左/中/右位。"""
    W, top = _canvas_w(o), _top(o)
    fs = 120 if o == "portrait" else 96
    greet = holiday.get("greeting", "")
    d = ImageDraw.Draw(canvas)
    # 胶囊比字形宽，避让宽度取二者较大者，防止胶囊压到版式文本区
    pw = int(d.textlength(greet, font=_get_font(18))) + 40 if greet else 0
    cx = _dodge_x(canvas, o, layout_id, max(fs, pw), top,
                  top + int(fs * 1.15) + 56,
                  alt_slot=bool(holiday.get("_alt_slot")))
    d.text((cx + 6, top + 6), "壽", font=_get_font(fs), fill=WHITE, anchor="ma")
    d.text((cx, top), "壽", font=_get_font(fs), fill=SEAL_RED, anchor="ma")
    if greet:
        px = max(_vis_x(o, W)[0] + 8, min(_vis_x(o, W)[1] - 8 - pw, cx - pw // 2))
        d.rounded_rectangle((px, top + int(fs * 1.12), px + pw,
                             top + int(fs * 1.12) + 42), radius=21, fill=WHITE)
        d.text((px + pw / 2, top + int(fs * 1.12) + 21), greet,
               font=_get_font(18), fill=SEAL_RED, anchor="mm")


def _t_adult_cake(canvas: Image.Image, o: str, layout_id: str,
                  holiday: Dict[str, Any]) -> None:
    """T4-A 横幅蛋糕：红横幅 + 右端白蛋糕剪影（右缘锚可视区）。"""
    W, top = _canvas_w(o), _top(o)
    box = _banner_zone(o, {"mode": "default"})
    ns, gs = (30, 16) if o == "portrait" else (26, 15)
    _draw_banner(canvas, box, SEAL_RED)
    _, xr = _vis_x(o, W)
    _cake(canvas, xr if o == "landscape" else box[2], top, box[3] - box[1])
    _banner_text_pair(canvas, box, holiday["name"], holiday.get("greeting", ""),
                      ns, gs, WHITE, WHITE_DIM, ornament_w=46 + 24)


def _t_adult_editorial(canvas: Image.Image, o: str, layout_id: str,
                       holiday: Dict[str, Any]) -> None:
    """T4-D 杂志刊头：白色刊头条，左红字大标题、右黑色小字英文字距排开，黄线收边。"""
    W, top = _canvas_w(o), _top(o)
    h = 56 if o == "portrait" else 48
    d = ImageDraw.Draw(canvas)
    d.rectangle((0, top, W, top + h), fill=WHITE)
    d.rectangle((0, top + h, W, top + h + 4), fill=ACCENT_YELLOW)
    xl, xr = _vis_x(o, W)
    ns = 28 if o == "portrait" else 24
    greet = holiday.get("greeting", "")
    d2 = ImageDraw.Draw(canvas)
    if greet:
        right = f"HAPPY BIRTHDAY · {greet}"
        if d2.textlength(right, font=_get_font(14)) > (xr - xl) * 0.55:
            right = greet
    else:
        right = "HAPPY  BIRTHDAY"
    # 白底上不用黑色偏移影（会成重影），改同色 0.3 描边加粗——与版式 _draw_dark 同口径
    d2.text((xl + 22, (2 * top + h) // 2 - 2), holiday["name"], font=_get_font(ns),
            fill=SEAL_RED, anchor="lm", stroke_width=0.3, stroke_fill=SEAL_RED)
    d2.text((xr - 22, (2 * top + h) // 2), right, font=_get_font(14),
            fill=(0, 0, 0), anchor="rm", stroke_width=0.3, stroke_fill=(0, 0, 0))


def _t_anniversary_rings(canvas: Image.Image, o: str, layout_id: str,
                         holiday: Dict[str, Any]) -> None:
    """T5-B 双戒相扣：红黄两枚交叠圆环 + 白胶囊横批。槽位避让同大字中堂。"""
    W, top = _canvas_w(o), _top(o)
    d = ImageDraw.Draw(canvas)
    ring_d, stroke, overlap = 52, 9, 24
    pair_w = ring_d + overlap
    label = holiday["name"]
    if holiday.get("greeting"):
        label = f"{label} · {holiday['greeting']}"
    pw = int(d.textlength(label, font=_get_font(17))) + 36
    # 胶囊比戒环对宽，避让宽度取二者较大者
    cx = _dodge_x(canvas, o, layout_id, max(pair_w + 20, pw),
                  top, top + 130, alt_slot=bool(holiday.get("_alt_slot")))
    px0 = cx - pair_w / 2
    cy = top + 16 + ring_d // 2
    d.ellipse((px0, cy - ring_d / 2, px0 + ring_d, cy + ring_d / 2),
              outline=SEAL_RED, width=stroke)
    d.ellipse((px0 + overlap, cy - ring_d / 2, px0 + overlap + ring_d,
               cy + ring_d / 2), outline=ACCENT_YELLOW, width=stroke)
    ph = 42
    py = cy + ring_d // 2 + 14
    px = max(_vis_x(o, W)[0] + 8, min(_vis_x(o, W)[1] - 8 - pw, cx - pw / 2))
    d.rounded_rectangle((px, py, px + pw, py + ph), radius=ph / 2, fill=WHITE)
    d.text((px + pw / 2, py + ph / 2), label, font=_get_font(17),
           fill=SEAL_RED, anchor="mm")


def _t_anniversary_hearts(canvas: Image.Image, o: str, layout_id: str,
                          holiday: Dict[str, Any]) -> None:
    """T5-E 心形旗语：红色横带 + 居中白字 + 底边一排悬垂小红心。"""
    W, top = _canvas_w(o), _top(o)
    h = 60 if o == "portrait" else 52
    box = (0, top, W, top + h)
    _draw_banner(canvas, box, SEAL_RED)
    label = holiday["name"]
    if holiday.get("greeting"):
        label = f"{label} · {holiday['greeting']}"
    ns = 20 if o == "portrait" else 18
    _shadow_text(canvas, (W / 2, (box[1] + box[3]) / 2), label, ns, WHITE,
                 anchor="mm")
    for cx in range(24, W, 48):
        _heart(canvas, cx, box[3] + 8, 18, SEAL_RED)


# ========== 版式冲突的匹配式变体 ==========

def _t_header_strip(canvas: Image.Image, o: str, holiday: Dict[str, Any],
                   layout_id: str) -> None:
    """L02/L06 变体：红色红头与版式部件（信息栏/挂签）同宽同位、顶边齐平，
    读作该部件自带的红色题头，而非独立悬浮的带子。
    L06 挂签宽 40..370 单行文字；L02 信息栏宽 40..216 两行居中。"""
    d = ImageDraw.Draw(canvas)
    if layout_id == "L02":
        x0, x1 = 40, 216
        d.rounded_rectangle((x0, 0, x1, 40), radius=14, fill=SEAL_RED)
        d.rectangle((x0, 26, x1, 40), fill=SEAL_RED)
        _shadow_text(canvas, ((x0 + x1) / 2, 15), holiday["name"], 17, WHITE,
                     anchor="mm")
        if holiday.get("greeting"):
            _shadow_text(canvas, ((x0 + x1) / 2, 31), holiday["greeting"], 10,
                         WHITE_DIM, anchor="mm")
    else:  # L06 挂签
        x0, x1 = 40, 370
        d.rounded_rectangle((x0, 0, x1, 40), radius=14, fill=SEAL_RED)
        d.rectangle((x0, 26, x1, 40), fill=SEAL_RED)
        _shadow_text(canvas, (x0 + 20, 20), holiday["name"], 20, WHITE, anchor="lm")
        if holiday.get("greeting"):
            _shadow_text(canvas, (x1 - 20, 21), holiday["greeting"], 12,
                         WHITE_DIM, anchor="rm")


def _t_p05_band(canvas: Image.Image, o: str, holiday: Dict[str, Any],
                tpl: str) -> None:
    """P05 变体：与右侧竖排题签对称的左侧竖排节日带（红底白竖字 + 黄色收尾）。
    长辈款带白寿章收底、成人生日款带白蛋糕收底；其余纯文字收尾。
    竖排惯例：名在右列（大），祝福在左列（小、次级白）。"""
    d = ImageDraw.Draw(canvas)
    bx0, bx1, by0, by1 = 16, 88, 56, 400
    d.rectangle((bx0, by0, bx1, by1), fill=SEAL_RED)
    name = holiday["name"][:6]
    cx_name, cx_greet = bx0 + 46, bx0 + 24
    for i, ch in enumerate(name):
        _shadow_text(canvas, (cx_name, by0 + 22 + i * 38), ch, 28, WHITE,
                     anchor="mm")
    greet = holiday.get("greeting", "")[:12]
    if greet:
        for i, ch in enumerate(greet):
            d.text((cx_greet, by0 + 24 + i * 21), ch, font=_get_font(16),
                   fill=WHITE_DIM, anchor="mm")
    # 收尾装饰
    if tpl == "elder_ribbon":
        d.rounded_rectangle((bx0 + 10, by1 - 58, bx1 - 10, by1 - 10),
                            radius=8, fill=WHITE)
        if not _glyph(canvas, "58FD", (bx0 + bx1) / 2, by1 - 34, 36, SEAL_RED):
            d.text(((bx0 + bx1) / 2, by1 - 34), "寿", font=_get_font(30),
                   fill=SEAL_RED, anchor="mm")
    elif tpl == "adult_cake":
        _cake(canvas, bx1 - 4, by1 - 52, 44)
    d.rectangle((bx0, by1 - 8, bx1, by1), fill=ACCENT_YELLOW)


_TEMPLATES = {
    "general_ribbon": _t_general_ribbon,
    "child_bunting": _t_child_bunting,
    "child_wave": _t_child_wave,
    "elder_ribbon": _t_elder_ribbon,
    "elder_calligraphy": _t_elder_calligraphy,
    "adult_cake": _t_adult_cake,
    "adult_editorial": _t_adult_editorial,
    "anniversary_rings": _t_anniversary_rings,
    "anniversary_hearts": _t_anniversary_hearts,
}


def apply_holiday_layer(canvas: Image.Image, layout_id: str, orientation: str,
                        holiday: Dict[str, Any]) -> None:
    """
    在版式文字层之上叠加节日庆祝元素。任何异常都不应中断每日渲染——
    失败打印警告并跳过（保留无装饰画面）。
    版式冲突（L02/L06/P05）时横幅族切换为匹配式变体（细横带/竖排带），
    其余模板照常（书法大字/双戒内部已做槽位避让）。
    """
    try:
        tpl = holiday.get("template", "")
        fn = _TEMPLATES.get(tpl, _t_general_ribbon)
        fit = LAYOUT_FIT.get(layout_id) if tpl in BANNER_FAMILY else None
        if fit == "strip":
            _t_header_strip(canvas, orientation, holiday, layout_id)
        elif fit == "p05_band":
            _t_p05_band(canvas, orientation, holiday, tpl)
        else:
            fn(canvas, orientation, layout_id, holiday)
    except Exception as e:
        print(f"[HOLIDAY] ⚠️ 节日层绘制失败（{e}），本张按无装饰处理")
