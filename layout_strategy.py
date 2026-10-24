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
侧避让版式几何不可行（避让带必然压人/切人）时否决（2026-10-02）。
L10/P10 中央铭牌遮挡任一重要主体 ≥25% 或中心入牌时硬否决、贴边软惩罚
（2026-10-09，翻车照：人在铭牌带内选型不可见）。
人脸框零容忍（2026-10-24，翻车照：特写人脸占满画幅，铭牌只遮主体 bbox
12% 不触发 25% 硬线，实白铭牌压在嘴下巴上照样选上）：分析期由
yolov8n-pose 关键点派生人脸框（subjects_json 的 face 字段），选型期
覆盖区在脸框上的有效不透明度 ≥ COVERAGE_FACE_VETO_OPACITY 即硬否决。
老数据无 face 字段自动退回主体级防护，行为不变。
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Dict, List, Optional, Tuple

from style_registry import (crop_reserves as _crop_reserves_from_json,
                            get_style, landscape_ids, layout_names,
                            portrait_ids)

import config as cfg

# 版式清单/名称/裁切避让自 layouts/*.json 注册表读取（ADR 0002 数据化迁移，
# 2026-09-24）。打分与否决逻辑仍是代码——策略与数据的分界：
# 几何属性（避让区）随样式走，选型策略留 Python。
PORTRAIT_LAYOUTS = tuple(portrait_ids())
LANDSCAPE_LAYOUTS = tuple(landscape_ids())
LAYOUT_NAMES = layout_names()
CROP_RESERVES: Dict[str, Dict[str, int]] = _crop_reserves_from_json()

# 单行版式（P01/P10/P05）的文案显示宽度上限：约 16 汉字 + 2 标点。
# 全宽字符计 1、半宽计 0.5；超限时选型硬否决这三款（它们会对长文案省略号截断）。
CAPTION_SINGLE_LINE_MAX = 18.0

# 高低调阈值（predicted_white_ratio，与 choose_gamma 同一口径）
HIGH_KEY = 0.55
LOW_KEY = 0.35

# ---- 几何可行性否决（2026-10-02）----
# 带侧避让区（left/right）的版式，若不存在任何一个窗口平移位置能让
# **全部**重要主体既完整可见、又不碰避让带，则该版式物理上必然压人
# （或逼得裁切把人整个切掉），选型期直接否决。
# 2026-10-02 实例：4:3 横版照 draw_w==800 横向余量为 0，窗口位置唯一，
# L02 的 left=220 避让带正压在左侧孩子身上；且旧选型只看最大主体
# （largest_subject），第二人被压不触发否决。
# 判定用精确区间求交（非粗糙的"余量<reserve 一票否决"）：全宽照但主体
# 都在避让带之外时（如纯右置人像），L02 固定窗口照样完美可用，不误杀。
# 竖向避让区（top/bottom）不参与：纵向余量通常充足，且 compute_crop_window
# 已能通过滑动窗口主动上移/下移避让，选型期过度否决会误伤。
GEOMETRY_VETO_SIDES = ("left", "right")
_GEOMETRY_EPS_PX = 2.0  # 浮点/四舍五入容差


def _important_subject_boxes(subjects_json: str) -> List[Tuple[float, float, float, float]]:
    """归一化 (x1, y1, x2, y2) 重要主体框列表。

    口径与 largest_subject 一致（person/cat/dog、conf>=0.2），
    但保留**全部**重要主体——这是 2026-10-02 缺陷②的修复点：选型不能
    只看最大主体，第二人被侧栏压住同样是否决条件。
    """
    if not subjects_json:
        return []
    try:
        subs = json.loads(subjects_json)
    except Exception:
        return []
    weights = {"person": 5.0, "cat": 4.0, "dog": 4.0}
    boxes = []
    for s in subs if isinstance(subs, list) else []:
        try:
            label = s.get("label")
            conf = float(s.get("conf", 0))
            bbox = s.get("bbox")
        except Exception:
            continue
        if label not in weights or conf < 0.2 or not bbox or len(bbox) != 4:
            continue
        cx, cy, bw, bh = (float(v) for v in bbox)
        boxes.append((cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2))
    return boxes


