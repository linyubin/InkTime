#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
声明式版式解释渲染器（ADR 0002）：layouts/*.json → Pillow 绘制到画布。

复用 render_layouts 的绘制原语（_glass/_scrim/_seal/_draw_dark/_fit_text/
_wrap_lines…），槽位文案的拼装口径也与被退役的逐款函数完全一致；
迁移验收：新旧实现逐像素一致（scripts/validate_style_migration.py）。

元素 kind 一览（详见 docs/plans/style-editor-2026-09-24.md）：
  glass   毛玻璃圆角卡（rect/radius/alpha/blur/shadow/highlight）
  veil    米白渐变底托（rect/a0/a1/ramp_px/rgb；a1=null 跟随全局 VEIL_ALPHA）
  text    横排文字（slot/x/y/size/fill/stroke/bold/align/cx + fit|wrap）
  vtext   竖排文字（x=首列中心/y=首行锚点/col_h/pitch/col_gap，右→左列序）
  seal    钤印（slot=city，小篆字形缺失回退字体）
  bar     饰杠/分隔线（rect/fill）
  frame   装饰边框（新预设：rect 或 margin 贴边，double=双线）
  badge   角标徽章（新预设：圆形色块 + 居中短文字）
"""

from __future__ import annotations

from typing import Any, Dict

from PIL import Image, ImageDraw

import render_layouts as rl

# 槽位枚举 → 取值函数都走 rl 的原有拼装口径（像素级一致的根基）
_SLOT_RESOLVERS = {
    "side": lambda item: item.get("side") or "",
    "city": lambda item: str(item.get("city") or "").strip(),
    "date_full": lambda item: rl._date_parts(item)[0],
    "date_year": lambda item: rl._date_parts(item)[1],
    "date_rest": lambda item: rl._date_parts(item)[2],
    "weekday": lambda item: rl.weekday_cn(item.get("date", "")),
    "meta_wd_city": lambda item: rl._meta_segments(item, with_weekday=True),
    "meta_city": lambda item: rl._meta_segments(item, with_weekday=False),
}


def resolve_slot(el: Dict[str, Any], item: Dict[str, Any]) -> str:
    """元素槽位 → 当前文案。static 用元素自带文字；未知槽位按 static 处理。"""
    slot = el.get("slot") or "static"
    if slot == "static":
        return el.get("static_text") or ""
    fn = _SLOT_RESOLVERS.get(slot)
    if fn is None:
        # 组合槽位：date_wd（日期·星期）/ wd_city（星期·城市）
        if slot == "date_wd":
            full = rl._date_parts(item)[0]
            wd = rl.weekday_cn(item.get("date", ""))
            return " · ".join(x for x in (full, wd) if x)
        if slot == "wd_city":
            wd = rl.weekday_cn(item.get("date", ""))
            city = str(item.get("city") or "").strip()
            return " · ".join(x for x in (wd, city) if x)
        return el.get("static_text") or ""
    return fn(item)


def _peer_draws(style: Dict[str, Any], item: Dict[str, Any]) -> Dict[str, bool]:
    """预判各元素是否会实际绘制（目前只有 seal 依赖城市非空，供 peer_pad 让位）。"""
    out: Dict[str, bool] = {}
    for el in style.get("elements", []):
        if el.get("kind") == "seal":
            out[el.get("name") or ""] = bool(resolve_slot(el, item))
    return out


def _draw_text_el(canvas, el: Dict[str, Any], item: Dict[str, Any],
                  peers: Dict[str, bool]) -> None:
    s = resolve_slot(el, item)
    cx = el.get("cx")
    fit = el.get("fit")
    pp = el.get("peer_pad")
    if pp and peers.get(pp.get("peer", "")):
        if cx is not None:
            cx += pp.get("dx_cx", 0)
        if fit is not None:
            fit += pp.get("dx_fit", 0)
    if not s:
        return
    size = int(el.get("size", 18))
    font_id = el.get("font")
    fill = tuple(el.get("fill") or rl.BLACK)
    stroke = float(el.get("stroke", 0.3))
    bold = bool(el.get("bold"))
    align = el.get("align", "l")
    wrap = el.get("wrap")
    if wrap:
        # 文字长度限制（2026-10-09）：先按 max_w 折行；行数超限时逐级缩字号
        # 重排（min_size 下限），避免超限直接截断丢内容。行高随字号等比缩放。
        max_lines = int(wrap.get("max_lines", 2))
        min_size = int(el.get("min_size", size))
        while size > min_size:
            if len(rl._wrap_lines(canvas, s, rl._get_font(size, font_id),
                                  wrap["max_w"], max_lines)) <= max_lines:
                break
            size -= 1
    if fit is not None:
        d = ImageDraw.Draw(canvas)
        s = rl._fit_text(d, s, rl._get_font(size, font_id), fit)
    if wrap:
        lines = rl._wrap_lines(canvas, s, rl._get_font(size, font_id),
                               wrap["max_w"], max_lines)
        line_step = int(wrap["line_step"] * size / int(el.get("size", size)))
        y = float(el["y"])
        for ln in lines:
            rl._draw_dark(canvas, el["x"], y, ln, size, fill=fill,
                          stroke=stroke, align=align, cx=cx, bold=bold,
                          font_id=font_id)
            y += line_step
        return
    rl._draw_dark(canvas, el["x"], el["y"], s, size, fill=fill,
                  stroke=stroke, align=align, cx=cx, bold=bold, font_id=font_id)


def _draw_vtext_el(canvas, el: Dict[str, Any], item: Dict[str, Any]) -> None:
    s = resolve_slot(el, item)
    if not s:
        return
    col_h, pitch, col_gap = el["col_h"], el["pitch"], el["col_gap"]
    max_cols = int(el.get("max_cols", 2))
    per_col = col_h // pitch
    cols = [s[i:i + per_col]
            for i in range(0, min(len(s), per_col * max_cols), per_col)]
    d = ImageDraw.Draw(canvas)
    font = rl._get_font(int(el.get("size", 24)), el.get("font"))
    fill = tuple(el.get("fill") or rl.BLACK)
    for ci, col in enumerate(cols):
        col_cx = el["x"] - ci * col_gap     # 传统竖排：从右往左列
        y0 = float(el["y"])
        for j, ch in enumerate(col):
            d.text((col_cx, y0 + pitch / 2 + j * pitch), ch,
                   font=font, fill=fill, anchor="mm")


def _draw_seal_el(canvas, el: Dict[str, Any], item: Dict[str, Any]) -> None:
    rl._seal(canvas, el["x"], el["y"], el["w"], el["h"],
             resolve_slot(el, item),
             vertical=bool(el.get("vertical")),
             glyph_px=int(el.get("glyph_px", 26)),
             radius=int(el.get("radius", 6)))


def _draw_veil_el(canvas, el: Dict[str, Any]) -> None:
    x0, y0, x1, y1 = el["rect"]
    a1 = el.get("a1")
    if a1 is None:
        a1 = rl.VEIL_ALPHA                     # 跟随 config.TEXT_VEIL_OPACITY
    rl._scrim(canvas, x0, y0, x1 - x0, y1 - y0,
              a0=el.get("a0", 0), a1=a1,
              ramp_px=el.get("ramp_px"),
              rgb=tuple(el.get("rgb") or (0, 0, 0)))


def _draw_bar_el(canvas, el: Dict[str, Any]) -> None:
    ImageDraw.Draw(canvas).rectangle(tuple(el["rect"]),
                                     fill=tuple(el.get("fill") or rl.SEAL_RED))


def _draw_frame_el(canvas, el: Dict[str, Any]) -> None:
    """装饰边框（新预设）：rect 或 margin 贴画布边；double 时再内缩画一道细线。"""
    if el.get("rect"):
        x0, y0, x1, y1 = el["rect"]
    else:
        m = int(el.get("margin", 12))
        x0, y0, x1, y1 = m, m, canvas.width - m, canvas.height - m
    w = float(el.get("stroke_w", 2))
    color = tuple(el.get("fill") or rl.WHITE)
    d = ImageDraw.Draw(canvas)
    d.rectangle((x0, y0, x1, y1), outline=color, width=int(w))
    if el.get("double"):
        gap = int(el.get("double_gap", 4))
        d.rectangle((x0 + gap, y0 + gap, x1 - gap, y1 - gap),
                    outline=color, width=1)


def _draw_badge_el(canvas, el: Dict[str, Any], item: Dict[str, Any]) -> None:
    """角标徽章（新预设）：圆形色块 + 居中短文字（slot 或 static）。"""
    x, y, r = el["x"], el["y"], el["r"]
    d = ImageDraw.Draw(canvas)
    d.ellipse((x - r, y - r, x + r, y + r),
              fill=tuple(el.get("fill") or rl.SEAL_RED))
    s = resolve_slot(el, item)
    if s:
        d.text((x, y), s, font=rl._get_font(int(el.get("size", 16)), el.get("font")),
               fill=tuple(el.get("text_fill") or rl.WHITE), anchor="mm",
               stroke_width=0.4, stroke_fill=tuple(el.get("text_fill") or rl.WHITE))


def render_style(canvas: Image.Image, style: Dict[str, Any],
                 item: Dict[str, Any]) -> None:
    """按声明式版式定义在已裁切好的画布上绘制文字层（元素顺序即层级，底→顶）。"""
    peers = _peer_draws(style, item)
    for el in style.get("elements", []):
        kind = el.get("kind")
        if kind == "glass":
            rl._glass(canvas, el["rect"], radius=int(el.get("radius", 14)),
                      alpha=int(el.get("alpha", 184)), blur=int(el.get("blur", 12)),
                      shadow=bool(el.get("shadow", False)),
                      highlight=bool(el.get("highlight", True)))
        elif kind == "veil":
            _draw_veil_el(canvas, el)
        elif kind == "text":
            _draw_text_el(canvas, el, item, peers)
        elif kind == "vtext":
            _draw_vtext_el(canvas, el, item)
        elif kind == "seal":
            _draw_seal_el(canvas, el, item)
        elif kind == "bar":
            _draw_bar_el(canvas, el)
        elif kind == "frame":
            _draw_frame_el(canvas, el)
        elif kind == "badge":
            _draw_badge_el(canvas, el, item)
        else:
            raise ValueError(f"未知元素类型: {kind}（版式 {style.get('id')}）")
