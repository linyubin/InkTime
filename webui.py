#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations
from flask import Flask, render_template, jsonify, send_file, abort, request
import sqlite3
import json
from pathlib import Path
import config as cfg
from common import extract_date_from_exif, parse_dirty_type, resolve_path
import re

app = Flask(__name__)

# Config loading
ROOT_DIR = Path(__file__).resolve().parent

DB_PATH = resolve_path(getattr(cfg, "DB_PATH", "./photos.db"), "./photos.db")
IMAGE_DIR = resolve_path(getattr(cfg, "IMAGE_DIR", ""))

# Using a distinct port for the webui manager
WEBUI_HOST = str(getattr(cfg, "WEBUI_HOST", "0.0.0.0") or "0.0.0.0")
WEBUI_PORT = int(getattr(cfg, "WEBUI_PORT", 8766) or 8766)

# Performance tuning: Precompute IMAGE_DIR string for fast image url prefix checking
_IMAGE_DIR_FAST = str(IMAGE_DIR).replace("\\", "/")


def _make_image_url(path_str: str) -> str:
    """把长绝对路径转为前端能访问的 /images 开头的路由。

    优先用原始路径（适配本机 IMAGE_DIR），匹配失败再尝试 PATH_MAP 转换（适配跨平台场景）。
    这样无论数据库是在 Windows 还是 Linux 上生成，本地 WebUI 都能找到图片。
    """
    def _try_match(clean: str) -> str:
        """尝试把 clean 路径（已统一为 /）匹配到 /images/ 路由"""
        # 1. 快速前缀匹配
        if clean.startswith(_IMAGE_DIR_FAST):
            rel = clean[len(_IMAGE_DIR_FAST):].lstrip("/")
            return "/images/" + rel

        # 2. 基于文件夹名自适应匹配（同名目录在不同挂载点）
        base_name = IMAGE_DIR.name
        pattern = f"/{base_name}/"
        if pattern in clean:
            suffix = clean.split(pattern, 1)[1]
            test_path = IMAGE_DIR / ".." / base_name / suffix
            try:
                if test_path.resolve().exists():
                    return "/images/" + suffix
            except Exception:
                pass

        # 3. 文件系统 resolve 兜底
        try:
            p = Path(clean).expanduser().resolve()
            rel = p.relative_to(IMAGE_DIR.resolve())
            return "/images/" + str(rel).replace("\\", "/")
        except Exception:
            return ""

    # ── 第一优先：用原始路径直接匹配（适合 Windows 本地 IMAGE_DIR）──
    result = _try_match(path_str.replace("\\", "/"))
    if result:
        return result

    # ── 第二优先：应用 PATH_MAP 后再匹配（适合 Linux / 树莓派跨平台场景）──
    path_map = getattr(cfg, "PATH_MAP", {})
    mapped = path_str
    for old_prefix, new_prefix in path_map.items():
        if path_str.startswith(old_prefix):
            mapped = path_str.replace(old_prefix, new_prefix).replace("\\", "/")
            break
    if mapped != path_str:
        result = _try_match(mapped.replace("\\", "/"))
        if result:
            return result

    return ""


def _safe_join(base: Path, rel: str) -> Path:
    """防目录穿越"""
    try:
        p = (base / rel).resolve()
        if not str(p).startswith(str(base.resolve())):
            raise ValueError("path traversal blocked")
        return p
    except Exception:
        raise ValueError("path resolution failed")