def geometry_veto_reason(item: Dict[str, Any], layout: str,
                         img_w: Optional[int] = None,
                         img_h: Optional[int] = None) -> Optional[str]:
    """判断 layout 对这张照片是否存在「避让带必然压人/切人」的几何矛盾。

    返回否决原因字符串，无矛盾返回 None。img_w/img_h 传真实像素尺寸
    （exif_transpose 后）；缺省时退化用 DB 的 width/height（对 EXIF 旋转
    照片可能不准，仅兜底）。

    模型（与 compute_crop_window 的窗口平移同口径）：
    - cover 缩放后横向余量 slack = draw_w - canvas_w，窗口偏移 x ∈ [0, slack]；
    - 画布左侧/右侧 reserve 像素的避让带映射回图像坐标：
      left 带=[x, x+reserve]，right 带=[x+W-reserve, x+W]（W=canvas_w）；
    - 主体"完整可见且避开带"要求 x 同时满足 sx2 ≤ x+W（可见）、
      x ≤ sx1（可见）、sx2 ≤ x+reserve（在带左外）或 sx1 ≥ x+reserve（带右外），
      整理成窗口偏移区间：left → [sx2-W, sx1-reserve]，
      right → [sx2-(W-reserve), sx1]；
    - 全部主体区间 ∩ [0, slack] 非空 → 存在可行窗口，放行；否则否决。
    - 无侧避让区的版式（bottom/top 类）、无主体照片恒返回 None。
    """
    reserves = CROP_RESERVES.get(layout) or {}
    side = next((k for k in GEOMETRY_VETO_SIDES if reserves.get(k)), None)
    if side is None:
        return None
    reserve_px = int(reserves[side])
    if reserve_px <= 0:
        return None

    canvas_w = 800 if layout.startswith("L") else 480
    canvas_h = 480 if layout.startswith("L") else 800
    w = img_w or int(item.get("width") or 0)
    h = img_h or int(item.get("height") or 0)
    if not w or not h:
        return None
    # cover 缩放口径与 _cover_crop 一致：scale = max(canvas_w/w, canvas_h/h)
    draw_w = max(canvas_w, int(math.ceil(w * max(canvas_w / w, canvas_h / h))))
    slack_w = draw_w - canvas_w

    boxes = _important_subject_boxes(item.get("subjects_json", ""))
    if not boxes:
        return None  # 无主体：避让带不压任何东西，不否决（保住风景主用例）

    W = canvas_w
    lo, hi = 0.0, float(slack_w)
    for x1, _y1, x2, _y2 in boxes:
        sx1, sx2 = x1 * draw_w, x2 * draw_w
        if side == "left":
            i_lo, i_hi = sx2 - W, sx1 - reserve_px
        else:
            i_lo, i_hi = sx2 - (W - reserve_px), sx1
        lo, hi = max(lo, i_lo), min(hi, i_hi)
        if lo > hi + _GEOMETRY_EPS_PX:
            return (f"subject_x[{sx1:.0f},{sx2:.0f}]不可避开"
                    f"(带{side}={reserve_px},slack={slack_w})")
    return None


# ---- 版式覆盖区 × 主体冲突（2026-10-09 两轮迭代后的泛化版）----
# 第一轮只判 L10/P10 铭牌，结果当日主照被否决 L10 后改选 L01 底部玻璃卡，
# 毛玻璃（模糊+白底）照样把原图底缘的人脸磨掉——「遮挡」不是铭牌特例，
# 而是所有版式的覆盖区（glass 卡 / veil 渐变底托）与主体的空间冲突。
# 本节统一模型：从版式 JSON 提取全部覆盖区，按有效不透明度加权计算
# 每个重要主体被覆盖的比例，超线硬否决、贴线软惩罚。
#
# 有效不透明度口径：
# - glass：alpha/255，但 blur≥6 时取 max(alpha, GLASS_FLOOR)——模糊本身
#   就毁掉面部细节，低透白卡（如 L01 alpha=100）不等于低遮挡；
# - veil：纵向 smoothstep 渐变（与 render_layouts._scrim 同曲线），
#   逐行采样平均；a1 缺省跟随 TEXT_VEIL_OPACITY。
# 多区域复合：occlusion = 1 - Π(1 - contribution_i)。
_COVERAGE_SOFT_DISTANCE_PX_FALLBACK = 120


