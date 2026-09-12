#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
节假日庆祝层渲染（2026-09-12 设计 §5，docs/plans/2026-09-12-holiday-mode-design.md）。

在 12 款版式文字层（render_layouts.render_layout）之后、gamma/抖动之前调用：
    holiday_render.apply_holiday_layer(canvas, layout_id, orientation, holiday)

铁律（与 render_layouts.py 同口径）：
- 只用调色板纯色 SEAL_RED / ACCENT_YELLOW / WHITE，元素全不透明、无深色描边；
- 立体感只靠柔和投影 + 白高光，文字用「深色偏移 + 主色」模拟阴影；
- 颜色在抖动前是调色板精确值，四色量化零失真。

放置让位（版式占用非底部区域时，顶部节日元素避开）：
    L02 左侧通高信息栏 → 元素右移 x≥240；L06 顶签悬挂 → 右移 x≥390；
    P05 右侧竖排题签+印 → 元素右缘截短至 x≤370，角标翻左上。
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from PIL import Image, ImageDraw

from render_layouts import (ACCENT_YELLOW, INSET, SEAL_RED, WHITE, WHITE_DIM,
                            _fit_text, _get_font)

CANVAS_W_P, CANVAS_H_P = 480, 800    # 竖屏
CANVAS_W_L, CANVAS_H_L = 800, 480    # 横屏

BANNER_H_P, BANNER_H_L = 72, 56      # 横幅高
BANNER_RADIUS = 16
YELLOW_LINE_H = 4                    # 横幅下沿黄色饰线

# 顶部元素放置让位表（值为具体几何，画布坐标系）
HOLIDAY_PLACEMENT: Dict[str, Dict[str, int]] = {
    "L02": {"mode": "right", "x0": 240},
    "L06": {"mode": "right", "x0": 390},
    "P05": {"mode": "left", "x1": 370},
}


# ========== 小工具 ==========

def _shadow_text(canvas: Image.Image, xy: Tuple[float, float], s: str, size: int,
                 fill: Tuple[int, int, int], anchor: Optional[str] = None) -> None:
    """文字一次绘制：深色偏移 2px 模拟投影 + 主色（无深色描边铁律）。"""
    d = ImageDraw.Draw(canvas)
    x, y = xy
    off = (x, y + 2)
    if anchor:
        d.text(off, s, font=_get_font(size), fill=(0, 0, 0), anchor=anchor)
        d.text((x, y), s, font=_get_font(size), fill=fill, anchor=anchor)
    else:
        d.text(off, s, font=_get_font(size), fill=(0, 0, 0))
        d.text((x, y), s, font=_get_font(size), fill=fill)


def _banner_box(orientation: str, placement: Dict[str, int]) -> Tuple[int, int, int, int]:
    """横幅几何：竖屏锚可视区顶（y=INSET），横屏顶边不被遮挡（y=0）。"""
    if orientation == "portrait":
        x0, x1, y0 = 0, CANVAS_W_P, INSET
        h = BANNER_H_P
    else:
        x0, x1, y0 = 0, CANVAS_W_L, 0
        h = BANNER_H_L
    mode = placement.get("mode", "default")
    if mode == "right":
        x0 = placement.get("x0", x0)
    elif mode == "left":
        x1 = placement.get("x1", x1)
    return x0, y0, x1, y0 + h


def _draw_banner(canvas: Image.Image, box: Tuple[int, int, int, int],
                 color: Tuple[int, int, int], underline: bool = True) -> None:
    """顶部横幅：顶部两角方角（贴屏顶）、底部两角圆角，下沿黄色饰线。"""
    x0, y0, x1, y1 = box
    d = ImageDraw.Draw(canvas)
    d.rounded_rectangle(box, radius=BANNER_RADIUS, fill=color)
    d.rectangle((x0, y0, x1, y0 + BANNER_RADIUS), fill=color)  # 方平顶边
    if underline:
        d.rectangle((x0, y1 - YELLOW_LINE_H, x1, y1), fill=ACCENT_YELLOW)


