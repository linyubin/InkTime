#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
holidays.py 匹配单测（无网络依赖，venv python 直跑）：
    venv/bin/python scripts/test_holidays.py

已知真值参考：
- 2026 中秋 = 2026-09-25（农历 8-15）；2027 春节 = 2027-02-06（农历 1-1），
  除夕 = 2027-02-05；春节 1-1~1-6 = 2027-02-06 ~ 2027-02-11。
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from holidays import DEFAULT_TEMPLATE, get_today_holiday, load_holidays  # noqa: E402

D = dt.date
_passed = _failed = 0


def check(desc: str, cond: bool) -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ✓ {desc}")
    else:
        _failed += 1
        print(f"  ✗ {desc}")


def make_entries(spec: dict) -> list:
    """把 JSON spec 写进临时文件再走真实加载路径（含校验/归一化）。"""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as f:
        json.dump(spec, f, ensure_ascii=False)
        tmp = Path(f.name)
    try:
        return load_holidays(tmp)
    finally:
        tmp.unlink(missing_ok=True)


def name_of(entries, d) -> str:
    h = get_today_holiday(d, entries)
    return h["name"] if h else ""


def main() -> None:
    print("== 公历：单日循环 / 范围 / 一次性 ==")
    entries = make_entries({"holidays": [
        {"name": "国庆节", "calendar": "solar", "start": "10-1", "end": "10-7"},
        {"name": "爸爸生日", "calendar": "solar", "start": "2027-3-5"},
    ]})
    check("2026-10-01 国庆", name_of(entries, D(2026, 10, 1)) == "国庆节")
    check("2026-10-07 国庆（范围含端点）", name_of(entries, D(2026, 10, 7)) == "国庆节")
    check("2026-10-08 不命中", name_of(entries, D(2026, 10, 8)) == "")
    check("2027-03-05 爸爸生日（一次性）", name_of(entries, D(2027, 3, 5)) == "爸爸生日")
    check("2028-03-05 不命中（一次性只当年）", name_of(entries, D(2028, 3, 5)) == "")

    print("== 公历：跨年回绕 ==")
    entries = make_entries({"holidays": [
        {"name": "跨年", "calendar": "solar", "start": "12-25", "end": "1-2"},
    ]})
    check("2026-12-26 命中（回绕后段）", name_of(entries, D(2026, 12, 26)) == "跨年")
    check("2027-01-01 命中（回绕后段）", name_of(entries, D(2027, 1, 1)) == "跨年")
    check("2027-01-03 不命中", name_of(entries, D(2027, 1, 3)) == "")
    check("2026-12-24 不命中", name_of(entries, D(2026, 12, 24)) == "")

    print("== 农历：单日 / 范围 / 除夕 ==")
    entries = make_entries({"holidays": [
        {"name": "中秋节", "calendar": "lunar", "start": "8-15"},
        {"name": "春节", "calendar": "lunar", "start": "1-1", "end": "1-6"},
        {"name": "除夕", "calendar": "lunar", "start": "chuxi"},
    ]})
    check("2026-09-25 中秋", name_of(entries, D(2026, 9, 25)) == "中秋节")
    check("2026-09-24 不命中", name_of(entries, D(2026, 9, 24)) == "")
    check("2027-02-05 除夕", name_of(entries, D(2027, 2, 5)) == "除夕")
    check("2027-02-06 春节", name_of(entries, D(2027, 2, 6)) == "春节")
    check("2027-02-11 春节（1-6 含端点）", name_of(entries, D(2027, 2, 11)) == "春节")
    check("2027-02-12 不命中（1-7 出范围）", name_of(entries, D(2027, 2, 12)) == "")

    print("== 农历：闰月 ==")
    from lunardate import LunarDate
    leap_day = LunarDate(2023, 2, 1, True).toSolarDate()  # 2023 闰二月初一
    entries = make_entries({"holidays": [
        {"name": "闰二月节", "calendar": "lunar", "start": "2-1", "leap": True},
        {"name": "平二月节", "calendar": "lunar", "start": "2-1"},
    ]})
    check(f"{leap_day} 闰二月初一 → 只命中 leap 条目",
          name_of(entries, leap_day) == "闰二月节")
    normal_day = LunarDate(2023, 2, 1, False).toSolarDate()
    check(f"{normal_day} 平二月初一 → 只命中非 leap 条目",
          name_of(entries, normal_day) == "平二月节")

    print("== 多条命中取配置顺序第一条 / 字段回退 ==")
    entries = make_entries({"holidays": [
        {"name": "A", "calendar": "solar", "start": "10-1", "end": "10-7"},
        {"name": "B", "calendar": "solar", "start": "10-3"},
    ]})
    h = get_today_holiday(D(2026, 10, 3), entries)
    check("10-3 同时命中 A/B → 取靠前的 A", h["name"] == "A")
    check("greeting 缺省为空串", h["greeting"] == "")
    check("template 缺省为 general_ribbon", h["template"] == DEFAULT_TEMPLATE)
    check("ai_hint 缺省回退 name", get_today_holiday(D(2026, 10, 3), entries)["ai_hint"] == "A")

    print("== 容错降级 ==")
    check("文件不存在 → 空表", load_holidays(Path("/nonexistent/x.json")) == [])
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as f:
        f.write("{broken json!!")
        bad = Path(f.name)
    try:
        check("坏 JSON → 空表", load_holidays(bad) == [])
    finally:
        bad.unlink(missing_ok=True)
    entries = make_entries({"holidays": [
        {"name": "坏日期", "calendar": "solar", "start": "13-45"},
        {"name": "坏日历", "calendar": "moon", "start": "1-1"},
        {"calendar": "solar", "start": "1-1"},  # 缺 name
        {"name": "国庆节", "calendar": "solar", "start": "10-1"},
    ]})
    check("非法条目被跳过，合法条目保留", len(entries) == 1
          and entries[0]["name"] == "国庆节")

    print(f"\n结果: {_passed} 通过, {_failed} 失败")
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()