def _veil_alpha_default() -> int:
    return int(255 * float(getattr(cfg, "TEXT_VEIL_OPACITY", 0.85)))


def _smoothstep(t: float) -> float:
    return t * t * (3.0 - 2.0 * t)


def _coverage_regions(layout: str) -> List[Tuple[Tuple[int, int, int, int], float, str]]:
    """版式的全部遮挡区 [(rect, 有效不透明度 0-1, kind)]，自版式 JSON 提取。

    glass → max(alpha/255, GLASS_FLOOR)（blur 大时）；veil → 用平滑渐变
    的矩形级近似上限（a1_eff）；精确的逐行渐变在冲突计算时按行积分。
    """
    style = get_style(layout)
    if not style:
        return []
    floor = float(getattr(cfg, "COVERAGE_GLASS_FLOOR", 0.7))
    out: List[Tuple[Tuple[int, int, int, int], float, str]] = []
    for el in style.get("elements", []):
        kind = el.get("kind")
        if kind == "glass":
            x0, y0, x1, y1 = (int(v) for v in el["rect"])
            eff = float(el.get("alpha", 184)) / 255.0
            if int(el.get("blur", 12)) >= 6:
                eff = max(eff, floor)
            out.append(((x0, y0, x1, y1), min(1.0, eff), "glass"))
        elif kind == "veil":
            x0, y0, x1, y1 = (int(v) for v in el["rect"])
            a1 = el.get("a1")
            eff = float(a1 if a1 is not None else _veil_alpha_default()) / 255.0
            out.append(((x0, y0, x1, y1), min(1.0, eff), "veil"))
    return out


def _region_opacity_at(region: Tuple[Tuple[int, int, int, int], float, str],
                       y: float) -> float:
    """区域在画布行 y 处的有效不透明度（veil 按 smoothstep 渐变，glass 常数）。"""
    (x0, y0, x1, y1), eff, kind = region
    if y < y0 or y > y1:
        return 0.0
    if kind != "veil":
        return eff
    a0 = 0.0  # 现役版式 veil 均为 a0=0 起渐变
    p = max(1.0, float(y1 - y0))  # ramp_px 缺省=全高，与 _scrim 口径一致
    t = min(1.0, (y - y0) / p)
    return (a0 + (eff - a0) * _smoothstep(t))


def _region_opacity_over(region, iy0: float, iy1: float, samples: int = 9) -> float:
    """区域在竖向区间 [iy0, iy1] 的平均不透明度（veil 渐变按行采样）。"""
    if iy1 <= iy0:
        return _region_opacity_at(region, iy0)
    step = (iy1 - iy0) / (samples - 1)
    return sum(_region_opacity_at(region, iy0 + i * step)
               for i in range(samples)) / samples