def _heart(canvas: Image.Image, cx: float, cy: float, r: float,
           color: Tuple[int, int, int]) -> None:
    """实心心形：两圆 + 下探三角，同色叠加成心。"""
    d = ImageDraw.Draw(canvas)
    d.ellipse((cx - r, cy - r * 0.9, cx, cy + r * 0.1), fill=color)
    d.ellipse((cx, cy - r * 0.9, cx + r, cy + r * 0.1), fill=color)
    d.polygon([(cx - r * 0.94, cy - r * 0.18), (cx + r * 0.94, cy - r * 0.18),
               (cx, cy + r)], fill=color)


def _seal_shou(canvas: Image.Image, x1: int, y0: int, banner_h: int) -> None:
    """「寿」字白文印：红底圆角方章贴横幅右端（仿 render_layouts._seal 手法）。"""
    side = max(44, banner_h - 12)
    x0, y0 = x1 - 12 - side, y0 + (banner_h - side) // 2
    d = ImageDraw.Draw(canvas)
    d.rounded_rectangle((x0, y0, x0 + side, y0 + side), radius=8, fill=WHITE)
    d.text((x0 + side / 2, y0 + side / 2), "寿", font=_get_font(int(side * 0.62)),
           fill=SEAL_RED, anchor="mm")


def _cake(canvas: Image.Image, x1: int, y0: int, banner_h: int) -> None:
    """三层蛋糕剪影（白色糕体 + 黄色蜡烛火苗），贴横幅右端。"""
    w = 46
    x0 = x1 - 12 - w
    base = y0 + banner_h - 10   # 蛋糕底
    d = ImageDraw.Draw(canvas)
    tiers = ((w, 12), (w - 12, 11), (w - 24, 10))  # (宽, 高) 自下而上
    ty = base
    for tw, th in tiers:
        tx = x0 + (w - tw) / 2
        d.rounded_rectangle((tx, ty - th, tx + tw, ty), radius=4, fill=WHITE)
        ty -= th + 1
    # 蜡烛 + 火苗立在顶层
    top_w = tiers[-1][0]
    for i in range(3):
        cx = x0 + (w - top_w) / 2 + top_w * (i + 0.5) / 3
        d.rectangle((cx - 1.5, ty - 9, cx + 1.5, ty), fill=WHITE)
        d.ellipse((cx - 3, ty - 16, cx + 3, ty - 9), fill=ACCENT_YELLOW)


def _balloon_cluster(canvas: Image.Image, cx: float, top_y: float, flip: bool = False) -> None:
    """一组 3 只气球（红/黄/红）+ 白色线绳，从横幅下方垂下。"""
    d = ImageDraw.Draw(canvas)
    specs = ((-26, 6, 20, SEAL_RED, 46), (2, -2, 24, ACCENT_YELLOW, 60),
             (28, 10, 17, SEAL_RED, 40))
    for dx, dy, r, color, string_len in specs:
        bx = cx + dx * (-1 if flip else 1)
        by = top_y + dy
        d.line((bx, by + r + string_len - 12, bx, by + r + 6),
               fill=WHITE, width=2)
        d.ellipse((bx - r, by - r, bx + r, by + r), fill=color)
        d.polygon([(bx - 4, by + r - 1), (bx + 4, by + r - 1), (bx, by + r + 6)],
                  fill=color)


# ========== 五套模板 ==========

