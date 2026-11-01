#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
节假日模板改版展示页生成器（2026-09-13）。

5 套模板 × 5 个设计方案，HTML mockup 1:1 画布（竖屏 480×800 / 横屏 800×480，
0.5 缩放呈现），底图复用 docs/layout-showcase-2026-09-11 的两张照片（base64 内嵌，
自包含单文件）。所有方案只用墨水屏四色纯色（#C80000/#DCB400/#FFFFFF/#000000），
无渐变、无半透明、无深色描边——保证后续 PIL 可直接实现。

运行：venv/bin/python scripts/generate_holiday_showcase.py
输出：docs/holiday-template-showcase-2026-09-13/index.html
"""

from __future__ import annotations

import base64
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "docs" / "holiday-template-showcase-2026-09-13"
IMG_DIR = ROOT / "docs" / "layout-showcase-2026-09-11" / "img"

RED, YELLOW, WHITE, BLACK = "#C80000", "#DCB400", "#FFFFFF", "#000000"
DIM = "#E4E2DE"  # ≈88% 白（压红底上的次级文字，同 render_layouts.WHITE_DIM）
SERIF = "'LXGW WenKai','Noto Serif SC','Songti SC',serif"


def b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


# ---------- mockup 元素助手（绝对定位，画布坐标系） ----------

def rect(x, y, w, h, bg, r=0, extra=""):
    style = f"left:{x}px;top:{y}px;width:{w}px;height:{h}px;background:{bg};"
    if r:
        style += f"border-radius:{r}px;"
    return f'<div class="el" style="{style}{extra}"></div>'


def frame(x, y, w, h, color, t, r=0):
    return (f'<div class="el" style="left:{x}px;top:{y}px;width:{w}px;height:{h}px;'
            f'border:{t}px solid {color};box-sizing:border-box;'
            f'background:none;{"border-radius:" + str(r) + "px;" if r else ""}"></div>')


def txt(x, y, s, size, color, w=600, center=False, right=None, shadow=None,
        ls=0):
    """y 为文字顶部。center=True 水平居中（x 忽略）；right=右边距。"""
    pos = f"left:{x}px;" if not center else "left:0;right:0;text-align:center;"
    if right is not None:
        pos = f"right:{right}px;text-align:right;"
    sh = f"text-shadow:{shadow};" if shadow else ""
    return (f'<div class="el txt" style="{pos}top:{y}px;font-size:{size}px;'
            f'color:{color};font-weight:{w};letter-spacing:{ls}px;{sh}font-family:{SERIF};'
            f'white-space:nowrap;">{s}</div>')


def heart(cx, cy, size, color):
    """用 ♥ 字符近似心形（PIL 实现为两圆+三角，同色纯色）。"""
    return (f'<div class="el" style="left:{cx - size / 2}px;top:{cy - size * 0.62}px;'
            f'width:{size}px;text-align:center;color:{color};font-size:{size}px;'
            f'line-height:1;">♥</div>')


def balloon(cx, cy, r, color):
    """气球：椭圆 + 同色结 + 白线绳。"""
    return (f'<div class="el" style="left:{cx - r}px;top:{cy - r}px;width:{2 * r}px;'
            f'height:{2 * r}px;background:{color};border-radius:50%;"></div>'
            f'<div class="el" style="left:{cx - 4}px;top:{cy + r - 3}px;width:8px;height:5px;'
            f'background:{color};"></div>'
            f'<div class="el" style="left:{cx - 1}px;top:{cy + r + 2}px;width:2px;'
            f'height:{r * 1.4:.0f}px;background:{WHITE};"></div>')


def candle(cx, y, h=26):
    return (f'<div class="el" style="left:{cx - 3}px;top:{y}px;width:6px;height:{h}px;'
            f'background:{RED};"></div>'
            f'<div class="el" style="left:{cx - 5}px;top:{y - 9}px;width:10px;height:10px;'
            f'background:{YELLOW};border-radius:50% 50% 50% 0;transform:rotate(-45deg);"></div>')


def seal(x, y, size, chars, fs, bg=WHITE, fg=RED, vertical=False):
    inner = chars if not vertical else "<br>".join(chars)
    lh = fs + 4
    return (f'<div class="el" style="left:{x}px;top:{y}px;width:{size}px;height:{size}px;'
            f'background:{bg};border-radius:10px;display:flex;align-items:center;'
            f'justify-content:center;"><span style="color:{fg};font-size:{fs}px;'
            f'font-weight:700;line-height:{lh}px;font-family:{SERIF};'
            f'text-align:center;">{inner}</span></div>')


def pill(x, y, w, h, s, fs, fg=RED, bg=WHITE):
    return (f'<div class="el" style="left:{x}px;top:{y}px;width:{w}px;height:{h}px;'
            f'background:{bg};border-radius:{h / 2}px;display:flex;align-items:center;'
            f'justify-content:center;padding:0 14px;box-sizing:border-box;">'
            f'<span style="color:{fg};font-size:{fs}px;font-weight:600;'
            f'font-family:{SERIF};white-space:nowrap;">{s}</span></div>')


# ---------- 画布 ----------

PW, PH = 480, 800
LW, LH = 800, 480
TOP_P, TOP_L = 40, 0  # 竖屏可视顶 = 遮挡 inset 40；横屏顶边不被遮


SC = 0.4  # mockup 缩放（竖屏 192×320 / 横屏 320×192，卡片内两图并排）


def stage(orient: str, inner: str, label: str) -> str:
    if orient == "portrait":
        w, h, cls = PW, PH, "portrait"
    else:
        w, h, cls = LW, LH, "landscape"
    return (f'<div class="shotwrap" style="width:{w * SC:.0f}px;">'
            f'<div class="shot" style="width:{w * SC:.0f}px;height:{h * SC:.0f}px;">'
            f'<div class="stage {cls}" style="width:{w}px;height:{h}px;'
            f'transform:scale({SC});">'
            f'<div class="bg bg-{cls}"></div>{inner}</div></div>'
            f'<span class="olabel">{label}</span></div>')


def both(P: str, L: str) -> tuple:
    return (stage("portrait", P, "竖屏 480×800"),
            stage("landscape", L, "横屏 800×480"))


def banner_rect(o, y, h, bg, x0=0, x1=None):
    """顶部横幅：底角圆角 + 顶角方角（与现行 PIL 实现同款）。"""
    W = PW if o == "portrait" else LW
    x1 = W if x1 is None else x1
    return rect(x0, y, x1 - x0, h, bg, r=16) + rect(x0, y, x1 - x0, 16, bg)


# ======================================================================
# 五套模板 × 五方案（每个函数返回 [(竖mockup, 横mockup), ...×5]）
# ======================================================================

def t1(o):  # ---- 节假日通用（示例文案：春节） ----
    TOP = TOP_P if o == "portrait" else TOP_L
    W = PW if o == "portrait" else LW
    H = PH if o == "portrait" else LH
    name, greet = "春节", "新春快乐 · 阖家团圆"
    bh, ns, gs = (72, 30, 17) if o == "portrait" else (56, 26, 15)

    a = banner_rect(o, TOP, bh, RED)
    a += rect(0, TOP + bh - 6, W, 6, YELLOW)
    a += rect(0, TOP + bh - 8, W, 2, WHITE)
    a += txt(22, TOP + (bh - ns) // 2 - 4, name, ns, WHITE, 700, shadow="0 2px 0 #000")
    a += txt(0, TOP + (bh - gs) // 2 + 2, greet, gs, DIM, 500, right=22)
    A = both(a, a)

    b_y = TOP + 8
    b = seal(W - 16 - 88, b_y, 88, name, 28, vertical=True)
    b += rect(W - 16 - 88 - 10, b_y + 88 + 8, 22, 22, YELLOW, r=4)
    b += pill(18, b_y + 4, 210, 38, greet, 15)
    B = both(b, b)

    c = (rect(0, TOP, W, 6, RED) + rect(0, TOP + 6, W, 46, WHITE)
         + rect(0, TOP + 52, W, 6, YELLOW))
    c += txt(0, TOP + 17, name, 24, RED, 700, center=True, ls=8)
    c += txt(0, TOP + 24, "二〇二七 · 丁未年", 13, BLACK, 400, right=18)
    C = both(c, c)

    d = rect(18, TOP + 10, 200, 64, WHITE, r=14)
    d += balloon(48, TOP + 38, 13, RED) + balloon(76, TOP + 33, 11, YELLOW)
    d += txt(98, TOP + 27, name, 24, RED, 700)
    d += pill(W - 228, TOP + 22, 210, 38, greet, 15)
    D = both(d, d)

    ins = TOP + 12
    e = frame(ins, ins, W - 2 * ins, H - 2 * ins, RED, 10, r=4)
    e += frame(ins + 14, ins + 14, W - 2 * (ins + 14), H - 2 * (ins + 14), YELLOW, 4, r=2)
    e += rect(W // 2 - 100, ins - 24, 200, 48, WHITE, r=8)
    e += txt(0, ins - 14, f"『{name}』", 24, RED, 700, center=True)
    e += rect(ins - 5, ins - 5, 10, 10, YELLOW) + rect(W - ins - 5, ins - 5, 10, 10, YELLOW)
    E = both(e, e)
    return A, B, C, D, E


def t2(o):  # ---- 儿童生日（示例文案：女儿的生日） ----
    TOP = TOP_P if o == "portrait" else TOP_L
    W = PW if o == "portrait" else LW
    H = PH if o == "portrait" else LH
    name, greet = "女儿的生日", "生日快乐"

    bh = 72 if o == "portrait" else 56
    a = banner_rect(o, TOP, bh, YELLOW)
    a += txt(22, TOP + (bh - 30) // 2 - 4, name, 30, RED, 700, shadow="0 2px 0 #000")
    a += txt(0, TOP + (bh - 17) // 2 + 2, greet, 17, RED, 500, right=22)
    a += balloon(52, TOP + bh + 18, 17, RED) + balloon(86, TOP + bh + 10, 21, YELLOW)
    a += balloon(W - 52, TOP + bh + 18, 17, RED) + balloon(W - 86, TOP + bh + 10, 21, YELLOW)
    A = both(a, a)

    b = rect(0, TOP + 2, W, 4, WHITE)
    n = 16
    seg = W / n
    for i in range(n):
        color = RED if i % 2 == 0 else YELLOW
        b += (f'<div class="el" style="left:{i * seg + (seg - 24) / 2}px;top:{TOP + 6}px;'
              f'width:0;height:0;border-left:12px solid transparent;'
              f'border-right:12px solid transparent;border-top:22px solid {color};"></div>')
    b += rect(18, TOP + 46, 186, 66, WHITE, r=12)
    b += txt(34, TOP + 56, name, 20, RED, 700)
    b += txt(34, TOP + 84, greet + " · 又一岁啦", 13, BLACK, 400)
    B = both(b, b)

    c = rect(0, TOP, W, 8, YELLOW)
    c += rect(W - 16 - 124, TOP + 20, 124, 144, WHITE, r=16)
    cake_cx = W - 16 - 62
    for k, (tw, th) in enumerate(((72, 16), (54, 14), (36, 12))):
        c += rect(cake_cx - tw / 2, TOP + 46 + 30 * k + 14 * k, tw, th, RED, r=4)
    for i in range(3):
        c += candle(cake_cx - 20 + 20 * i, TOP + 36, 16)
    c += txt(0, TOP + 134, greet, 16, RED, 700, center=True)
    c += pill(18, TOP + 24, 162, 40, name, 18)
    C = both(c, c)

    d = ""
    bx, by = W - 78, TOP + 170
    for dx, dy, r, col in ((-30, 0, 20, RED), (0, -26, 24, YELLOW), (28, 4, 18, RED),
                           (-14, -46, 18, YELLOW), (16, -52, 16, RED)):
        d += balloon(bx + dx, by + dy, r, col)
    d += pill(18, TOP + 20, 200, 42, f"{name} · {greet}", 15)
    D = both(d, d)

    bh2 = 64
    e = banner_rect(o, TOP, bh2, YELLOW)
    for i in range(W // 22 + 1):
        e += (f'<div class="el" style="left:{i * 22 - 11}px;top:{TOP + bh2 - 1}px;'
              f'width:22px;height:22px;background:{YELLOW};border-radius:50%;"></div>')
    e += txt(0, TOP + 8, name, 28, RED, 700, center=True, shadow="0 2px 0 #000")
    e += txt(0, TOP + 42, greet, 14, WHITE, 500, center=True)
    E = both(e, e)
    return A, B, C, D, E


def t3(o):  # ---- 长辈生日（示例文案：爸爸70大寿） ----
    TOP = TOP_P if o == "portrait" else TOP_L
    W = PW if o == "portrait" else LW
    H = PH if o == "portrait" else LH
    name, greet = "爸爸70大寿", "福如东海 · 寿比南山"
    bh = 72 if o == "portrait" else 56

    a = banner_rect(o, TOP, bh, RED)
    a += txt(22, TOP + (bh - 30) // 2 - 4, name, 30, WHITE, 700, shadow="0 2px 0 #000")
    a += txt(0, TOP + (bh - 16) // 2 + 2, greet, 16, DIM, 500, right=22)
    a += seal(W - 12 - 52, TOP + (bh - 52) // 2, 52, "寿", 30)
    A = both(a, a)

    fs = 120 if o == "portrait" else 96
    b = txt(0, TOP - 6, "壽", fs, RED, 700, center=True, shadow=f"6px 6px 0 {WHITE}")
    b += pill(W // 2 - 165, TOP + fs + 14, 330, 42, greet, 18)
    B = both(b, b)

    c = rect(0, 0, 18, H, RED) + rect(18, 0, 4, H, YELLOW)
    c += rect(W - 18, 0, 18, H, RED) + rect(W - 22, 0, 4, H, YELLOW)
    c += rect(W // 2 - 74, TOP + 4, 148, 62, WHITE, r=10)
    c += txt(0, TOP + 12, name, 22, RED, 700, center=True)
    c += txt(0, TOP + 40, greet, 12, BLACK, 400, center=True)
    C = both(c, c)

    d = seal(18, TOP + 8, 76, "福", 38) + seal(W - 18 - 76, TOP + 8, 76, "壽", 38)
    d += pill(W // 2 - 175, H - (140 if o == "portrait" else 118), 350, 42, greet, 18)
    D = both(d, d)

    ins = TOP + 12
    e = frame(ins, ins, W - 2 * ins, H - 2 * ins, RED, 8, r=4)
    e += frame(ins + 14, ins + 14, W - 2 * (ins + 14), H - 2 * (ins + 14), YELLOW, 4, r=2)
    e += seal(W // 2 - 28, ins - 6, 56, "壽", 30, bg=YELLOW, fg=RED)
    e += rect(W // 2 - 175, H - ins - 52, 350, 44, RED, r=10)
    e += txt(0, H - ins - 43, greet, 18, WHITE, 600, center=True)
    E = both(e, e)
    return A, B, C, D, E


def t4(o):  # ---- 成人生日（示例文案：生日快乐） ----
    TOP = TOP_P if o == "portrait" else TOP_L
    W = PW if o == "portrait" else LW
    H = PH if o == "portrait" else LH
    name, greet = "生日快乐", "愿岁岁常欢愉"
    bh = 72 if o == "portrait" else 56

    a = banner_rect(o, TOP, bh, RED)
    a += txt(22, TOP + (bh - 30) // 2 - 4, name, 30, WHITE, 700, shadow="0 2px 0 #000")
    a += txt(0, TOP + (bh - 16) // 2 + 2, greet, 16, DIM, 500, right=22)
    cx = W - 12 - 23
    base = TOP + bh - 12
    for tw, th in ((46, 12), (34, 11), (22, 10)):
        a += rect(cx - tw / 2, base - th, tw, th, WHITE, r=4)
        base -= th + 1
    for i in range(3):
        a += (f'<div class="el" style="left:{cx - 20 + 20 * i - 2}px;top:{base - 9}px;'
              f'width:4px;height:8px;background:{WHITE};"></div>'
              f'<div class="el" style="left:{cx - 20 + 20 * i - 4}px;top:{base - 17}px;'
              f'width:8px;height:8px;background:{YELLOW};'
              f'border-radius:50% 50% 50% 0;transform:rotate(-45deg);"></div>')
    A = both(a, a)

    dd = 100
    b = (f'<div class="el" style="left:{W - 16 - dd}px;top:{TOP + 10}px;width:{dd}px;'
         f'height:{dd}px;background:{WHITE};border-radius:50%;"></div>'
         f'<div class="el" style="left:{W - 16 - dd + 7}px;top:{TOP + 17}px;width:{dd - 14}px;'
         f'height:{dd - 14}px;border:3px solid {YELLOW};border-radius:50%;'
         f'box-sizing:border-box;background:none;"></div>')
    b += txt(0, TOP + 36, "HAPPY", 15, RED, 700, center=True, ls=2)
    b += txt(0, TOP + 55, "BIRTHDAY", 15, RED, 700, center=True, ls=2)
    b += pill(18, TOP + 22, 180, 40, greet, 17)
    B = both(b, b)

    c = rect(0, TOP, W, 10, RED)
    gx, gy = W - 128, int(H * 0.52)
    c += rect(gx, gy, 70, 56, RED, r=6)
    c += rect(gx + 29, gy, 12, 56, WHITE)
    c += (f'<div class="el" style="left:{gx + 12}px;top:{gy - 14}px;width:0;height:0;'
          f'border-left:16px solid transparent;border-right:16px solid transparent;'
          f'border-bottom:14px solid {YELLOW};transform:rotate(-24deg);"></div>'
          f'<div class="el" style="left:{gx + 30}px;top:{gy - 14}px;width:0;height:0;'
          f'border-left:16px solid transparent;border-right:16px solid transparent;'
          f'border-bottom:14px solid {YELLOW};transform:rotate(24deg);"></div>'
          f'<div class="el" style="left:{gx + 30}px;top:{gy - 8}px;width:10px;height:10px;'
          f'background:{YELLOW};border-radius:50%;"></div>')
    c += pill(18, TOP + 22, 210, 42, f"{name} · {greet}", 15)
    C = both(c, c)

    d2 = rect(0, TOP, W, 56, WHITE) + rect(0, TOP + 56, W, 4, YELLOW)
    d2 += txt(22, TOP + 13, name, 28, RED, 700)
    d2 += txt(0, TOP + 22, "HAPPY  BIRTHDAY", 14, BLACK, 400, right=22, ls=2)
    D = both(d2, d2)

    pw2 = 320 if o == "portrait" else 360
    e = rect(W // 2 - pw2 // 2, TOP + 8, pw2, 84, WHITE, r=14)
    for i in range(5):
        e += candle(W // 2 - 80 + 40 * i, TOP + 22, 24)
    e += txt(0, TOP + 58, greet, 18, RED, 600, center=True)
    E = both(e, e)
    return A, B, C, D, E


def t5(o):  # ---- 结婚纪念日（示例文案：结婚纪念日） ----
    TOP = TOP_P if o == "portrait" else TOP_L
    W = PW if o == "portrait" else LW
    H = PH if o == "portrait" else LH
    name, greet = "结婚纪念日", "永结同心"
    label = f"{name} · {greet}"

    pw = 258
    a = pill(W // 2 - pw // 2, TOP + 6, pw, 44, label, 17)
    hr = 21 if o == "portrait" else 18
    a += heart(W // 2 - pw // 2 - hr * 1.8, TOP + 28, hr * 2, RED)
    a += heart(W // 2 + pw // 2 + hr * 1.8, TOP + 28, hr * 2, RED)
    A = both(a, a)

    cy = TOP + 42
    # 两环交叠 28px（红环先画，黄环压上，交叠处黄盖红——PIL 同序实现即得相扣感）
    b = (f'<div class="el" style="left:{W // 2 - 62}px;top:{cy - 26}px;width:52px;'
         f'height:52px;border:9px solid {RED};border-radius:50%;'
         f'box-sizing:border-box;background:none;"></div>'
         f'<div class="el" style="left:{W // 2 - 34}px;top:{cy - 26}px;width:52px;'
         f'height:52px;border:9px solid {YELLOW};border-radius:50%;'
         f'box-sizing:border-box;background:none;"></div>')
    b += pill(W // 2 - 152, cy + 46, 304, 42, label, 17)
    B = both(b, b)

    c = seal(W - 16 - 84, TOP + 8, 84, "囍", 42, bg=RED, fg=WHITE)
    c += pill(18, TOP + 18, 150, 42, greet, 18)
    C = both(c, c)

    ins = TOP + 12
    d = frame(ins, ins, W - 2 * ins, H - 2 * ins, RED, 6, r=2)
    for cx2, cy2 in ((ins, ins), (W - ins, ins), (ins, H - ins), (W - ins, H - ins)):
        d += heart(cx2, cy2, 26, YELLOW)
    d += pill(W // 2 - 142, TOP + 4, 284, 42, label, 17)
    D = both(d, d)

    bh = 60
    e = banner_rect(o, TOP, bh, RED)
    e += txt(0, TOP + (bh - 20) // 2, label, 20, WHITE, 600, center=True,
             shadow="0 2px 0 #000")
    for i in range(W // 48 + 1):
        e += heart(i * 48 + 24, TOP + bh + 8, 18, RED)
    E = both(e, e)
    return A, B, C, D, E


# ======================================================================
# HTML 组装
# ======================================================================

TEMPLATES = [
    ("T1", "general_ribbon", "节假日通用", "春节 / 国庆 / 中秋等一切节日", t1, [
        ("A", "刊头横幅（现行基线优化）", "现行方案微调：下沿饰线改为黄 6px + 白 2px 双色，层次更精致。稳妥、信息一目了然。"),
        ("B", "竖排方章", "右上竖排红底白文方章 + 黄色小副章，左上白胶囊祝福。零大面积遮挡，中式雅致。"),
        ("C", "报头双线", "报纸刊头风：红/白/黄三色横带，节日名居中加宽字距，右侧干支年号。庄严、版式感强。"),
        ("D", "灯笼贴纸", "左上白底贴纸内画一对灯笼 + 节日名，右上祝福胶囊。轻快喜庆，遮挡极少。"),
        ("E", "仪式画框", "红 10px + 黄 4px 双层描金画框全包，顶部白色名牌嵌框上。仪式感最强，适合『大日子』。"),
    ]),
    ("T2", "child_birthday", "儿童生日", "孩子生日 / 儿童节", t2, [
        ("A", "气球黄幅（现行基线优化）", "现行方案微调：气球改为一红一黄错落双组，更饱满。"),
        ("B", "彩旗派对", "顶部一排红黄三角彩旗，左下白底名牌。派对氛围浓，几乎不遮照片。"),
        ("C", "蛋糕贴纸", "右上白色大贴纸：三层蛋糕 + 三支蜡烛，左上名牌。聚焦『蛋糕』意象。"),
        ("D", "气球束", "右侧悬浮一大束五只气球（红黄相间白绳），左上祝福胶囊。画面最生动。"),
        ("E", "波浪黄幅", "黄色横幅底边一圈半圆波浪（像蛋糕花边），居中两行文字。甜度最高。"),
    ]),
    ("T3", "elder_birthday", "长辈生日", "长辈寿辰 / 母亲节父亲节", t3, [
        ("A", "横幅寿印（现行基线优化）", "现行方案：红横幅 + 右端白底红文寿章。"),
        ("B", "大字中堂", "书法大字「壽」直接压照片（白色错位影保证可读），下方白胶囊对联句。气势最足，适合上部较亮的照片。"),
        ("C", "楹联双线", "左右红竖线 + 黄细线如楹联，顶部白牌写寿辰。遮挡最少（约 4% 画面）。"),
        ("D", "福寿对章", "左「福」右「壽」两枚方章对称悬挂，下方白胶囊横批。对称、稳重。"),
        ("E", "描金画框", "红黄双层画框 + 顶部黄底寿章 + 底部红底白字横批，匾额式收尾。寿宴仪式感。"),
    ]),
    ("T4", "adult_birthday", "成人生日", "自己 / 朋友 / 配偶生日", t4, [
        ("A", "横幅蛋糕（现行基线优化）", "现行方案：红横幅 + 右端白色蛋糕剪影 + 黄蜡烛。"),
        ("B", "极简圆徽", "右上白圆徽 + 黄色细环，HAPPY BIRTHDAY 两行红字。克制、成人感。"),
        ("C", "礼盒丝带", "顶部细红线 + 右中悬浮红礼盒（白丝带黄蝴蝶结），左上胶囊。符号明确不喧闹。"),
        ("D", "杂志刊头", "白色刊头条：左侧大红字、右侧小号英文字距排开，黄线收边。编辑排版感，最『设计』。"),
        ("E", "烛光一排", "居中白色圆角面板：五支蜡烛一排 + 祝福语。安静温暖，遮挡集中。"),
    ]),
    ("T5", "anniversary", "结婚纪念日", "结婚周年 / 情人节", t5, [
        ("A", "药丸双心（现行基线优化）", "现行方案：居中红药丸 + 两侧爱心。"),
        ("B", "双戒相扣", "顶部红黄两枚相扣圆环 + 白胶囊横批。符号直白，不遮画面。"),
        ("C", "红囍印章", "右上红底白文「囍」章 + 左上胶囊。中式婚姻意象，最传统。"),
        ("D", "细框心角", "细红画框 + 四角黄色小心，顶部胶囊。全包围但不压迫，浪漫克制。"),
        ("E", "心形旗语", "红色横幅 + 底边一排悬垂小心形，像爱心旗语。浪漫且俏皮。"),
    ]),
]


def build_html() -> str:
    sections = ""
    for tid, tpl_id, cn, scene, fn, proposals in TEMPLATES:
        p_results = fn("portrait")
        l_results = fn("landscape")
        cards = ""
        for i, (pid, pname, desc) in enumerate(proposals):
            p_stage = p_results[i][0]
            l_stage = l_results[i][1]
            tag = "<span class='tag'>现行基线</span>" if "现行" in pname else ""
            cards += f"""
      <article class="card" data-id="{tid}-{pid}">
        <header><b>{tid}-{pid}</b>{pname}{tag}<span class="tpl">{tpl_id}</span></header>
        <div class="shots">{p_stage}{l_stage}</div>
        <p class="desc">{desc}</p>
      </article>"""
        sections += f"""
    <section>
      <h2>{tid} · {cn} <code>{tpl_id}</code></h2>
      <p class="scene">适用：{scene}。每张卡片左：竖屏 480×800，右：横屏 800×480（1:1 画布，0.5 缩放呈现）</p>
      <div class="grid">{cards}</div>
    </section>"""

    return f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>节假日模板改版提案 · 5×5 方案（2026-09-13）</title>
<style>
*{{box-sizing:border-box}}
body{{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;
  margin:0 auto;max-width:1180px;padding:28px 20px 80px;color:#1f2328;line-height:1.65}}
h1{{font-size:24px;margin:0 0 6px}}
h2{{font-size:19px;border-bottom:2px solid #d0d7de;padding-bottom:8px;margin:44px 0 4px}}
h2 code{{font-size:13px;background:#f6f8fa;padding:2px 8px;border-radius:6px;margin-left:8px}}
.scene{{color:#656d76;font-size:13px;margin:6px 0 16px}}
.legend span{{display:inline-block;width:14px;height:14px;border-radius:3px;
  vertical-align:-2px;margin:0 4px 0 14px;border:1px solid #d0d7de}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(500px,1fr));gap:18px}}
.card{{border:2px solid #d0d7de;border-radius:14px;padding:14px;background:#fff;
  cursor:pointer;transition:border-color .15s}}
.card:hover{{border-color:#C80000}}
.card.picked{{border-color:#C80000;box-shadow:0 0 0 3px rgba(200,0,0,.15)}}
.card header{{font-size:15px;margin-bottom:10px}}
.card header b{{color:#C80000;margin-right:8px}}
.tag{{background:#DCB400;color:#5b4a00;font-size:11px;padding:1px 8px;
  border-radius:8px;margin-left:8px;vertical-align:2px}}
.tpl{{float:right;color:#8c959f;font-size:11px;font-family:monospace}}
.shots{{display:flex;gap:14px;align-items:flex-start}}
.shotwrap{{flex:none}}
.shot{{position:relative;flex:none;border-radius:8px;overflow:hidden;
  box-shadow:0 2px 10px rgba(0,0,0,.18)}}
.stage{{position:relative;transform-origin:top left}}
.stage .bg{{position:absolute;inset:0;background-size:cover;background-position:center}}
.bg.bg-portrait{{background-image:/*PHOTO_P*/}}
.bg.bg-landscape{{background-image:/*PHOTO_L*/}}
.el{{position:absolute}}
.el.txt{{line-height:1.1}}
.olabel{{display:block;text-align:center;color:#8c959f;font-size:11px;margin-top:4px}}
.desc{{font-size:13px;color:#424a53;margin:12px 0 0}}
.note{{background:#fff8c6;border:1px solid #e8d90f;border-radius:10px;
  padding:10px 16px;font-size:13px;margin:14px 0}}
#picks{{position:fixed;bottom:0;left:0;right:0;background:#1f2328;color:#fff;
  padding:10px 20px;font-size:13px;display:none;z-index:9}}
#picks button{{float:right;background:#C80000;color:#fff;border:0;border-radius:8px;
  padding:4px 14px;cursor:pointer}}
</style></head><body>
<h1>节假日模板改版提案 · 5 套模板 × 5 方案</h1>
<p>2026-09-13 · 按网页扁平设计的标准重出提案。所有方案严格约束在墨水屏四色内：
<span class="legend"><span style="background:#C80000"></span>红 #C80000
<span style="background:#DCB400"></span>黄 #DCB400
<span style="background:#FFFFFF"></span>白
<span style="background:#000000"></span>黑</span>
——无渐变、无半透明、无深色描边，全部元素可直接用 PIL 矢量实现。
mockup 底图沿用版式展示页同款照片，画布 1:1。</p>
<p class="note">👉 点击卡片标记「喜欢」（左下角自动汇总编号），选好后把编号发我即可按方案实施。
标「现行基线」的 A 方案是当前已上线的样子，供对比。</p>
{sections}
<div id="picks"></div>
<script>
const KEY='holiday-showcase-picks';
let picks=new Set(JSON.parse(localStorage.getItem(KEY)||'[]'));
function render(){{
  document.querySelectorAll('.card').forEach(c=>{{
    c.classList.toggle('picked',picks.has(c.dataset.id));}});
  const bar=document.getElementById('picks');
  if(picks.size){{
    bar.style.display='block';
    bar.innerHTML='已选：'+[...picks].join('、')+
      ' <button onclick="localStorage.removeItem(KEY);picks.clear();render()">清空</button>';
  }} else bar.style.display='none';
}}
document.querySelectorAll('.card').forEach(c=>c.onclick=()=>{{
  const id=c.dataset.id;
  picks.has(id)?picks.delete(id):picks.add(id);
  localStorage.setItem(KEY,JSON.stringify([...picks]));render();}});
render();
</script></body></html>"""


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    photo_p = b64(IMG_DIR / "p_portrait.jpg")
    photo_l = b64(IMG_DIR / "p_landscape.jpg")
    html = build_html()
    # 底图通过 CSS 类只内嵌一次（50 个 mockup 复用同一 background-image）
    html = html.replace("/*PHOTO_P*/", f"url(data:image/jpeg;base64,{photo_p})")
    html = html.replace("/*PHOTO_L*/", f"url(data:image/jpeg;base64,{photo_l})")
    out = OUT_DIR / "index.html"
    out.write_text(html, encoding="utf-8")
    print(f"[OK] {out}（{out.stat().st_size / 1024:.0f} KB）")


if __name__ == "__main__":
    main()
