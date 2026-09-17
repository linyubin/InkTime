#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
按照片格局自动选排版式（2026-09-12 设计定稿）。

设计文档与打分表：docs/layout-showcase-2026-09-11/index.html 「选型策略」章节。
核心原则：
- 零新增分析成本：信号全部来自现有管线（choose_gamma 的白率估计、YOLO subjects_json、
  VLM type/caption 关键词、photos.db 的 memory_score）。
- 加分制打分 + 硬否决（主体不被遮挡优先级最高）+ 确定性哈希平局
  （同一照片永远同一版式，跨照片分布均匀）。
- 信号缺失 / 全负分 → 回落基线 P01 / L01。

保留版式（2026-09-11 评审裁撤 P02/P04/P07/P09/L03/L09）：
  竖屏 P01/P03/P05/P08/P10（480×800）
  横屏 L01/L02/L04/L05/L06/L07/L10（800×480）
无城市时否决带钤印的 P05/L07（红章无内容可镌，2026-09-12）。
文案显示宽度超过约 16 汉字 + 2 标点时否决单行款 P01/P10/P05（2026-09-13）。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Optional, Tuple

PORTRAIT_LAYOUTS = ("P01", "P03", "P05", "P08", "P10")
LANDSCAPE_LAYOUTS = ("L01", "L02", "L04", "L05", "L06", "L07", "L10")


LAYOUT_NAMES = {
    "P01": "底部玻璃卡", "P03": "角落手记", "P05": "竖排题签·钤印",
    "P08": "大字日期", "P10": "全幅字幕",
    "L01": "底部玻璃卡", "L02": "左侧信息栏", "L04": "电影字幕",
    "L05": "报纸横条", "L06": "顶签悬挂", "L07": "落款钤印",
    "L10": "中央铭牌",
}

# 选型后传给 compute_crop_window 的避让（px，画布坐标系）。
# 实色条方案（L05 报纸横条）条身不透明且被当作设计元素，避让为 0。
CROP_RESERVES: Dict[str, Dict[str, int]] = {
    "P01": {"bottom": 130},
    "P03": {"bottom": 140},
    "P05": {"right": 110},
    "P08": {"bottom": 140},
    "P10": {"bottom": 140},
    "L01": {"bottom": 110},
    "L02": {"left": 220},
    "L04": {"bottom": 120},
    "L05": {},
    "L06": {"top": 180},
    "L07": {"bottom": 150},
    "L10": {"bottom": 140},
}

# 单行版式（P01/P10/P05）的文案显示宽度上限：约 16 汉字 + 2 标点。
# 全宽字符计 1、半宽计 0.5；超限时选型硬否决这三款（它们会对长文案省略号截断）。
CAPTION_SINGLE_LINE_MAX = 18.0

# 高低调阈值（predicted_white_ratio，与 choose_gamma 同一口径）
HIGH_KEY = 0.55
LOW_KEY = 0.35

# ---- 风格标签关键词（type + caption + reason + city 上匹配）----
_ZH_KW = ("古镇", "园林", "庙", "塔", "寺", "水乡", "祠", "亭", "牌坊", "城墙",
          "古街", "古巷", "灯笼", "汉服", "旗袍", "书法", "水墨", "庭院", "石桥", "古")
_SNOW_KW = ("雪", "冰", "雾凇")
_NIGHT_KW = ("夜景", "夜晚", "灯光", "霓虹", "灯会", "烟花", "夜色", "夜幕")
_TRAVEL_KW = ("旅行", "风景", "山水", "湖", "海", "山", "徒步", "登山",
              "草原", "沙漠", "梯田", "峡谷", "瀑布", "海岸")

# 简体 → 钤印小篆字形（seal_glyphs/<hex>.png，汉典小篆字形）。
# 小篆时代尚无简化字，印面用对应繁体篆形（符合篆刻惯例）。
_SEAL_GLYPH_MAP = {"厦": "5EC8", "门": "9580", "市": "5E02",
                   "木": "6728", "渎": "7006", "镇": "93AE"}
_SEAL_STRIP_SUFFIX = ("市", "镇", "县", "区")


