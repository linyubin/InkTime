#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
每日相册渲染脚本：
- 从 photos.db / photo_scores 中选出一张“历史上的今天”照片
- 按 InkTime 模拟器的布局渲染到 480x800
- 用 LXGWHeartSerifMN.ttf 把文案 / 日期 / 地点都画到图上
- 转成四色墨水屏（黑/白/红/黄）图像，并保存为 BIN（1 字节 1 像素，行优先）
- 同时导出 latest.h 头文件数组，给 ESP32 直接 include
"""

from __future__ import annotations

from pathlib import Path
import sqlite3
import json
import re
import datetime as dt
import os
from typing import List, Dict, Any, Tuple, Optional
from PIL import Image, ImageDraw, ImageFont, ImageOps
import config as cfg


TODAY = dt.date.today()

# === 路径配置（来自 config.py） ===
ROOT_DIR = Path(__file__).resolve().parent

DB_PATH = Path(str(getattr(cfg, "DB_PATH", "photos.db") or "photos.db")).expanduser()
if not DB_PATH.is_absolute():
    DB_PATH = (ROOT_DIR / DB_PATH).resolve()

BIN_OUTPUT_DIR = Path(str(getattr(cfg, "BIN_OUTPUT_DIR", "output/inktime") or "output/inktime")).expanduser()
if not BIN_OUTPUT_DIR.is_absolute():
    BIN_OUTPUT_DIR = (ROOT_DIR / BIN_OUTPUT_DIR).resolve()
BIN_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

FONT_PATH = Path(str(getattr(cfg, "FONT_PATH", "") or "")).expanduser()
if str(FONT_PATH) and not FONT_PATH.is_absolute():
    FONT_PATH = (ROOT_DIR / FONT_PATH).resolve()

MEMORY_THRESHOLD = float(getattr(cfg, "MEMORY_THRESHOLD", 70.0) or 70.0)
DAILY_PHOTO_QUANTITY = int(getattr(cfg, "DAILY_PHOTO_QUANTITY", 5) or 5)

# 加权选片配置（均设默认值以向后兼容）
PATH_WEIGHTS         = getattr(cfg, "PATH_WEIGHTS",         {})
CATEGORY_WEIGHTS     = getattr(cfg, "CATEGORY_WEIGHTS",     {})
PORTRAIT_BOOST       = getattr(cfg, "PORTRAIT_BOOST",       {})
SCORE_MEMORY_WEIGHT  = float(getattr(cfg, "SCORE_MEMORY_WEIGHT",  0.7))
HIGH_SCORE_THRESHOLD = float(getattr(cfg, "HIGH_SCORE_THRESHOLD", 87.0))
RESHOW_AFTER_DAYS    = int(getattr(cfg, "RESHOW_AFTER_DAYS",   180))
RECENCY_PENALTY      = float(getattr(cfg, "RECENCY_PENALTY",    0.5))
PATH_MAP             = getattr(cfg, "PATH_MAP",             {})

# 墨水屏尺寸
CANVAS_WIDTH = 480
CANVAS_HEIGHT = 800

# 底部文字区域高度
TEXT_AREA_HEIGHT = 120


# ========== DB 与 EXIF 处理 ==========

def extract_date_from_exif(exif_json: Optional[str], filepath: str = "") -> str:
    """
    从 EXIF JSON 中提取拍摄日期，返回 YYYY-MM-DD 格式，失败则返回空字符串。
    逻辑与 review_web.py 中保持一致。
    """
    date_str = ""
    if exif_json:
        try:
            data = json.loads(exif_json)
            dt_str = data.get("datetime")
            if dt_str:
                date_part = str(dt_str).split()[0]
                parts = date_part.replace(":", "-").split("-")
                if len(parts) >= 3:
                    date_str = f"{parts[0]}-{parts[1]}-{parts[2]}"
        except Exception:
            pass
            
    if date_str and len(date_str) == 10:
        return date_str
        
    if filepath:
        clean_path = filepath.replace('\\', '/')
        # 1. YYYY-MM-DD 或 YYYY_MM_DD 或 YYYY.MM.DD
        m1 = re.search(r'(20\d{2}|19\d{2})[-_ \.](0[1-9]|1[0-2])[-_ \.](0[1-9]|[12]\d|3[01])', clean_path)
        if m1:
            return f"{m1.group(1)}-{m1.group(2)}-{m1.group(3)}"
        # 2. YYYYMMDD (如 20231225)
        m2 = re.search(r'(?:^|[^0-9])((?:20|19)\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?:[^0-9]|$)', clean_path)
        if m2:
            return f"{m2.group(1)}-{m2.group(2)}-{m2.group(3)}"
        # 3. YYYYMM (如 201607)
        m3 = re.search(r'(?:^|[^0-9])((?:20|19)\d{2})(0[1-9]|1[0-2])(?:[^0-9]|$)', clean_path)
        if m3:
            return f"{m3.group(1)}-{m3.group(2)}-01"
            
    return ""


def load_sim_rows() -> List[Dict[str, Any]]:
    """
    加载 InkTime 用的核心字段：
    - path: 照片路径
    - exif_json: 用于解析日期 / GPS
    - side_caption: 文案
    - memory_score: 回忆度
    - exif_gps_lat / exif_gps_lon / exif_city: 地点信息（纯本地，不上网）
    """
    if not DB_PATH.exists():
        raise SystemExit(f"找不到数据库文件: {DB_PATH}")

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    rows = c.execute(
        """
        SELECT path,
               exif_json,
               side_caption,
               memory_score,
               exif_gps_lat,
               exif_gps_lon,
               exif_city,
               beauty_score,
               type,
               used_at,
               subjects_json
        FROM photo_scores
        WHERE exif_json IS NOT NULL
        """
    ).fetchall()
    conn.close()

    items: List[Dict[str, Any]] = []
    for path, exif_json, side_caption, memory_score, gps_lat, gps_lon, exif_city, beauty_score, type_str, used_at, subjects_json in rows:
        date_str = extract_date_from_exif(exif_json, str(path))
        if not date_str:
            continue
        # 再次兜底过滤 Screenshot 等
        if "screenshot" in str(path).lower():
            continue

        try:
            y, m, d = map(int, date_str.split("-"))
        except Exception:
            continue
        md = f"{m:02d}-{d:02d}"

        item = {
            "path": str(path),
            "date": date_str,  # YYYY-MM-DD
            "md": md,          # MM-DD
            "side": side_caption or "",
            "memory": float(memory_score) if memory_score is not None else -1.0,
            "lat": gps_lat,
            "lon": gps_lon,
            "city": exif_city or "",
            "beauty":   float(beauty_score) if beauty_score is not None else None,
            "type_raw": type_str or "",
            "used_at":  used_at or None,
            "subjects_json": subjects_json or "",
        }
        items.append(item)

    return items


# ========== 加权选片辅助函数 ==========

def normalize_path(path: str) -> str:
    """将 DB Windows 路径通过 PATH_MAP 转换为本地路径（仅用于权重匹配，不校验文件是否存在）。"""
    for old_prefix, new_prefix in PATH_MAP.items():
        if path.startswith(old_prefix):
            return path.replace(old_prefix, new_prefix).replace("\\", "/")
    return path


def compute_path_weight(path: str) -> float:
    """
    在 PATH_WEIGHTS 中查找最长匹配前缀，返回对应权重（默认 1.0）。
    同时尝试原始 DB 路径（Windows 格式）和 PATH_MAP 规范化后路径（Linux 格式），
    取最长匹配的权重——支持用户用任意格式配置 key。

    相对路径支持：只有 \\\\ 开头的 UNC 网络路径视为绝对路径，其余 key
    （包括 Unix 风格的 /subpath/... 写法）均视为相对 IMAGE_DIR 的子路径，
    会自动拼上 IMAGE_DIR 并统一斜杠后参与匹配。例如：
        PATH_WEIGHTS = {"/Timeline/Timeline@布丁": 1.3}
    在 Windows 上等价于 IMAGE_DIR + "\\Timeline\\Timeline@布丁"，
    在树莓派上经 PATH_MAP 规范化后同样可以命中。
    """
    if not PATH_WEIGHTS:
        return 1.0
    normalized = normalize_path(path)
    image_dir = str(getattr(cfg, "IMAGE_DIR", "")).rstrip("\\/")
    best_weight, best_len = 1.0, -1
    for prefix, weight in PATH_WEIGHTS.items():
        prefix_str = str(prefix)
        # 只有 \\\\ 开头的 UNC 路径视为绝对路径，其余均视为相对 IMAGE_DIR
        if image_dir and not prefix_str.startswith("\\\\"):
            # 统一斜杠风格，与 image_dir（通常为 Windows UNC）对齐
            rel = prefix_str.lstrip("\\/").replace("/", "\\")
            prefix_str = image_dir + "\\" + rel
        norm_prefix = normalize_path(prefix_str)
        for p in (path.replace("\\", "/"), normalized.replace("\\", "/")):
            for pfx in (prefix_str.replace("\\", "/"), norm_prefix.replace("\\", "/")):
                if p.startswith(pfx) and len(pfx) > best_len:
                    best_len, best_weight = len(pfx), float(weight)
    return best_weight


def compute_category_weight(type_raw: str) -> float:
    """
    解析 type 字段（JSON 数组字符串），对每个分类查 CATEGORY_WEIGHTS，
    未命中取 1.0，多分类取算术平均值。
    """
    if not type_raw or not CATEGORY_WEIGHTS:
        return 1.0
    try:
        cats = json.loads(type_raw)
        if not isinstance(cats, list) or not cats:
            return 1.0
        weights = [CATEGORY_WEIGHTS.get(c, 1.0) for c in cats]
        return sum(weights) / len(weights)
    except Exception:
        return 1.0


def compute_portrait_boost(type_raw: str) -> float:
    """
    从 PORTRAIT_BOOST 中查找照片分类的最大加成系数（取最大值策略）。
    多分类命中时取最大值，避免多标签稀释加成；无命中则返回 1.0。
    """
    if not type_raw or not PORTRAIT_BOOST:
        return 1.0
    try:
        cats = json.loads(type_raw)
        if not isinstance(cats, list) or not cats:
            return 1.0
        return max((PORTRAIT_BOOST.get(c, 1.0) for c in cats), default=1.0)
    except Exception:
        return 1.0


def apply_portrait_rerank(photos: list) -> list:
    """
    对已选定的照片列表按肖像加成进行二次重排序。
    _display_score = _final_score × portrait_boost
    不修改 _final_score（保持选片逻辑独立），仅影响最终展示顺序。
    """
    if not PORTRAIT_BOOST:
        return photos
    for p in photos:
        fs = p.get("_final_score") or compute_final_score(p)
        pb = compute_portrait_boost(p.get("type_raw", ""))
        p["_display_score"] = fs * pb
        p["_portrait_boost"] = pb
    photos.sort(key=lambda x: x["_display_score"], reverse=True)
    return photos


def compute_recency_penalty(used_at) -> float:
    """近期展示过的照片降权，返回乘数（0~1）。超过 RESHOW_AFTER_DAYS 天或未展示则返回 1.0。"""
    if not used_at:
        return 1.0
    try:
        days_ago = (TODAY - dt.date.fromisoformat(str(used_at))).days
        if days_ago < RESHOW_AFTER_DAYS:
            return RECENCY_PENALTY
    except Exception:
        pass
    return 1.0


def compute_final_score(item: dict) -> float:
    """
    计算照片综合加权得分：
        final = (memory × path_w × cat_w × recency_w) × α + beauty × (1−α)
    若 beauty 为 None，退化为 final = weighted_memory。
    """
    memory = item.get("memory", -1.0)
    if memory < 0:
        return 0.0
    path_w    = compute_path_weight(item["path"])
    cat_w     = compute_category_weight(item.get("type_raw", ""))
    recency_w = compute_recency_penalty(item.get("used_at"))
    wm = memory * path_w * cat_w * recency_w
    beauty = item.get("beauty")
    if beauty is None:
        return wm
    return wm * SCORE_MEMORY_WEIGHT + beauty * (1.0 - SCORE_MEMORY_WEIGHT)


def mark_photo_used(path: str) -> None:
    """渲染成功后，将照片的 used_at 更新为今天（防重复展示）。"""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "UPDATE photo_scores SET used_at = ? WHERE path = ?",
            (TODAY.isoformat(), path),
        )
        conn.commit()
        conn.close()
        print(f"[INFO] 已记录展示历史: {Path(path).name}")
    except Exception as e:
        print(f"[WARN] 写入 used_at 失败: {e}")


# ========== “历史上的今天”选片 ==========

def md_to_day_of_year(md: str) -> Optional[int]:
    """把 'MM-DD' 转成非闰年的第几天（1~365）。"""
    try:
        m, d = map(int, md.split("-"))
        days_before = [0, 0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
        if m < 1 or m > 12:
            return None
        return days_before[m] + d
    except Exception:
        return None


def day_of_year_to_md(day: int) -> str:
    # 选一个非闰年（2001/2005 随便），只依赖 day-of-year。
    base = dt.date(2001, 1, 1) + dt.timedelta(days=day - 1)
    return f"{base.month:02d}-{base.day:02d}"


def choose_photo_for_today(items: List[Dict[str, Any]], today: dt.date) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    选片规则（按月日）：
    - 以 today 的月日为目标，例如 12 月 2 日 -> "12-02"
    - 在所有年份该月日的照片中，找 memory > MEMORY_THRESHOLD 的候选，随机选一张
    - 如果该月日没有任何 > 阈值的，则往前一天（月日）继续找（12-01, 11-30, ...），最多回溯 365 天
    - 如果整个 365 天都没有任何 > 阈值的照片，则在全局中选 memory 最大的一张作为兜底
    """

    if not items:
        raise RuntimeError("没有任何可用照片")

    # 按 md 分组
    by_md: Dict[str, List[Dict[str, Any]]] = {}
    for it in items:
        md = it["md"]
        by_md.setdefault(md, []).append(it)

    # 每组内按 memory 从高到低排序
    for arr in by_md.values():
        arr.sort(key=lambda x: x.get("memory", -1.0), reverse=True)

    target_md = f"{today.month:02d}-{today.day:02d}"
    target_doy = md_to_day_of_year(target_md)
    if target_doy is None:
        raise RuntimeError(f"无法解析今天的月日: {target_md}")

    import random

    for offset in range(0, 365):
        doy = target_doy - offset
        if doy <= 0:
            doy += 365
        md = day_of_year_to_md(doy)

        arr = by_md.get(md, [])
        if not arr:
            continue
        candidates = [p for p in arr if p.get("memory", -1.0) > MEMORY_THRESHOLD]
        if not candidates:
            continue

        # 计算 final_score 并排序
        for p in candidates:
            p["_final_score"] = compute_final_score(p)
        candidates.sort(key=lambda x: x["_final_score"], reverse=True)

        # 精英随机（>= 阈值取 Top-3 随机）or 确定性最优
        elite = [p for p in candidates if p["_final_score"] >= HIGH_SCORE_THRESHOLD]
        if elite:
            chosen = random.choice(elite[:3])
            selection_mode = "elite_top3_random"
        else:
            chosen = candidates[0]
            selection_mode = "deterministic_top1"

        info = {
            "target_md": target_md,
            "used_md": md,
            "day_offset": -offset,
            "candidate_count": len(candidates),
            "elite_count": len(elite),
            "total_count_md": len(arr),
            "threshold": MEMORY_THRESHOLD,
            "high_score_threshold": HIGH_SCORE_THRESHOLD,
            "selection_mode": selection_mode,
            "final_score": chosen["_final_score"],
            "fallback_global_max": False,
        }
        return chosen, info

    # 兜底：全局 final_score 最高的照片
    for p in items:
        p["_final_score"] = compute_final_score(p)
    global_best = max(items, key=lambda x: x["_final_score"])
    info = {
        "target_md": target_md,
        "used_md": global_best["md"],
        "day_offset": None,
        "candidate_count": 1,
        "elite_count": 0,
        "total_count_md": len(by_md.get(global_best["md"], [])),
        "threshold": MEMORY_THRESHOLD,
        "high_score_threshold": HIGH_SCORE_THRESHOLD,
        "selection_mode": "fallback_global",
        "final_score": global_best["_final_score"],
        "fallback_global_max": True,

    }
    return global_best, info

