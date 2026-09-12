#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
节假日匹配模块（2026-09-12 设计，docs/plans/2026-09-12-holiday-mode-design.md）。

职责单一：读 holidays.json，判定"某天是什么节日"。纯逻辑、不依赖 PIL，
供 render_daily_photo.py 每日调用一次。

匹配口径：
- 判定基准日 = 渲染当天（展示日），与照片历史拍摄日期无关。
- 公历：比对 (month, day)；带年份的一次性日期比对完整 (y, m, d)。
- 农历：lunardate 转换后比对农历 (month, day)；闰月默认不匹配，
  条目 "leap": true 时只匹配闰该月。
- "chuxi"：明天是农历正月初一，则今天是除夕（免查农历月天数表）。
- 多条命中取配置顺序第一条，不叠加。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import config as cfg

# 实现的模板 ID（与 holiday_render.py 对齐；未知值回落 general_ribbon）
KNOWN_TEMPLATES = ("general_ribbon", "child_birthday", "elder_birthday",
                   "adult_birthday", "anniversary")

DEFAULT_TEMPLATE = "general_ribbon"

try:
    from lunardate import LunarDate
    _LUNAR_OK = True
except Exception:  # ImportError 等：农历条目跳过，公历照常（见 §8 降级表）
    _LUNAR_OK = False


# ========== 配置 ==========

def _holidays_file() -> Path:
    p = Path(str(getattr(cfg, "HOLIDAYS_FILE", "./holidays.json") or "./holidays.json"))
    if not p.is_absolute():
        p = (Path(__file__).resolve().parent / p).resolve()
    return p


def _enabled() -> bool:
    return bool(getattr(cfg, "HOLIDAY_MODE_ENABLED", True))


# ========== 配置解析 ==========

def _parse_date_token(tok: str, field: str, name: str
                      ) -> Optional[Tuple[Optional[int], int, int]]:
    """
    "8-15" -> (None, 8, 15)（每年循环）；"2027-3-5" -> (2027, 3, 5)（一次性）。
    非法返回 None（调用方打警告并跳过该条目）。
    """
    parts = str(tok).strip().split("-")
    try:
        if len(parts) == 3:
            y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
            if not (1 <= m <= 12 and 1 <= d <= 31):
                raise ValueError
            return y, m, d
        if len(parts) == 2:
            m, d = int(parts[0]), int(parts[1])
            if not (1 <= m <= 12 and 1 <= d <= 31):
                raise ValueError
            return None, m, d
    except ValueError:
        pass
    print(f"[HOLIDAY] ⚠️ {name}: {field}=\"{tok}\" 格式非法（应为 \"月-日\" 或 \"年-月-日\"），跳过该条目")
    return None


