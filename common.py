# -*- coding: utf-8 -*-
"""
common.py — InkTime 各脚本共享的基础解析模块。

收敛此前在 server.py / webui.py / render_daily_photo.py / analyze_photos.py /
repair_db_datetime.py 中各自复制的四类逻辑：
  1. config 相对路径解析（resolve_path）
  2. 拍摄日期提取（extract_date_from_exif：EXIF JSON 优先，文件名兜底）
  3. 文件名时间戳解析（parse_datetime_from_filename）
  4. 脏 type 字段解析（parse_dirty_type 读方向 / normalize_type_to_list 写方向）

注意：本模块禁止 import config，保持可被任何脚本无副作用地导入。
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent


# ---------- 1. config 路径解析 ----------

def resolve_path(value, default: str = "") -> Path:
    """把 config 里的路径配置解析为绝对路径：相对路径以仓库根为基准。"""
    p = Path(str(value if value not in (None, "") else default)).expanduser()
    if not p.is_absolute():
        p = (ROOT_DIR / p).resolve()
    return p


# ---------- 2. 拍摄日期提取 ----------

# 1) 路径中的 YYYY-MM-DD / YYYY_MM_DD / YYYY.MM.DD
_RE_DATE1 = re.compile(r'(20\d{2}|19\d{2})[-_ \.](0[1-9]|1[0-2])[-_ \.](0[1-9]|[12]\d|3[01])')
# 2) 路径中的 YYYYMMDD（如 20231225）
_RE_DATE2 = re.compile(r'(?:^|[^0-9])((?:20|19)\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?:[^0-9]|$)')
# 3) 路径中的 YYYYMM（如 201607）
_RE_DATE3 = re.compile(r'(?:^|[^0-9])((?:20|19)\d{2})(0[1-9]|1[0-2])(?:[^0-9]|$)')


def extract_date_from_exif(exif_json: str | None, filepath: str = "") -> str:
    """
    从 EXIF JSON 中提取拍摄日期，返回 YYYY-MM-DD 格式，失败则回退到从
    文件名/路径猜测（支持分隔符日期、YYYYMMDD、YYYYMM），再失败返回空字符串。
    """
    date_str = ""
    if exif_json:
        try:
            data = json.loads(exif_json)
            dtv = data.get("datetime")
            if dtv:
                date_part = str(dtv).split()[0]  # "2018:03:18"
                parts = date_part.replace(":", "-").split("-")
                if len(parts) >= 3:
                    date_str = f"{parts[0]}-{parts[1]}-{parts[2]}"
        except Exception:
            pass

    if date_str and len(date_str) == 10:
        return date_str

    if filepath:
        clean_path = filepath.replace('\\', '/')
        m1 = _RE_DATE1.search(clean_path)
        if m1:
            return f"{m1.group(1)}-{m1.group(2)}-{m1.group(3)}"
        m2 = _RE_DATE2.search(clean_path)
        if m2:
            return f"{m2.group(1)}-{m2.group(2)}-{m2.group(3)}"
        m3 = _RE_DATE3.search(clean_path)
        if m3:
            return f"{m3.group(1)}-{m3.group(2)}-01"

    return ""


# ---------- 3. 文件名时间戳解析 ----------

def parse_datetime_from_filename(filename: str) -> str | None:
    """
    从文件名解析可能的拍摄时间，返回 "%Y:%m:%d %H:%M:%S"（EXIF 风格）或 None。
    支持：13 位毫秒 Unix 时间戳（mmexport…）、14 位 YYYYMMDDHHMMSS、
    10 位秒级 Unix 时间戳、8 位 YYYYMMDD。
    """
    # 1. 13 位毫秒级 Unix 时间戳 (例如 mmexport1723116376342.jpg)
    ms_match = re.search(r'(?:^|[^0-9])(1\d{12})(?:[^0-9]|$)', filename)
    if ms_match:
        try:
            ts = int(ms_match.group(1)) / 1000.0
            from datetime import datetime
            return datetime.fromtimestamp(ts).strftime("%Y:%m:%d %H:%M:%S")
        except Exception:
            pass

    # 2. 14 位 YYYYMMDDHHMMSS 格式 (例如 QQ图片20230702183242.jpg)
    dt14_match = re.search(r'(?:^|[^0-9])((?:20|19)\d{12})(?:[^0-9]|$)', filename)
    if dt14_match:
        try:
            from datetime import datetime
            return datetime.strptime(dt14_match.group(1), "%Y%m%d%H%M%S").strftime("%Y:%m:%d %H:%M:%S")
        except Exception:
            pass

    # 3. 10 位秒级 Unix 时间戳 (例如 1723116376)
    s_match = re.search(r'(?:^|[^0-9])(1\d{9})(?:[^0-9]|$)', filename)
    if s_match:
        try:
            ts = int(s_match.group(1))
            if 946684800 <= ts <= 2147483647:
                from datetime import datetime
                return datetime.fromtimestamp(ts).strftime("%Y:%m:%d %H:%M:%S")
        except Exception:
            pass

    # 4. 8 位 YYYYMMDD 格式 (例如 20230702)
    dt8_match = re.search(r'(?:^|[^0-9])((?:20|19)\d{6})(?:[^0-9]|$)', filename)
    if dt8_match:
        try:
            from datetime import datetime
            return datetime.strptime(dt8_match.group(1), "%Y%m%d").strftime("%Y:%m:%d 00:00:00")
        except Exception:
            pass

    return None


# ---------- 4. 脏 type 字段解析 ----------

def parse_dirty_type(ptype_val) -> list[str]:
    """
    把 DB 的 type 字段解析成 tag 数组（读方向，容错优先）。
    兼容三种常见存储：
    - JSON 数组：   ["人物","日常"]
    - 伪数组文本：  [人物, 日常] / [人物，日常]
    - 普通字符串：  人物,日常 / 人物
    """
    if ptype_val is None:
        return []
    s = str(ptype_val).strip()
    if not s:
        return []

    # 1) 先尝试严格 JSON
    if s.startswith("[") and s.endswith("]"):
        try:
            arr = json.loads(s)
            if isinstance(arr, list):
                return [str(x).strip() for x in arr if str(x).strip()]
        except Exception:
            pass  # JSON 不合法：继续走容错
        s = s[1:-1].strip()

    # 2) 容错：去掉最外层 [] 与引号，按中英文逗号切
    s = s.replace('"', '').replace("'", "")
    parts = [p.strip() for p in s.replace('，', ',').split(',')]
    return [p for p in parts if p]


def normalize_type_to_list(type_val) -> str:
    """
    将各种格式的 type 字段标准化为 JSON list 字符串（写方向），
    如 ["人物", "旅行"]；空输入返回 "[]"。
    """
    if type_val is None or str(type_val).strip() == "":
        return "[]"
    s = str(type_val).strip()
    # 尝试解析为合法 JSON list
    try:
        parsed = json.loads(s)
        if isinstance(parsed, list):
            result = [str(t).strip() for t in parsed if str(t).strip()]
            return json.dumps(result, ensure_ascii=False)
    except Exception:
        pass
    # 处理 Python list 字面量
    if s.startswith("[") and s.endswith("]"):
        try:
            parsed = ast.literal_eval(s)
            if isinstance(parsed, list):
                result = [str(t).strip() for t in parsed if str(t).strip()]
                return json.dumps(result, ensure_ascii=False)
        except Exception:
            pass
    # 处理斜杠分隔
    if "/" in s:
        parts = [p.strip() for p in s.split("/") if p.strip()]
        return json.dumps(parts, ensure_ascii=False)
    # 处理中英文逗号分隔
    if "," in s or "，" in s:
        parts = re.split(r"[,，]\s*", s)
        parts = [p.strip().strip("'\" ") for p in parts if p.strip().strip("'\" ")]
        return json.dumps(parts, ensure_ascii=False)
    # 单个值
    return json.dumps([s], ensure_ascii=False)