def style_tags(item: Dict[str, Any]) -> Dict[str, bool]:
    """在 类型 / 文案 / 理由 / 城市 上做关键词命中。"""
    text = " ".join(str(item.get(k) or "") for k in
                    ("type_raw", "side", "reason", "city"))
    return {
        "zh": any(k in text for k in _ZH_KW),
        "snow": any(k in text for k in _SNOW_KW),
        "night": any(k in text for k in _NIGHT_KW),
        "travel": any(k in text for k in _TRAVEL_KW),
    }


def largest_subject(subjects_json: str) -> Optional[Dict[str, float]]:
    """
    解析 subjects_json，返回加权最大的主体 {"cx","cy","area"}（归一化 0–1）。
    权重口径与 compute_crop_window 一致：conf × 类别基础权重，再乘面积。
    无有效主体返回 None。
    """
    if not subjects_json:
        return None
    try:
        subs = json.loads(subjects_json)
    except Exception:
        return None
    weights = {"person": 5.0, "cat": 4.0, "dog": 4.0}
    best, best_w = None, 0.0
    for s in subs if isinstance(subs, list) else []:
        try:
            label, conf, bbox = s.get("label"), float(s.get("conf", 0)), s.get("bbox")
        except Exception:
            continue
        if label not in weights or conf < 0.2 or not bbox or len(bbox) != 4:
            continue
        cx, cy, bw, bh = (float(v) for v in bbox)
        area = max(0.0, bw) * max(0.0, bh)
        w = conf * weights[label] * area
        if w > best_w:
            best_w, best = w, {"cx": cx, "cy": cy, "area": area,
                               "bbox": (cx - bw / 2, cy - bh / 2,
                                        cx + bw / 2, cy + bh / 2)}
    return best


def _display_width(text: str) -> float:
    """文案显示宽度：全宽字符（CJK/全角标点）计 1，半宽计 0.5。"""
    return sum(1.0 if ord(ch) > 0x2E7F else 0.5 for ch in str(text or ""))


def extract_signals(item: Dict[str, Any], white_ratio: float) -> Dict[str, Any]:
    """汇总选型信号（供打分与验证报告使用）。"""
    sub = largest_subject(item.get("subjects_json", ""))
    tags = style_tags(item)
    S = sub["area"] if sub else 0.0
    cx = sub["cx"] if sub else 0.5
    cy = sub["cy"] if sub else 0.5
    return {
        "white_ratio": white_ratio,
        "high": white_ratio >= HIGH_KEY,
        "low": white_ratio <= LOW_KEY,
        "S": S,
        "has_subject": sub is not None,
        "cx": cx,
        "cy": cy,
        "caption_w": _display_width(item.get("side", "")),
        "city_ok": bool(str(item.get("city") or "").strip()),
        "sym": abs(cx - 0.5) < 0.08 and S >= 0.08,
        "tags": tags,
        "memory": item.get("memory", -1.0),
    }


def _score_portrait(g: Dict[str, Any]) -> Dict[str, float]:
    I, hi, lo = g["white_ratio"], g["high"], g["low"]
    S, cx, cy = g["S"], g["cx"], g["cy"]
    no_sub, sym, tags = not g["has_subject"], g["sym"], g["tags"]
    mem_ok = g["memory"] >= 85
    # 权重经 2026-09-12 首轮 50 张随机回归调平：基线降辅助分、专项版式加主触发分，
    # 避免基线垄断（首版 P01+L01 合计占 64%，目标每款 10–25%）。
    return {
        "P01": 1.0 + .3 * (not hi and not lo) + .2 * no_sub
               - .8 * (S >= .12 and cy > .6),
        "P03": 1.0 * lo + .5 * (cx > .35 or cy < .55) + .2 * no_sub - .6 * hi,
        "P05": 1.0 * hi + .9 * tags["zh"] + .5 * (cx < .45) - .3 * lo,
        "P08": .9 * (cx < .4) + .6 * (I > .45 and S < .12)
               + .3 * mem_ok - .8 * (cx > .6),
        "P10": 1.0 * sym + .6 * (S >= .12 and abs(cx - .5) < .15)
               + .4 * lo - .4 * (cy > .65),
    }