def _t_ribbon(canvas: Image.Image, orientation: str, placement: Dict[str, int],
              name: str, greeting: str) -> None:
    """节假日通用：红色横幅 + 白名（左）/祝福语（右）+ 黄饰线。"""
    box = _banner_box(orientation, placement)
    x0, y0, x1, y1 = box
    h = y1 - y0
    _draw_banner(canvas, box, SEAL_RED)
    name_size = 30 if orientation == "portrait" else 26
    greet_size = 18 if orientation == "portrait" else 16
    d = ImageDraw.Draw(canvas)
    _shadow_text(canvas, (x0 + 22, y0 + (h - name_size) / 2 - 3), name, name_size, WHITE)
    if greeting:
        probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
        max_w = (x1 - x0) - 44 - probe.textlength(name, font=_get_font(name_size))
        if max_w > 60:
            greeting = _fit_text(probe, greeting, _get_font(greet_size), max_w)
            gw = probe.textlength(greeting, font=_get_font(greet_size))
            _shadow_text(canvas, (x1 - 22 - gw, y0 + (h - greet_size) / 2 - 2),
                         greeting, greet_size, WHITE_DIM)


def _t_child(canvas: Image.Image, orientation: str, placement: Dict[str, int],
             name: str, greeting: str) -> None:
    """儿童生日：黄色横幅 + 红字，横幅下两角各一组气球。"""
    box = _banner_box(orientation, placement)
    x0, y0, x1, y1 = box
    h = y1 - y0
    _draw_banner(canvas, box, ACCENT_YELLOW)
    name_size = 30 if orientation == "portrait" else 26
    greet_size = 18 if orientation == "portrait" else 16
    _shadow_text(canvas, (x0 + 22, y0 + (h - name_size) / 2 - 3), name, name_size, SEAL_RED)
    if greeting:
        probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
        max_w = (x1 - x0) - 44 - probe.textlength(name, font=_get_font(name_size))
        if max_w > 60:
            greeting = _fit_text(probe, greeting, _get_font(greet_size), max_w)
            gw = probe.textlength(greeting, font=_get_font(greet_size))
            _shadow_text(canvas, (x1 - 22 - gw, y0 + (h - greet_size) / 2 - 2),
                         greeting, greet_size, SEAL_RED)
    # 气球从横幅下沿垂下；窄横幅（让位）时只在内侧放一组
    drop_y = y1 + 6
    flip = placement.get("mode") == "left"
    _balloon_cluster(canvas, x0 + 46 if flip else x1 - 46, drop_y, flip=flip)
    if (x1 - x0) > 430:
        _balloon_cluster(canvas, x1 - 46 if flip else x0 + 46, drop_y,
                         flip=not flip)


def _t_elder(canvas: Image.Image, orientation: str, placement: Dict[str, int],
             name: str, greeting: str) -> None:
    """长辈生日：红色横幅 + 右端「寿」字白文印。"""
    box = _banner_box(orientation, placement)
    x0, y0, x1, y1 = box
    h = y1 - y0
    _draw_banner(canvas, box, SEAL_RED)
    _seal_shou(canvas, x1, y0, h)
    name_size = 30 if orientation == "portrait" else 26
    greet_size = 17 if orientation == "portrait" else 15
    d = ImageDraw.Draw(canvas)
    seal_side = max(44, h - 12)
    _shadow_text(canvas, (x0 + 22, y0 + (h - name_size) / 2 - 3), name, name_size, WHITE)
    if greeting:
        probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
        max_w = (x1 - x0) - 44 - seal_side - 12 \
            - probe.textlength(name, font=_get_font(name_size))
        if max_w > 60:
            greeting = _fit_text(probe, greeting, _get_font(greet_size), max_w)
            gw = probe.textlength(greeting, font=_get_font(greet_size))
            _shadow_text(canvas, (x1 - 12 - seal_side - 12 - gw,
                                  y0 + (h - greet_size) / 2 - 2),
                         greeting, greet_size, WHITE_DIM)