def _get_conn():
    if not DB_PATH.exists():
        return None
    uri = f"file:{DB_PATH.as_posix()}?mode=ro"
    try:
        return sqlite3.connect(uri, uri=True)
    except sqlite3.OperationalError:
        return sqlite3.connect(DB_PATH)


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/photos/meta')
def api_photos_meta():
    """独立接口：返回所有照片的路径目录、城市、类型列表，用于填充下拉框
    仅查询需要的字段，不做 EXIF 解析，速度很快。
    """
    conn = _get_conn()
    if conn is None:
        return jsonify({"dirs": [], "cities": [], "types": []})

    c = conn.cursor()
    rows = c.execute('SELECT path, type, exif_city FROM photo_scores').fetchall()
    conn.close()

    dirs = set()
    cities = set()
    types = set()

    for path, p_type, city in rows:
        clean_path = (path or "").replace("\\", "/")
        idx = clean_path.rfind("/")
        if idx > 0:
            dirs.add(clean_path[:idx])
        if city:
            cities.add(city)
        if p_type:
            # 统一走 common 的脏 type 解析（与 server /sim 同一份实现）
            types.update(parse_dirty_type(p_type))

    return jsonify({
        "dirs": sorted(dirs),
        "cities": sorted(cities),
        "types": sorted(t for t in types if t and len(t) < 20 and '{' not in t and '[' not in t)
    })