def load_holidays(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """
    读入并归一化节假日列表。文件缺失 / 解析失败返回 []（每日主流程零风险）。
    归一化条目：{name, calendar, start:(y|m, m, d), end, special, template,
                greeting, ai_hint, leap}
    """
    f = Path(path) if path else _holidays_file()
    if not _enabled():
        return []
    if not f.exists():
        print(f"[HOLIDAY] 未找到 {f}，节假日模式未启用（不影响每日渲染）")
        return []
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[HOLIDAY] ⚠️ {f.name} 解析失败（{e}），按无节日处理")
        return []
    entries: List[Dict[str, Any]] = []
    for i, raw in enumerate(data.get("holidays", []) if isinstance(data, dict) else []):
        name = str(raw.get("name") or "").strip()
        if not name:
            print(f"[HOLIDAY] ⚠️ 第 {i + 1} 条缺少 name，跳过")
            continue
        calendar = str(raw.get("calendar") or "solar").strip().lower()
        if calendar not in ("solar", "lunar"):
            print(f"[HOLIDAY] ⚠️ {name}: calendar=\"{calendar}\" 非法（solar/lunar），跳过")
            continue
        start_tok = str(raw.get("start") or "").strip()
        special = "chuxi" if start_tok.lower() == "chuxi" else ""
        entry: Dict[str, Any] = {
            "name": name,
            "calendar": calendar,
            "start": None,
            "end": None,
            "special": special,
            "template": str(raw.get("template") or DEFAULT_TEMPLATE).strip(),
            "greeting": str(raw.get("greeting") or "").strip(),
            "ai_hint": str(raw.get("ai_hint") or "").strip(),
            "leap": bool(raw.get("leap", False)),
        }
        if entry["template"] not in KNOWN_TEMPLATES:
            print(f"[HOLIDAY] ⚠️ {name}: 未知模板 \"{entry['template']}\"，回退 {DEFAULT_TEMPLATE}")
            entry["template"] = DEFAULT_TEMPLATE
        if special:
            if calendar != "lunar":
                print(f"[HOLIDAY] ⚠️ {name}: chuxi 规则仅对农历有意义，跳过")
                continue
            if not _LUNAR_OK:
                print("[HOLIDAY] ⚠️ 未安装 lunardate，农历条目（含除夕）无法匹配：pip install lunardate")
                continue
            entries.append(entry)
            continue
        start = _parse_date_token(start_tok, "start", name)
        if start is None:
            continue
        entry["start"] = start
        end_tok = str(raw.get("end") or "").strip()
        if end_tok:
            end = _parse_date_token(end_tok, "end", name)
            if end is None:
                continue
            # 范围端点年份补齐：起点带年、终点省年 → 同年；反之亦然
            if end[0] is None and start[0] is not None:
                end = (start[0], end[1], end[2])
            if start[0] is None and end[0] is not None:
                start = (end[0],) + start[1:]
            entry["start"], entry["end"] = start, end
        if calendar == "lunar" and not _LUNAR_OK:
            print(f"[HOLIDAY] ⚠️ {name}: 农历条目在未安装 lunardate 时无法匹配，跳过")
            continue
        entries.append(entry)
    return entries


# ========== 日期匹配 ==========

def _md_range_hit(cur: Tuple[int, int], start: Tuple[int, int],
                  end: Tuple[int, int]) -> bool:
    """(月, 日) 范围命中，支持跨年回绕（12-25 ~ 1-2）。"""
    if start <= end:
        return start <= cur <= end
    return cur >= start or cur <= end


def _solar_hit(entry: Dict[str, Any], d: dt.date) -> bool:
    sy, sm, sd = entry["start"]
    if entry["end"]:
        ey, em, ed = entry["end"]
        if sy is not None:  # 带年的一次性范围
            if not (sy <= d.year <= ey):
                return False
            return dt.date(sy, sm, sd) <= d <= dt.date(ey, em, ed)
        return _md_range_hit((d.month, d.day), (sm, sd), (em, ed))
    if sy is not None:
        return (d.year, d.month, d.day) == (sy, sm, sd)
    return (d.month, d.day) == (sm, sd)


def _to_lunar(d: dt.date) -> Optional[Any]:
    try:
        return LunarDate.fromSolarDate(d.year, d.month, d.day)
    except Exception:
        return None


def _lunar_hit(entry: Dict[str, Any], d: dt.date) -> bool:
    ld = _to_lunar(d)
    if ld is None:
        return False
    is_leap_day = bool(getattr(ld, "isLeapMonth", False))
    if entry["leap"] != is_leap_day:
        return False
    cur = (ld.month, ld.day)
    if entry["special"] == "chuxi":
        tomorrow = _to_lunar(d + dt.timedelta(days=1))
        return (tomorrow is not None and tomorrow.month == 1 and tomorrow.day == 1
                and not entry["leap"])
    sy, sm, sd = entry["start"]
    if entry["end"]:
        ey, em, ed = entry["end"]
        if sy is not None and (sy, ey) != (d.year, d.year):
            # 带年农历范围：年份落在范围内才继续（农历年≈公历年，按公历年粗筛）
            if not (sy <= d.year <= ey):
                return False
        return _md_range_hit(cur, (sm, sd), (em, ed))
    if sy is not None:
        return d.year == sy and cur == (sm, sd)
    return cur == (sm, sd)


def _entry_hit(entry: Dict[str, Any], d: dt.date) -> bool:
    if entry["calendar"] == "lunar":
        return _lunar_hit(entry, d)
    return _solar_hit(entry, d)


def get_today_holiday(date: dt.date,
                      entries: Optional[List[Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
    """
    判定 date 命中的节假日。命中返回：
      {"name", "greeting", "template", "ai_hint"}
    未命中 / 模式关闭 / 配置为空返回 None。多条命中取配置顺序第一条。
    """
    if entries is None:
        entries = load_holidays()
    for entry in entries:
        if _entry_hit(entry, date):
            return {
                "name": entry["name"],
                "greeting": entry["greeting"],
                "template": entry["template"],
                "ai_hint": entry["ai_hint"] or entry["name"],
            }
    return None