def choose_photos_for_today(items: List[Dict[str, Any]], today: dt.date, count: int = 5) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    选片规则（多张版，按月日）：
    - 以 today 的月日为目标，例如 12 月 2 日 -> "12-02"
    - 在所有年份该月日的照片中，找 memory > MEMORY_THRESHOLD 的候选，尽量随机选 count 张
    - 如果该月日没有任何 > 阈值的，则往前一天（月日）继续找（12-01, 11-30, ...），最多回溯 365 天
    - 如果整个 365 天都没有任何 > 阈值的照片，则在全局中选回忆度最高的若干张作为兜底
    """
    if not items:
        raise RuntimeError("没有任何可用照片")

    # 按 md 分组
    by_md: Dict[str, List[Dict[str, Any]]] = {}
    for it in items:
        md = it["md"]
        by_md.setdefault(md, []).append(it)

    # 每组内按 memory 从高到低排序
    for arr in by_md.values():
        arr.sort(key=lambda x: x.get("memory", -1.0), reverse=True)

    target_md = f"{today.month:02d}-{today.day:02d}"
    target_doy = md_to_day_of_year(target_md)
    if target_doy is None:
        raise RuntimeError(f"无法解析今天的月日: {target_md}")

    import random

    for offset in range(0, 365):
        doy = target_doy - offset
        if doy <= 0:
            doy += 365
        md = day_of_year_to_md(doy)

        arr = by_md.get(md, [])
        if not arr:
            continue
        candidates = [p for p in arr if p.get("memory", -1.0) > MEMORY_THRESHOLD]
        if not candidates:
            continue

        # 计算 final_score 并排序
        for p in candidates:
            p["_final_score"] = compute_final_score(p)
        candidates.sort(key=lambda x: x["_final_score"], reverse=True)

        # 精英分级选片：
        # 第一步：从 final_score >= HIGH_SCORE_THRESHOLD 的精英中随机抽取
        elite = [p for p in candidates if p["_final_score"] >= HIGH_SCORE_THRESHOLD]
        if len(elite) >= count:
            # 精英超过上限，随机选 count 张
            chosen_list = random.sample(elite, count)
            selection_mode = "elite_random"
        elif elite:
            # 有精英但未达上限，只渲染精英，不补齐
            chosen_list = list(elite)
            selection_mode = "elite_only"
        else:
            # 无精英，从 Top-(count*2) 的池中随机抽取（保底多样性）
            pool_size = min(len(candidates), max(count * 2, count + 3))
            pool = candidates[:pool_size]
            if len(pool) >= count:
                chosen_list = random.sample(pool, count)
            else:
                chosen_list = list(pool)
                # 候选不足 count 张，按 final_score 从当日剩余照片补齐
                chosen_set = {id(p) for p in chosen_list}
                for extra in sorted(arr, key=lambda x: compute_final_score(x), reverse=True):
                    if id(extra) in chosen_set:
                        continue
                    chosen_list.append(extra)
                    chosen_set.add(id(extra))
                    if len(chosen_list) >= count:
                        break
            selection_mode = "topn_pool_random"

        info = {
            "target_md": target_md,
            "used_md": md,
            "day_offset": -offset,
            "candidate_count": len(candidates),
            "elite_count": len(elite),
            "total_count_md": len(arr),
            "threshold": MEMORY_THRESHOLD,
            "high_score_threshold": HIGH_SCORE_THRESHOLD,
            "selection_mode": selection_mode,
            "fallback_global_max": False,
        }
        return chosen_list, info

    # 兜底：全局 final_score 最高的若干张
    for p in items:
        if "_final_score" not in p:
            p["_final_score"] = compute_final_score(p)
    sorted_all = sorted(items, key=lambda x: x["_final_score"], reverse=True)
    chosen_list = sorted_all[:count]
    info = {
        "target_md": target_md,
        "used_md": chosen_list[0]["md"] if chosen_list else "",
        "day_offset": None,
        "candidate_count": len(chosen_list),
        "total_count_md": len(items),
        "threshold": MEMORY_THRESHOLD,
        "fallback_global_max": True,
    }
    return chosen_list, info
# ========== 绘制 + 抖动 ==========

# 四色墨水屏调色板（RGB）
PALETTE = [
    (0, 0, 0),         # 0 = 黑
    (255, 255, 255),   # 1 = 白
    (200, 0, 0),       # 2 = 红
    (220, 180, 0),     # 3 = 黄
]


def nearest_palette_color(r: float, g: float, b: float) -> Tuple[int, int, int, int]:
    """
    返回 (idx, pr, pg, pb)，idx 为 PALETTE 中最近颜色的索引。
    """
    best_idx = 0
    best_dist = float("inf")
    for i, (pr, pg, pb) in enumerate(PALETTE):
        dr = r - pr
        dg = g - pg
        db = b - pb
        dist = dr * dr + dg * dg + db * db
        if dist < best_dist:
            best_dist = dist
            best_idx = i
    pr, pg, pb = PALETTE[best_idx]
    return best_idx, pr, pg, pb


def wrap_text_chinese(draw: ImageDraw.ImageDraw,
                      text: str,
                      font: ImageFont.FreeTypeFont,
                      max_width: int,
                      max_lines: int) -> List[str]:
    """
    简单中文按字符宽度折行。支持处理自带换行符的文本。
    """
    if not text:
        return []
    lines: List[str] = []
    
    # 先按自带的换行符分割段落
    for paragraph in text.replace("\r\n", "\n").split("\n"):
        if len(lines) >= max_lines:
            break
            
        if not paragraph:
            continue
            
        line = ""
        for ch in paragraph:
            test = line + ch
            w = draw.textlength(test, font=font)
            if w <= max_width:
                line = test
            else:
                if line:
                    lines.append(line)
                line = ch
                if len(lines) >= max_lines:
                    break
        if line and len(lines) < max_lines:
            lines.append(line)
    return lines



def format_date_display(date_str: str) -> str:
    """
    "YYYY-MM-DD" -> "YYYY.M.D"
    """
    if not date_str:
        return ""
    parts = date_str.split("-")
    if len(parts) < 3:
        return date_str
    y = parts[0]
    try:
        m = str(int(parts[1]))
        d = str(int(parts[2]))
    except Exception:
        return date_str
    return f"{y}.{m}.{d}"


def format_location(lat, lon, city: str) -> str:
    """
    地点字符串：
    - 有 city 用 city
    - 否则如果有 lat/lon，用 "lat, lon"（5 位小数）
    - 否则空字符串（不写“未知地点”）
    """
    if city and str(city).strip():
        return str(city).strip()
    if lat is None or lon is None:
        return ""
    try:
        return f"{float(lat):.5f}, {float(lon):.5f}"
    except Exception:
        return ""


def compute_crop_window(draw_w: int, draw_h: int, img_area_w: int, img_area_h: int, subjects_json: str) -> tuple[int, int]:
    default_left = max(0, (draw_w - img_area_w) // 2)
    default_top  = max(0, (draw_h - img_area_h) // 2)

    if not subjects_json:
        return default_left, default_top

    try:
        import json
        subjects = json.loads(subjects_json)
        if not subjects:
            return default_left, default_top
            
        weights_map = getattr(cfg, "YOLO_SUBJECT_WEIGHTS", {"person": 5.0, "cat": 4.0, "dog": 4.0})
        
        # 使用“滑动窗口（Sliding Window）”覆盖率最大化算法
        # 彻底解决“强行居中导致左右主体被从中间切成两半”或“主体在边缘被切掉”的问题。
        valid_subs = []
        for s in subjects:
            label = s.get("label", "")
            conf = s.get("conf", 0.0)
            if label not in weights_map or conf < 0.2:
                continue
                
            bbox = s.get("bbox")
            if not bbox or len(bbox) != 4:
                continue
                
            cx, cy, bw, bh = bbox
            # 计算主体在放大后的像素坐标系中的边界框
            sx1 = (cx - bw/2) * draw_w
            sx2 = (cx + bw/2) * draw_w
            sy1 = (cy - bh/2) * draw_h
            sy2 = (cy + bh/2) * draw_h
            area = (sx2 - sx1) * (sy2 - sy1)
            
            base_w = weights_map[label]
            weight = conf * base_w * (area ** 0.5)
            
            valid_subs.append({
                "x1": sx1, "x2": sx2, "y1": sy1, "y2": sy2,
                "area": area,
                "weight": weight
            })
            
        if not valid_subs:
            return default_left, default_top

        x_steps = []
        if draw_w > img_area_w:
            step_x = max(5, (draw_w - img_area_w) // 50)
            x_steps = list(range(0, draw_w - img_area_w, step_x))
            x_steps.append(draw_w - img_area_w)
        else:
            x_steps = [0]
            
        y_steps = []
        if draw_h > img_area_h:
            step_y = max(5, (draw_h - img_area_h) // 50)
            y_steps = list(range(0, draw_h - img_area_h, step_y))
            y_steps.append(draw_h - img_area_h)
        else:
            y_steps = [0]
            
        center_x = max(0, draw_w - img_area_w) / 2.0
        center_y = max(0, draw_h - img_area_h) / 2.0
        
        best_score = -1.0
        best_left = default_left
        best_top = default_top
        
        for x in x_steps:
            for y in y_steps:
                wx1, wx2 = x, x + img_area_w
                wy1, wy2 = y, y + img_area_h
                
                score = 0.0
                for s in valid_subs:
                    ix1 = max(wx1, s["x1"])
                    ix2 = min(wx2, s["x2"])
                    iy1 = max(wy1, s["y1"])
                    iy2 = min(wy2, s["y2"])
                    
                    if ix1 < ix2 and iy1 < iy2:
                        iarea = (ix2 - ix1) * (iy2 - iy1)
                        cov = iarea / s["area"] if s["area"] > 0 else 0
                        # 核心逻辑：平方惩罚 (cov ** 2)
                        # 如果切掉了一半(cov=0.5)，得分会变成 0.25 倍，从而遭到极大的排斥！
                        # 算法会宁可放弃一个小目标，也优先保证将大目标100%完整框入，绝不切头。
                        score += s["weight"] * (cov ** 2)
                
                # 中心偏置：得分相同时，优先选择最靠近中心的构图
                dist_x = abs(x - center_x) / (center_x + 1)
                dist_y = abs(y - center_y) / (center_y + 1)
                bias = 0.001 * (2 - dist_x - dist_y)
                score += bias
                
                if score > best_score:
                    best_score = score
                    best_left = x
                    best_top = y
                    
        return int(best_left), int(best_top)
    except Exception as e:
        print(f"[WARN] compute_crop_window error: {e}")
        return default_left, default_top

def render_image(item: Dict[str, Any]) -> Image.Image:
    """
    根据选中的 item 渲染一张 480x800 的 RGB 图像（竖屏）：
    - 上方图片：占 [0, CANVAS_HEIGHT - TEXT_AREA_HEIGHT)
    - 底部 TEXT_AREA_HEIGHT 像素为文字区：第一行 side 文案（最多两行），第二行日期 + 地点
    """
    canvas = Image.new("RGB", (CANVAS_WIDTH, CANVAS_HEIGHT), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)

    # ---------- 加载原图并按 EXIF 方向纠正 ----------
    raw_path = str(item["path"])
    img_path = Path(raw_path)

    # 跨平台路径映射支持 (Windows -> Linux 迁移自动修复)
    if not img_path.exists():
        path_map = getattr(cfg, "PATH_MAP", {})
        for old_prefix, new_prefix in path_map.items():
            if raw_path.startswith(old_prefix):
                test_path = Path(raw_path.replace(old_prefix, new_prefix).replace("\\", "/"))
                if test_path.exists():
                    img_path = test_path
                    break

    # 自动推断相对路径兜底 (通过提取 IMAGE_DIR 共同目录名称作后缀重连)
    if not img_path.exists() and hasattr(cfg, "IMAGE_DIR"):
        base_name = Path(cfg.IMAGE_DIR).name
        norm_raw = raw_path.replace("\\", "/")
        pattern = f"/{base_name}/"
        if pattern in norm_raw:
            suffix = norm_raw.split(pattern, 1)[1]
            guessed_path = Path(cfg.IMAGE_DIR) / ".." / base_name / suffix
            guessed_path = guessed_path.resolve()
            if guessed_path.exists():
                img_path = guessed_path

    if not img_path.exists():
        # 添加详细的 Debug 日志帮助排查
        debug_msg = f"图片不存在: {raw_path}\n"
        debug_msg += f"[DEBUG] 你的 cfg.IMAGE_DIR 是: {getattr(cfg, 'IMAGE_DIR', '未定义')}\n"
        if hasattr(cfg, 'IMAGE_DIR'):
            base_name = Path(cfg.IMAGE_DIR).name
            norm_raw = raw_path.replace("\\", "/")
            pattern = f"/{base_name}/"
            debug_msg += f"[DEBUG] base_name={base_name}, 正在尝试寻找匹配格式: {pattern}\n"
            if pattern in norm_raw:
                suffix = norm_raw.split(pattern, 1)[1]
                guessed_path = Path(cfg.IMAGE_DIR) / ".." / base_name / suffix
                guessed_path_abs = guessed_path.resolve()
                debug_msg += f"[DEBUG] 分割后半部 suffix: {suffix}\n"
                debug_msg += f"[DEBUG] 尝试拼凑绝对路径: {guessed_path_abs}\n"
                debug_msg += f"[DEBUG] 该拼凑的路径存在吗？{guessed_path_abs.exists()}\n"
            else:
                debug_msg += f"[DEBUG] {pattern} 不存在于 {norm_raw} 中。推断失效。\n"
        
        debug_msg += f"[DEBUG] 或者，你可以尝试在树莓派的 config.py 里加上 PATH_MAP = {{r'\\\\10.168.1.111\\Photos': '你实际的Linux目录'}}\n"
        raise RuntimeError(debug_msg)
    img = Image.open(img_path)
    img = ImageOps.exif_transpose(img).convert("RGB")

    img_w, img_h = img.size
    if img_w == 0 or img_h == 0:
        raise RuntimeError(f"图片尺寸非法: {img.size}")

    # ---------- 照片区域 ----------
    img_area_w = CANVAS_WIDTH
    img_area_h = CANVAS_HEIGHT  # 取消专属文字留白，让照片和背景彻底铺满全屏

    # “铺满裁剪”：缩放到至少覆盖区域，再从中间裁一块
    ratio_w = img_area_w / img_w
    ratio_h = img_area_h / img_h
    scale = max(ratio_w, ratio_h)
    
    # ====== 内容感知缩放（防止多个主体相隔太远导致必然有一人被裁掉） ======
    subjects_json_str = item.get("subjects_json", "")
    if subjects_json_str:
        try:
            import json
            subs = json.loads(subjects_json_str)
            weights_map = getattr(cfg, "YOLO_SUBJECT_WEIGHTS", {"person": 5.0, "cat": 4.0, "dog": 4.0})
            valid_subs = [s for s in subs if s.get("label") in weights_map and s.get("conf", 0) >= 0.2]
            
            # 如果存在有效主体，计算它们在画面中占据的极值边界
            if len(valid_subs) >= 1:
                xs = []
                ys = []
                for s in valid_subs:
                    cx, cy, bw, bh = s["bbox"]
                    xs.append(cx - bw/2)
                    xs.append(cx + bw/2)
                    ys.append(cy - bh/2)
                    ys.append(cy + bh/2)
                
                group_min_x, group_max_x = min(xs), max(xs)
                group_min_y, group_max_y = min(ys), max(ys)
                
                # 增加 5% 的边缘呼吸空间，防止人脸紧贴边框
                group_min_x = max(0.0, group_min_x - 0.05)
                group_max_x = min(1.0, group_max_x + 0.05)
                group_min_y = max(0.0, group_min_y - 0.05)
                group_max_y = min(1.0, group_max_y + 0.05)
                
                # 主体群在原图的真实物理像素跨度
                sub_w = (group_max_x - group_min_x) * img_w
                sub_h = (group_max_y - group_min_y) * img_h
                
                # 计算为了将群体塞进目标框内，所允许的最大 scale
                max_scale_w = img_area_w / sub_w if sub_w > 0 else scale
                max_scale_h = img_area_h / sub_h if sub_h > 0 else scale
                max_safe_scale = min(max_scale_w, max_scale_h)
                
                # 不能无下限地缩小，极限是 contain 模式（完全展现整张原图，出现大面积留白）
                min_scale = min(ratio_w, ratio_h)
                
                # 如果默认的 fill scale 会导致主体群越界被硬切，我们就妥协退让，缩小 scale
                if scale > max_safe_scale:
                    scale = max(max_safe_scale, min_scale)
        except Exception as e:
            print(f"[WARN] Content-aware scaling failed: {e}")
    # =================================================================

    draw_w = int(img_w * scale)
    draw_h = int(img_h * scale)

    img_resized = img.resize((draw_w, draw_h), Image.LANCZOS)

    left, top = compute_crop_window(draw_w, draw_h, img_area_w, img_area_h, subjects_json_str)
    
    # 防止因为 scale 缩小导致 draw_w 小于 img_area_w，PIL crop 越界产生黑边
    crop_left = left if draw_w >= img_area_w else 0
    crop_right = left + img_area_w if draw_w >= img_area_w else draw_w
    crop_top = top if draw_h >= img_area_h else 0
    crop_bottom = top + img_area_h if draw_h >= img_area_h else draw_h
    
    img_cropped = img_resized.crop((crop_left, crop_top, crop_right, crop_bottom))
    
    # 居中 Letterboxing (如果由于缩小导致图像没填满目标区域，生成高斯模糊的扩大版背景)
    paste_x = (img_area_w - draw_w) // 2 if draw_w < img_area_w else 0
    paste_y = (img_area_h - draw_h) // 2 if draw_h < img_area_h else 0

    if draw_w < img_area_w or draw_h < img_area_h:
        from PIL import ImageFilter, ImageEnhance
        # 1. 采用 Fill 模式(即默认比例)将原图填充整个区域作为背景
        bg_scale = max(img_area_w / img_w, img_area_h / img_h)
        bg_w = int(img_w * bg_scale)
        bg_h = int(img_h * bg_scale)
        bg_resized = img.resize((bg_w, bg_h), Image.LANCZOS)
        
        # 居中裁剪出需要的背景
        bg_left = max(0, (bg_w - img_area_w) // 2)
        bg_top = max(0, (bg_h - img_area_h) // 2)
        bg_cropped = bg_resized.crop((bg_left, bg_top, bg_left + img_area_w, bg_top + img_area_h))
        
        # 2. 高斯模糊参数 (你可以调整这个 radius，数值越大越模糊)
        bg_blurred = bg_cropped.filter(ImageFilter.GaussianBlur(radius=45))
        
        # 3. 调整背景明暗度 (你可以调整这里的 enhance 数值)
        # 数值为 1.0 表示原亮度，小于 1.0 表示变暗。
        # 这里设置为 0.4，让背景变成低调的暗色系，这样能在墨水屏的抖动算法下保留质感，同时让主体照片更突出
        pic_area = ImageEnhance.Brightness(bg_blurred).enhance(0.4)
    else:
        pic_area = Image.new("RGB", (img_area_w, img_area_h), (255, 255, 255))

    pic_area.paste(img_cropped, (paste_x, paste_y))
    
    # 贴到上方
    canvas.paste(pic_area, (0, 0))

    # ---------- 底部文字区域 (毛玻璃圆角矩形) ----------
    padding_x = 24
    text_area_top = CANVAS_HEIGHT - TEXT_AREA_HEIGHT + 15
    text_width = CANVAS_WIDTH - 2 * padding_x

    # 1. 定义毛玻璃区域的坐标 (缩小边缘距离，让圆角矩形更大)
    box_x0 = 6
    box_y0 = text_area_top - 5
    box_x1 = CANVAS_WIDTH - 6
    box_y1 = CANVAS_HEIGHT - 6
    box_w = box_x1 - box_x0
    box_h = box_y1 - box_y0

    # 2. 从画布中裁剪出底层画面并进行高斯模糊
    from PIL import ImageFilter
    bg_crop = canvas.crop((box_x0, box_y0, box_x1, box_y1))
    bg_crop_blurred = bg_crop.filter(ImageFilter.GaussianBlur(radius=15))

    # 3. 创建半透明白色覆盖层 (增加透明度，140/255)
    overlay = Image.new("RGBA", (box_w, box_h), (255, 255, 255, 140))
    glass_bg = Image.alpha_composite(bg_crop_blurred.convert("RGBA"), overlay)

    # 4. 创建圆角遮罩
    mask = Image.new("L", (box_w, box_h), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle((0, 0, box_w, box_h), radius=16, fill=255)

    # 5. 把处理好的毛玻璃图层贴回画布
    canvas.paste(glass_bg.convert("RGB"), (box_x0, box_y0), mask)

    def _get_font(path: str, size: int):
        if path and Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
        
        # Windows 系统中文字体回退
        if os.name == "nt":
            for fallback in [r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simsun.ttc", r"C:\Windows\Fonts\simhei.ttf"]:
                if os.path.exists(fallback):
                    try:
                        return ImageFont.truetype(fallback, size)
                    except Exception:
                        continue
        
        return ImageFont.load_default()

    font_big = _get_font(str(FONT_PATH), 22)  # 文案
    font_small = _get_font(str(FONT_PATH), 20)  # 日期/地点

    side_text = item.get("side") or ""

    # 因为有了半透明背景，文字可以使用纯黑色。
    # 为了实现“加粗(bold)”效果，我们使用与填充色相同的描边 (stroke_width=1)
    text_fill = (0, 0, 0)
    stroke_w = 1

    # 文案：最多两行，从 text_area_top 开始
    y = text_area_top
    if side_text:
        lines = wrap_text_chinese(draw, side_text, font_big, text_width, max_lines=2)
        for line in lines:
            draw.text((padding_x, y), line, font=font_big, fill=text_fill, stroke_width=stroke_w, stroke_fill=text_fill)
            y += 24  # 行高略大于字号

    # 日期 + 地点：固定在底部区域内的第二行
    date_display = format_date_display(item["date"])
    loc_display = format_location(item.get("lat"), item.get("lon"), item.get("city") or "")

    second_line_y = text_area_top + 54
    draw.text((padding_x, second_line_y), date_display, font=font_small, fill=text_fill, stroke_width=stroke_w, stroke_fill=text_fill)

    loc_w = draw.textlength(loc_display, font=font_small)
    loc_x = padding_x + text_width - loc_w
    if loc_x < padding_x:
        loc_x = padding_x
    draw.text((loc_x, second_line_y), loc_display, font=font_small, fill=text_fill, stroke_width=stroke_w, stroke_fill=text_fill)

    return canvas

def apply_four_color_dither(img: Image.Image) -> Image.Image:
    """
    对图像做 Floyd–Steinberg 抖动，量化到四种颜色（黑/白/红/黄）。
    """
    img = img.convert("RGB")
    w, h = img.size
    pixels = img.load()

    err_r = [0.0] * w
    err_g = [0.0] * w
    err_b = [0.0] * w
    next_err_r = [0.0] * w
    next_err_g = [0.0] * w
    next_err_b = [0.0] * w

    for y in range(h):
        for x in range(w):
            r, g, b = pixels[x, y]
            r = max(0.0, min(255.0, r + err_r[x]))
            g = max(0.0, min(255.0, g + err_g[x]))
            b = max(0.0, min(255.0, b + err_b[x]))

            idx, pr, pg, pb = nearest_palette_color(r, g, b)

            # 写回量化后的颜色
            pixels[x, y] = (pr, pg, pb)

            # 误差
            er = r - pr
            eg = g - pg
            eb = b - pb

            # Floyd–Steinberg:
            #        *   7/16
            #   3/16 5/16 1/16
            if x + 1 < w:
                err_r[x + 1] += er * (7.0 / 16.0)
                err_g[x + 1] += eg * (7.0 / 16.0)
                err_b[x + 1] += eb * (7.0 / 16.0)
            if y + 1 < h:
                if x > 0:
                    next_err_r[x - 1] += er * (3.0 / 16.0)
                    next_err_g[x - 1] += eg * (3.0 / 16.0)
                    next_err_b[x - 1] += eb * (3.0 / 16.0)
                next_err_r[x] += er * (5.0 / 16.0)
                next_err_g[x] += eg * (5.0 / 16.0)
                next_err_b[x] += eb * (5.0 / 16.0)
                if x + 1 < w:
                    next_err_r[x + 1] += er * (1.0 / 16.0)
                    next_err_g[x + 1] += eg * (1.0 / 16.0)
                    next_err_b[x + 1] += eb * (1.0 / 16.0)

        if y + 1 < h:
            # 把 next_err_* 移到当前行，并清零 next_err_*
            for i in range(w):
                err_r[i] = next_err_r[i]
                err_g[i] = next_err_g[i]
                err_b[i] = next_err_b[i]
                next_err_r[i] = 0.0
                next_err_g[i] = 0.0
                next_err_b[i] = 0.0

    return img


def image_to_palette_bin(img: Image.Image) -> bytes:
    """
    把已经量化到 PALETTE 的图像转换成 BIN：
    - 行优先，从上到下，从左到右
    - 每像素 1 字节：0=黑,1=白,2=红,3=黄
    """
    img = img.convert("RGB")
    if img.size != (CANVAS_WIDTH, CANVAS_HEIGHT):
        raise RuntimeError(f"图像尺寸错误：{img.size}，应为 {(CANVAS_WIDTH, CANVAS_HEIGHT)}")

    data = bytearray(CANVAS_WIDTH * CANVAS_HEIGHT)
    idx_map = {c: i for i, c in enumerate(PALETTE)}  # (r,g,b) -> index

    for y in range(CANVAS_HEIGHT):
        for x in range(CANVAS_WIDTH):
            r, g, b = img.getpixel((x, y))
            key = (int(r), int(g), int(b))
            idx = idx_map.get(key)
            if idx is None:
                idx, _, _, _ = nearest_palette_color(r, g, b)
            data[y * CANVAS_WIDTH + x] = idx

    return bytes(data)


def write_h_array(bin_path: Path, h_path: Path, array_name: str = "daily_bin"):
    """
    把 BIN 转成 C 数组头文件 latest.h：
    const unsigned int daily_bin_size = ...;
    const uint8_t daily_bin[] = { 0x00, 0x01, ... };
    """
    data = bin_path.read_bytes()
    with open(h_path, "w", encoding="utf-8") as f:
        f.write("// Auto-generated from render_daily_photo.py\n")
        f.write(f"// Size = {len(data)} bytes (480x800, 1 byte/pixel)\n\n")
        f.write(f"const unsigned int {array_name}_size = {len(data)};\n")
        f.write(f"const uint8_t {array_name}[] = {{\n    ")

        for i, b in enumerate(data):
            f.write(f"0x{b:02X}, ")
            if (i + 1) % 16 == 0:
                f.write("\n    ")

        f.write("\n};\n")


# ========== 主流程 ==========

def main():
    items = load_sim_rows()
    if not items:
        raise SystemExit("没有可用照片（exif_json 为空或解析失败）。")

    photos, info = choose_photos_for_today(items, TODAY, count=DAILY_PHOTO_QUANTITY)
    photos = apply_portrait_rerank(photos)

    print("[INFO] 目标月日:", info["target_md"])
    print("[INFO] 实际使用月日:", info["used_md"])
    print("[INFO] 回溯天数(day_offset):", info["day_offset"])
    print("[INFO] 候选数(>阈值):", info["candidate_count"])
    print("[INFO] 当日总数:", info["total_count_md"])
    print("[INFO] 使用兜底全局最大:", info["fallback_global_max"])
    print("[INFO] 选片模式:", info.get("selection_mode", "N/A"))
    print("[INFO] 精英候选数(>=高分阈值):", info.get("elite_count", "N/A"))

    if not photos:
        raise SystemExit("选片结果为空。")

    import shutil

    # 对今天选出的多张照片逐一渲染
    for idx, chosen in enumerate(photos):
        print(f"[INFO] 第 {idx} 张选中照片:", chosen["path"])
        print("[INFO] 拍摄日期:", chosen["date"])
        print("[INFO] 回忆度:", chosen["memory"])
        # 加权得分调试输出
        _fs = chosen.get("_final_score") or compute_final_score(chosen)
        _pw = compute_path_weight(chosen["path"])
        _cw = compute_category_weight(chosen.get("type_raw", ""))
        _rw = compute_recency_penalty(chosen.get("used_at"))
        _pb = chosen.get("_portrait_boost") or compute_portrait_boost(chosen.get("type_raw", ""))
        _ds = chosen.get("_display_score") or _fs * _pb
        print(f"[SELECT] final={_fs:.2f} | portrait_boost={_pb:.2f} | display={_ds:.2f}"
              f" | path_w={_pw:.2f} cat_w={_cw:.2f} recency_w={_rw:.2f}"
              f" | mem={chosen['memory']:.1f} beauty={chosen.get('beauty', 'N/A')}"
              f" | type={chosen.get('type_raw', '')}")
        # 额外调试信息：城市 / 经纬度 / 文案
        print("[DEBUG] 城市:", chosen.get("city", ""))
        print("[DEBUG] 经纬度:", chosen.get("lat"), chosen.get("lon"))
        print("[DEBUG] 文案:", chosen.get("side", ""))

        # 渲染成完整成品图（照片 + 文案 + 日期 + 地点）
        img = render_image(chosen)

        # 抖动成四色墨水屏风格
        img_dithered = apply_four_color_dither(img)

        # 保存预览 PNG（已经是抖动后的效果），按索引区分
        preview_path = BIN_OUTPUT_DIR / f"preview_{idx}.png"
        img_dithered.save(preview_path)
        print(f"[OK] 已保存预览 PNG: {preview_path}")

        # 转 BIN：photo_0.bin, photo_1.bin, ...
        bin_data = image_to_palette_bin(img_dithered)
        bin_path = BIN_OUTPUT_DIR / f"photo_{idx}.bin"
        with open(bin_path, "wb") as f:
            f.write(bin_data)
        print(f"[OK] 已生成 BIN: {bin_path} （大小 {len(bin_data)} 字节）")

        # 头文件数组：photo_0.h, photo_1.h，数组名区分开
        h_path = BIN_OUTPUT_DIR / f"photo_{idx}.h"
        array_name = f"daily_bin_{idx}"
        write_h_array(bin_path, h_path, array_name=array_name)
        print(f"[OK] 已生成头文件数组: {h_path}")

        # 记录展示历史改为由推送展示端执行，此处仅保存此图片的路径
        path_file = BIN_OUTPUT_DIR / f"photo_{idx}.path.txt"
        with open(path_file, "w", encoding="utf-8") as f:
            f.write(chosen["path"])

    # 为兼容旧流程，再额外生成 latest.* 指向第 0 张
    first_bin = BIN_OUTPUT_DIR / "photo_0.bin"
    first_h = BIN_OUTPUT_DIR / "photo_0.h"
    first_preview = BIN_OUTPUT_DIR / "preview_0.png"
    first_path = BIN_OUTPUT_DIR / "photo_0.path.txt"
    latest_bin = BIN_OUTPUT_DIR / "latest.bin"
    latest_h = BIN_OUTPUT_DIR / "latest.h"
    latest_preview = BIN_OUTPUT_DIR / "preview.png"
    latest_path = BIN_OUTPUT_DIR / "latest.path.txt"

    if first_bin.exists():
        shutil.copyfile(first_bin, latest_bin)
        print(f"[OK] 已更新 latest.bin -> {first_bin.name}")
    if first_h.exists():
        shutil.copyfile(first_h, latest_h)
        print(f"[OK] 已更新 latest.h -> {first_h.name}")
    if first_preview.exists():
        shutil.copyfile(first_preview, latest_preview)
        print(f"[OK] 已更新 preview.png -> {first_preview.name}")
    if first_path.exists():
        shutil.copyfile(first_path, latest_path)


if __name__ == "__main__":
    main()