def _score_landscape(g: Dict[str, Any]) -> Dict[str, float]:
    hi, lo = g["high"], g["low"]
    S, cx, cy = g["S"], g["cx"], g["cy"]
    no_sub, sym, tags = not g["has_subject"], g["sym"], g["tags"]
    return {
        "L01": 1.0 + .3 * (not hi and not lo) + .2 * no_sub
               - .6 * (S >= .12 and cy > .6),
        "L02": .9 * (cx > .55 or no_sub) + .5 * (S < .12)
               + .5 * tags["travel"] - .9 * (cx < .4),
        "L04": .9 * lo + .7 * sym - .5 * hi - .5 * (cy > .6),
        "L05": 1.0 * hi + .5 * (cy > .6) - .3 * lo,
        "L06": 1.0 * (cy > .55) + .4 * no_sub,
        "L07": 1.1 * lo + .9 * tags["zh"] + .3 * (S < .12) - .7 * hi,
        "L10": 1.0 * sym + .5 * (S >= .12 and abs(cx - .5) < .15)
               + .2 * tags["zh"] - .5 * (abs(cx - .5) > .25),
    }


_VETO_PORTRAIT = {
    # P01/P10 文案单行纪律（约 16 汉字 + 2 标点）、P05 题签两列约 18 字：
    # 超长文案一律避开这三款，导向支持折行的版式（2026-09-13）
    "P01": lambda g: g["caption_w"] > CAPTION_SINGLE_LINE_MAX,
    "P10": lambda g: g["caption_w"] > CAPTION_SINGLE_LINE_MAX,
    # P05 题签占右侧 → 右侧有主体就否决；无城市/超长文案 → 否决
    "P05": lambda g: g["cx"] > .6 or not g["city_ok"]
           or g["caption_w"] > CAPTION_SINGLE_LINE_MAX,
}
_VETO_LANDSCAPE = {
    # L02 信息栏占左侧；L06 顶签占上部
    "L02": lambda g: g["cx"] < .4,
    "L06": lambda g: g["cy"] < .4,
    "L07": lambda g: not g["city_ok"],   # 落款钤印：无城市→无内容可镌
}


def pick_layout(item: Dict[str, Any], orientation: str,
                white_ratio: float) -> Tuple[str, Dict[str, Any]]:
    """
    选版式。返回 (layout_id, info)，info 携带信号 / 得分 / 否决明细，
    供 sidecar 与验证报告解释「为什么选它」。
    """
    g = extract_signals(item, white_ratio)
    if orientation == "landscape":
        scores, veto, base = _score_landscape(g), _VETO_LANDSCAPE, "L01"
    else:
        scores, veto, base = _score_portrait(g), _VETO_PORTRAIT, "P01"

    final = dict(scores)
    vetoed = {}
    for k, fn in veto.items():
        if fn(g):
            final[k] = float("-inf")
            vetoed[k] = "vetoed"

    top = max(final.values())
    if top == float("-inf") or top <= 0:
        layout = base
        tie = "fallback"
    else:
        best = sorted(k for k, v in final.items() if v == top)
        if len(best) == 1:
            layout, tie = best[0], "argmax"
        else:
            h = hashlib.md5(f"{item.get('path','')}|{item.get('date','')}"
                            .encode("utf-8")).hexdigest()
            layout, tie = best[int(h, 16) % len(best)], "hash-tiebreak"

    info = {"signals": g, "scores": scores, "final": final,
            "vetoed": vetoed, "tie": tie}
    return layout, info


def seal_glyphs_for_city(city: str) -> Optional[list]:
    """
    城市名 → 钤印字形序列（去行政后缀 + 简转已有篆形）。
    任一字符缺篆形则返回 None（调用方回退为字体文字印章）。
    "厦门市"→["5EC8","9580"]，"木渎镇"→["6728","7006"]。
    """
    name = str(city or "").strip()
    for suf in _SEAL_STRIP_SUFFIX:
        if name.endswith(suf) and len(name) > len(suf):
            name = name[:-len(suf)]
            break
    if not 1 <= len(name) <= 3:
        return None
    hexes = [_SEAL_GLYPH_MAP.get(ch) for ch in name]
    if any(h is None for h in hexes):
        return None
    return hexes