@app.route('/api/photos')
def api_photos():
    """DataTables server-side 分页接口。
    支持参数: draw, start, length, order[0][column], order[0][dir]
    以及自定义过滤: f_date_op, f_date_val, f_path, f_path_exclude, f_types, f_cities,
                    f_mem_op, f_mem_val, f_bf_op, f_bf_val, f_used
    """
    conn = _get_conn()
    if conn is None:
        return jsonify({"draw": 1, "recordsTotal": 0, "recordsFiltered": 0, "data": []})

    draw = int(request.args.get('draw', 1))
    start = int(request.args.get('start', 0))
    length = int(request.args.get('length', 50))

    # 排序
    order_col_idx = request.args.get('order[0][column]', '4')
    order_dir = request.args.get('order[0][dir]', 'desc').upper()
    if order_dir not in ('ASC', 'DESC'):
        order_dir = 'DESC'

    col_map = {
        '0': 'date_str',
        '1': 'path',
        '2': 'type',
        '3': 'exif_city',
        '4': 'memory_score',
        '5': 'beauty_score',
        '6': 'used_at',
    }
    order_col = col_map.get(order_col_idx, 'memory_score')

    # 过滤参数
    f_date_op = request.args.get('f_date_op', '')
    f_date_val = request.args.get('f_date_val', '')
    f_path = request.args.get('f_path', '')
    f_path_exclude = request.args.get('f_path_exclude', '') == '1'
    f_types = request.args.getlist('f_types[]')
    f_cities = request.args.getlist('f_cities[]')
    f_mem_op = request.args.get('f_mem_op', '')
    f_mem_val = request.args.get('f_mem_val', '')
    f_bf_op = request.args.get('f_bf_op', '')
    f_bf_val = request.args.get('f_bf_val', '')
    f_used = request.args.get('f_used', '')

    # 使用 json_extract 在 SQL 层提取日期，比 Python 循环快一个数量级
    base_select = """
        SELECT
            path,
            caption,
            type,
            memory_score,
            beauty_score,
            exif_city,
            reason,
            side_caption,
            used_at,
            exif_json,
            subjects_json,
            COALESCE(
                CASE
                    WHEN json_extract(exif_json, '$.datetime') IS NOT NULL
                    THEN substr(replace(substr(json_extract(exif_json, '$.datetime'), 1, 10), ':', '-'), 1, 10)
                    ELSE NULL
                END,
                ''
            ) AS date_str
        FROM photo_scores
    """

    # 构建 WHERE 子句
    conditions = []
    params = []

    if f_date_val and f_date_op in ('=', '<', '>'):
        # date_str 只来自 EXIF，路径日期在 SQL 层不好提取，作为最优努力
        if f_date_op == '=':
            conditions.append("date_str = ?")
        elif f_date_op == '<':
            conditions.append("date_str < ? AND date_str != ''")
        elif f_date_op == '>':
            conditions.append("date_str > ? AND date_str != ''")
        params.append(f_date_val)

    if f_path:
        clean_f_path = f_path.replace("\\", "/")
        if f_path_exclude:
            conditions.append("REPLACE(path, '\\', '/') NOT LIKE ?")
        else:
            conditions.append("REPLACE(path, '\\', '/') LIKE ?")
        params.append(f"%{clean_f_path}%")

    if f_types:
        type_conds = " OR ".join(["type LIKE ?"] * len(f_types))
        conditions.append(f"({type_conds})")
        for t in f_types:
            params.append(f"%{t}%")

    if f_cities:
        placeholders = ",".join(["?"] * len(f_cities))
        conditions.append(f"exif_city IN ({placeholders})")
        params.extend(f_cities)

    if f_mem_val:
        try:
            mv = float(f_mem_val)
            if f_mem_op == '>':
                conditions.append("memory_score > ?")
            elif f_mem_op == '<':
                conditions.append("memory_score < ?")
            elif f_mem_op == '=':
                conditions.append("ABS(memory_score - ?) < 0.01")
            params.append(mv)
        except ValueError:
            pass

    if f_bf_val:
        try:
            bv = float(f_bf_val)
            if f_bf_op == '>':
                conditions.append("beauty_score > ?")
            elif f_bf_op == '<':
                conditions.append("beauty_score < ?")
            elif f_bf_op == '=':
                conditions.append("ABS(beauty_score - ?) < 0.01")
            params.append(bv)
        except ValueError:
            pass

    if f_used == '已上屏':
        conditions.append("used_at IS NOT NULL AND used_at != ''")
    elif f_used == '未上屏':
        conditions.append("(used_at IS NULL OR used_at = '')")

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    c = conn.cursor()

    # 总数
    total_count = c.execute("SELECT COUNT(*) FROM photo_scores").fetchone()[0]

    # 过滤后总数
    if conditions:
        filtered_count = c.execute(
            f"SELECT COUNT(*) FROM ({base_select}) {where_clause}", params
        ).fetchone()[0]
    else:
        filtered_count = total_count

    # 分页数据
    query = f"""
        SELECT * FROM ({base_select})
        {where_clause}
        ORDER BY {order_col} {order_dir} NULLS LAST
        LIMIT ? OFFSET ?
    """
    rows = c.execute(query, params + [length, start]).fetchall()
    conn.close()

    data = []
    for r in rows:
        path, caption, p_type, memory_score, beauty_score, city, reason, side_caption, used_at, exif_json, subjects_json, date_str = r

        # 路径日期兜底：如果 SQL 层没提取到，再用正则
        if not date_str:
            date_str = extract_date_from_exif(None, path)

        data.append({
            "date": date_str,
            "path": path,
            "image_url": _make_image_url(path),
            "type": p_type or "",
            "memory": memory_score if memory_score is not None else -1,
            "beauty": beauty_score if beauty_score is not None else -1,
            "city": city or "",
            "caption": caption or "",
            "reason": reason or "",
            "side_caption": side_caption or "",
            "used": "已上屏" if used_at else "未上屏",
            "exif_json": exif_json or "{}",
            "subjects_json": subjects_json or "[]"
        })

    return jsonify({
        "draw": draw,
        "recordsTotal": total_count,
        "recordsFiltered": filtered_count,
        "data": data
    })


@app.route('/images/<path:filepath>')
def serve_image(filepath):
    try:
        abs_path = _safe_join(IMAGE_DIR, filepath)
        if not abs_path.exists() or not abs_path.is_file():
            abort(404)
        return send_file(abs_path)
    except ValueError:
        abort(403)
    except Exception:
        abort(404)


if __name__ == '__main__':
    print(f"==================================================")
    print(f"InkTime Independent WebUI Manager")
    print(f"启动 URL: http://{WEBUI_HOST}:{WEBUI_PORT}")
    print(f"==================================================")
    app.run(host=WEBUI_HOST, port=WEBUI_PORT, debug=True)