def _canvas_mapping(item: Dict[str, Any], landscape: bool,
                    img_w: Optional[int], img_h: Optional[int]
                    ) -> Optional[Tuple[int, int, int, int]]:
    """cover 缩放 + 确定性居中窗口 → (draw_w, draw_h, left, top)。

    与 _cover_crop 口径一致（scale = max(canvas_w/w, canvas_h/h)）。
    无尺寸来源时返回 None（宁可不判也不误杀，与侧栏几何否决同口径）。
    """
    w = img_w or int(item.get("width") or 0)
    h = img_h or int(item.get("height") or 0)
    if not w or not h:
        return None
    cw, ch = (800, 480) if landscape else (480, 800)
    scale = max(cw / w, ch / h)
    draw_w = max(cw, int(math.ceil(w * scale)))
    draw_h = max(ch, int(math.ceil(h * scale)))
    left = max(0, (draw_w - cw) // 2)
    top = max(0, (draw_h - ch) // 2)
    return draw_w, draw_h, left, top


def _subject_canvas_boxes(item: Dict[str, Any], layout: str,
                          img_w: Optional[int], img_h: Optional[int]
                          ) -> Optional[List[Tuple[float, float, float, float, float]]]:
    """重要主体 bbox 映射到画布坐标 [(x1, y1, x2, y2, weight)]。

    映射口径与 _cover_crop 一致：scale = max(canvas_w/w, canvas_h/h)，
    裁切窗口取确定性居中近似（compute_crop_window 按主体优化窗口，选型期
    复算太贵；居中近似的误差 ≤ slack/2，由否决余量与软惩罚带吸收）。
    无尺寸来源时返回 None（宁可不判也不误杀，与侧栏几何否决同口径）。
    """
    if not _important_subject_boxes(item.get("subjects_json", "")):
        return []
    m = _canvas_mapping(item, layout.startswith("L"), img_w, img_h)
    if not m:
        return None
    draw_w, draw_h, left, top = m
    try:
        subs = json.loads(item.get("subjects_json", "") or "[]")
    except Exception:
        return []
    weights = {"person": 5.0, "cat": 4.0, "dog": 4.0}
    out: List[Tuple[float, float, float, float, float]] = []
    for s in subs if isinstance(subs, list) else []:
        try:
            label = s.get("label")
            if label not in weights or float(s.get("conf", 0)) < 0.2:
                continue
            cx, cy, bw, bh = (float(v) for v in s["bbox"])
        except Exception:
            continue
        out.append((cx * draw_w - left - bw * draw_w / 2,
                    cy * draw_h - top - bh * draw_h / 2,
                    cx * draw_w - left + bw * draw_w / 2,
                    cy * draw_h - top + bh * draw_h / 2,
                    weights[label]))
    return out


def _face_canvas_boxes(item: Dict[str, Any], layout: str,
                       img_w: Optional[int], img_h: Optional[int]
                       ) -> Optional[List[Tuple[float, float, float, float]]]:
    """person 脸框（subjects_json 的 face 字段，归一化 cx/cy/w/h）映射画布。

    返回 [(x1, y1, x2, y2)]；无 face 数据返回 []（老数据退回主体级防护），
    无尺寸来源返回 None（宁可不判）。映射口径与 _subject_canvas_boxes 一致。
    """
    try:
        subs = json.loads(item.get("subjects_json", "") or "[]")
    except Exception:
        return []
    m = _canvas_mapping(item, layout.startswith("L"), img_w, img_h)
    if not m:
        return None
    draw_w, draw_h, left, top = m
    out: List[Tuple[float, float, float, float]] = []
    for s in subs if isinstance(subs, list) else []:
        try:
            if s.get("label") != "person":
                continue
            face = s.get("face")
            if not face or len(face) != 4:
                continue
            cx, cy, bw, bh = (float(v) for v in face)
        except Exception:
            continue
        out.append((cx * draw_w - left - bw * draw_w / 2,
                    cy * draw_h - top - bh * draw_h / 2,
                    cx * draw_w - left + bw * draw_w / 2,
                    cy * draw_h - top + bh * draw_h / 2))
    return out


def coverage_conflict(item: Dict[str, Any], layout: str,
                      img_w: Optional[int] = None,
                      img_h: Optional[int] = None
                      ) -> Optional[Tuple[str, float, float]]:
    """判定 layout 的覆盖区（glass 卡 + veil 底托）是否遮挡重要主体。

    返回 (类型, 遮挡率, 最大区域不透明度, 来源)：
    - "hard"：任一重要主体被有效遮挡 ≥ COVERAGE_VETO_RATIO（复合遮挡率，
      按各区域不透明度加权），或主体中心落入不透明度 ≥0.5 的区域；
      或覆盖区在任一人脸框上的有效不透明度 ≥ COVERAGE_FACE_VETO_OPACITY
      （脸部零容忍，veil 渐变按脸框行区间积分，2026-10-24）。
      来源为 "face"（脸框命中）或 "subject"（主体级命中）；
    - "soft"：有接触/贴边但未达硬线——返回 (类型, 冲突度 0-1, opac, "subject")，
      冲突度 = max(遮挡率/硬线比, 贴近距离比)。
    无覆盖区/无主体/无尺寸返回 None。
    """
    regions = _coverage_regions(layout)
    if not regions:
        return None
    boxes = _subject_canvas_boxes(item, layout, img_w, img_h)
    if not boxes:
        return None
    ratio_lim = float(cfg.COVERAGE_VETO_RATIO)
    soft_d = float(getattr(cfg, "COVERAGE_SOFT_DISTANCE_PX",
                           _COVERAGE_SOFT_DISTANCE_PX_FALLBACK))
    worst: Optional[Tuple[str, float, float]] = None

    def _merge(kind: str, degree: float, opac: float) -> None:
        nonlocal worst
        if worst is not None and worst[0] == "hard" and kind != "hard":
            return
        if worst is None or kind == "hard" or degree > worst[1]:
            worst = (kind, min(1.0, max(0.0, degree)), opac, "subject")

    for x1, y1, x2, y2, _wt in boxes:
        area = max(1.0, (x2 - x1) * (y2 - y1))
        occluded = 1.0
        center_in_strong = False
        near_dist = None
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        for reg in regions:
            (px0, py0, px1, py1), eff, _kind = reg
            ix = max(0.0, min(x2, px1) - max(x1, px0))
            iy0, iy1 = max(y1, py0), min(y2, py1)
            if ix > 0 and iy1 > iy0:
                opac = _region_opacity_over(reg, iy0, iy1)
                contrib = opac * (ix * (iy1 - iy0)) / area
                occluded *= (1.0 - min(1.0, contrib))
            # 中心入强遮区判定（区域自身不透明度达标即可，与主体位置无关）
            if eff >= 0.5 and px0 <= cx <= px1 and py0 <= cy <= py1:
                center_in_strong = True
            # 贴边距离：只对"强遮区"（eff≥0.4）计，弱渐变边缘不构成挤压
            if eff >= 0.4:
                dx = max(px0 - cx, 0.0, cx - px1)
                dy = max(py0 - cy, 0.0, cy - py1)
                d = math.hypot(dx, dy)
                near_dist = d if near_dist is None else min(near_dist, d)
        occ = 1.0 - occluded
        if occ >= ratio_lim or center_in_strong:
            return ("hard", max(occ, ratio_lim), 1.0, "subject")
        degree = occ / ratio_lim
        if near_dist is not None and near_dist < soft_d:
            degree = max(degree, 1.0 - near_dist / soft_d)
        if degree > 0:
            _merge("soft", degree, occ)

    # 脸框零容忍（2026-10-24）：覆盖区在脸框竖向区间上的有效不透明度
    # 达标即硬否决。veil 渐变按行积分——脸只碰到浅端不拦，探进深端才拦；
    # glass 常数不透明度，碰脸即拦。不按主体 bbox 比例稀释（特写下会失真）。
    face_lim = float(getattr(cfg, "COVERAGE_FACE_VETO_OPACITY", 0.5))
    face_boxes = _face_canvas_boxes(item, layout, img_w, img_h)
    for fx1, fy1, fx2, fy2 in face_boxes or []:
        farea = max(1.0, (fx2 - fx1) * (fy2 - fy1))
        focc = 1.0
        for reg in regions:
            (px0, py0, px1, py1), eff, _kind = reg
            ix = max(0.0, min(fx2, px1) - max(fx1, px0))
            iy0, iy1 = max(fy1, py0), min(fy2, py1)
            if ix <= 0 or iy1 <= iy0:
                continue
            opac = _region_opacity_over(reg, iy0, iy1)
            focc *= 1.0 - min(1.0, opac * (ix * (iy1 - iy0)) / farea)
            if opac >= face_lim:
                return ("hard", max(1.0 - focc, face_lim), 1.0, "face")
    return worst


def coverage_soft_penalty(item: Dict[str, Any], layout: str,
                          img_w: Optional[int], img_h: Optional[int]) -> float:
    """覆盖区贴边软惩罚（0–COVERAGE_SOFT_MAX_PENALTY，随冲突度线性）。"""
    res = coverage_conflict(item, layout, img_w, img_h)
    if not res or res[0] == "hard":  # hard 走否决，不重复计惩罚
        return 0.0
    return float(getattr(cfg, "COVERAGE_SOFT_MAX_PENALTY", 0.8)) * res[1]



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
                white_ratio: float,
                img_size: Optional[Tuple[int, int]] = None) -> Tuple[str, Dict[str, Any]]:
    """
    选版式。返回 (layout_id, info)，info 携带信号 / 得分 / 否决明细，
    供 sidecar 与验证报告解释「为什么选它」。
    img_size: 照片真实像素尺寸（exif_transpose 后），供侧避让版式的
    几何可行性否决（2026-10-02）与 L10/P10 铭牌冲突判定（2026-10-09）；
    缺省时跳过几何否决（DB 尺寸对 EXIF 旋转照片不可信，宁可不判也不误杀）。
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
    # 几何可行性否决：带侧避让区的版式若物理上避不开重要主体（或横向
    # 余量不足使避让带失效），直接出局。geometry_veto_reason 内部自过滤
    # 非侧栏版式与无尺寸场景。
    # 铭牌/覆盖区冲突否决（2026-10-09）：任一版式覆盖区（glass 卡/veil 底托）
    # 遮挡任一重要主体 ≥ COVERAGE_VETO_RATIO 或中心入强遮区 → 硬否决；
    # 贴边/轻度接触 → 软惩罚线性扣分。判定口径见 coverage_conflict。
    img_w = img_size[0] if img_size else None
    img_h = img_size[1] if img_size else None
    penalties = {}
    degrees = {}  # 覆盖区冲突度（clean=0.0；rule/geometry 否决的不入册）
    for k in scores:
        if k in vetoed:
            continue
        reason = geometry_veto_reason(item, k, img_w, img_h)
        if reason:
            final[k] = float("-inf")
            vetoed[k] = f"geometry: {reason}"
            continue
        conf = coverage_conflict(item, k, img_w, img_h)
        if conf and conf[0] == "hard":
            final[k] = float("-inf")
            degrees[k] = conf[1]  # hard: degree=遮挡率(≥硬线)
            if conf[3] == "face":
                vetoed[k] = ("coverage: 覆盖区压到人脸框（有效不透明度 ≥ "
                             f"{float(getattr(cfg, 'COVERAGE_FACE_VETO_OPACITY', 0.5)):.0%}）")
            else:
                vetoed[k] = (f"coverage: 覆盖区遮挡主体 {conf[1]:.0%} ≥ "
                             f"{cfg.COVERAGE_VETO_RATIO:.0%} 或中心入强遮区")
        elif conf and conf[0] == "soft":
            penalties[k] = coverage_soft_penalty(item, k, img_w, img_h)
            degrees[k] = conf[2]  # soft: 用实际遮挡率（与 hard 同量纲可比）
        else:
            degrees[k] = 0.0
    for k, p in penalties.items():
        final[k] = final.get(k, scores.get(k, 0.0)) - p

    top = max(final.values())
    if top == float("-inf") or top <= 0:
        # 回退纪律（2026-10-09 二轮教训）：基线本身可能正压着主体
        # （当日主照 L10 被否后回退 L01，毛玻璃照样磨脸）。仅当基线
        # 无覆盖冲突时维持历史 fallback；否则在覆盖冲突候选里选
        # 「伤害最小」（冲突度升序，并列哈希，确定性）。
        if final.get(base, float("-inf")) != float("-inf") and \
                degrees.get(base, 1.0) == 0.0:
            layout, tie = base, "fallback"
        else:
            pool = {k: v for k, v in degrees.items() if v is not None}
            if pool:
                best_deg = min(pool.values())
                cand = sorted(k for k, v in pool.items() if v == best_deg)
                if len(cand) == 1:
                    layout, tie = cand[0], "least-harm"
                else:
                    h = hashlib.md5(f"{item.get('path', '')}|{item.get('date', '')}"
                                    .encode("utf-8")).hexdigest()
                    layout, tie = cand[int(h, 16) % len(cand)], "least-harm"
            else:
                layout, tie = base, "fallback"
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