def _t_adult(canvas: Image.Image, orientation: str, placement: Dict[str, int],
             name: str, greeting: str) -> None:
    """成人生日：红色横幅 + 右端白色三层蛋糕剪影。"""
    box = _banner_box(orientation, placement)
    x0, y0, x1, y1 = box
    h = y1 - y0
    _draw_banner(canvas, box, SEAL_RED)
    _cake(canvas, x1, y0, h)
    name_size = 30 if orientation == "portrait" else 26
    greet_size = 17 if orientation == "portrait" else 15
    _shadow_text(canvas, (x0 + 22, y0 + (h - name_size) / 2 - 3), name, name_size, WHITE)
    if greeting:
        probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
        max_w = (x1 - x0) - 44 - 46 - 12 \
            - probe.textlength(name, font=_get_font(name_size))
        if max_w > 60:
            greeting = _fit_text(probe, greeting, _get_font(greet_size), max_w)
            gw = probe.textlength(greeting, font=_get_font(greet_size))
            _shadow_text(canvas, (x1 - 12 - 46 - 12 - gw,
                                  y0 + (h - greet_size) / 2 - 2),
                         greeting, greet_size, WHITE_DIM)


def _t_anniversary(canvas: Image.Image, orientation: str, placement: Dict[str, int],
                   name: str, greeting: str) -> None:
    """结婚纪念日：居中红药丸横幅（白字）+ 两侧红心内叠黄心。"""
    text = f"{name} · {greeting}" if greeting else name
    size = 20 if orientation == "portrait" else 18
    pill_h = 46 if orientation == "portrait" else 40
    canvas_w = CANVAS_W_P if orientation == "portrait" else CANVAS_W_L
    y0 = INSET + 6 if orientation == "portrait" else 6

    probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    text = _fit_text(probe, text, _get_font(size), canvas_w - 220)
    tw = probe.textlength(text, font=_get_font(size))

    mode = placement.get("mode", "default")
    if mode == "right":      # 让位后药丸在剩余自由区居中
        zone_l, zone_r = placement.get("x0", 0), canvas_w
    elif mode == "left":
        zone_l, zone_r = 0, placement.get("x1", canvas_w)
    else:
        zone_l, zone_r = 0, canvas_w
    pill_w = tw + 48
    px0 = (zone_l + zone_r) / 2 - pill_w / 2
    box = (px0, y0, px0 + pill_w, y0 + pill_h)
    d = ImageDraw.Draw(canvas)
    d.rounded_rectangle(box, radius=pill_h / 2, fill=SEAL_RED)
    _shadow_text(canvas, (px0 + pill_w / 2, y0 + (pill_h - size) / 2 - 3), text,
                 size, WHITE, anchor="mm")
    # 药丸两侧心形：红心 + 右下内叠小黄心（尺寸放大一档保证抖动后存在感）
    heart_r = 21 if orientation == "portrait" else 18
    cy = y0 + pill_h / 2
    for hx in (px0 - heart_r * 1.3, px0 + pill_w + heart_r * 1.3):
        _heart(canvas, hx, cy, heart_r, SEAL_RED)
        _heart(canvas, hx + heart_r * 0.4, cy + heart_r * 0.32, heart_r * 0.45,
               ACCENT_YELLOW)


_TEMPLATES = {
    "general_ribbon": _t_ribbon,
    "child_birthday": _t_child,
    "elder_birthday": _t_elder,
    "adult_birthday": _t_adult,
    "anniversary": _t_anniversary,
}


def apply_holiday_layer(canvas: Image.Image, layout_id: str, orientation: str,
                        holiday: Dict[str, Any]) -> None:
    """
    在版式文字层之上叠加节日庆祝元素。任何异常都不应中断每日渲染——
    失败打印警告并跳过（保留无装饰画面）。
    """
    try:
        placement = HOLIDAY_PLACEMENT.get(layout_id, {"mode": "default"})
        fn = _TEMPLATES.get(holiday.get("template", ""), _t_ribbon)
        fn(canvas, orientation, placement,
           holiday.get("name", ""), holiday.get("greeting", ""))
    except Exception as e:
        print(f"[HOLIDAY] ⚠️ 节日层绘制失败（{e}），本张按无装饰处理")
