#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations
from flask import Blueprint, Flask, redirect, render_template, jsonify, send_file, abort, request
import json
from pathlib import Path
from datetime import datetime
import os
from admin_security import install_admin_security
from device_log_storage import device_log_paths, read_device_log_lines
from download_key_security import resolve_download_keys
from db_schema import open_current_readonly_database
import config as cfg
from path_security import safe_join
from common import extract_date_from_exif, parse_dirty_type, resolve_path
import re

# 以 Blueprint 形式挂载到 server.py（8765 统一入口，见 server.py 的 register_blueprint）；
# 直接运行本文件时由 __main__ 临时组一个 app，兼容旧的独立启动方式。
bp = Blueprint("webui", __name__)

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
        return safe_join(base, rel)
    except Exception:
        raise ValueError("path resolution failed")


def _get_conn():
    """Read-only v2 SQLite connection; never create or silently reopen for writes."""
    return open_current_readonly_database(DB_PATH)


@bp.route('/manager')
def manager():
    return render_template('index.html')


@bp.route('/api/photos/meta')
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


@bp.route('/api/photos')
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


# /images/<path> 由 server.py 提供（合并后共用同一路由，本 blueprint 不再重复注册）


# ── 设备日志（黑盒）只读页 ───────────────────────────────────
# 数据由 server.py 的 POST /api/device_log/<key> 落盘到不含密钥的哈希文件名；
# 历史 logs/device/<key>.log 仅保留读取兼容。
# 每行一条 JSON 事件（设备行号 ls/开机序号 b/相对毫秒 ms/类型 t/明细 d/接收时刻 rcv）。
# 倒序展示最近 N 条，供事故回查，不做任何写操作。

DEVICE_LOG_DIR = ROOT_DIR / "logs" / "device"
DEVLOG_PAGE_SIZE = 500
_ANCHOR_RE = re.compile(r" anchor=(\d+)")


@bp.route('/devlog')
def devlog():
    # Include both keys during an explicitly configured rotation. Read the
    # previous key first, then the active key, and legacy files before new ones.
    # Invalid standalone config must not turn into an arbitrary file read.
    try:
        keys = resolve_download_keys(getattr(cfg, "DOWNLOAD_KEY", ""), os.environ)
    except ValueError:
        keys = ()
    events = []
    exists = False
    for key in reversed(keys):
        for p in device_log_paths(DEVICE_LOG_DIR, key):
            try:
                lines = read_device_log_lines(p)
            except OSError:
                # Never display symlinked or substituted journal contents.
                abort(503, description="device journal unavailable")
            if lines is None:
                continue
            exists = True
            for ln in lines:
                try:
                    ev = json.loads(ln)
                    if isinstance(ev, dict):
                        events.append(ev)
                except ValueError:
                    continue

    # 绝对时间换算：TIME_SYNC 事件的 anchor=epoch 是真实墙钟。
    # 两遍扫描——第一遍按开机序号收集各自的锚点，第二遍用"本开机"的锚点换算。
    # （单遍会让开机早期、锚点之前的行错用上一次开机的锚点，时间显示穿越。）
    anchors = {}
    for ev in events:
        if ev.get("t") == "TIME_SYNC":
            m = _ANCHOR_RE.search(str(ev.get("d", "")))
            if m:
                anchors[ev.get("b")] = (int(m.group(1)), int(ev.get("ms", 0)))
    for ev in events:
        d = str(ev.get("d", ""))
        a = anchors.get(ev.get("b"))
        if a:
            abs_s = a[0] + (int(ev.get("ms", 0)) - a[1]) / 1000.0
            ev["abs"] = datetime.fromtimestamp(abs_s).strftime("%m-%d %H:%M:%S")
        else:
            ev["abs"] = ""
        ev["d"] = _ANCHOR_RE.sub("", d)   # 明细里去掉锚点数字，页面不重复展示

    rows = events[-DEVLOG_PAGE_SIZE:][::-1]   # 最近 N 条，新事件在前
    return render_template('devlog.html', rows=rows, exists=exists)


# ── 样式编辑器（/style-editor，ADR 0002 版式数据化 + ADR 0003 预览保真）──

import io
import re
import uuid

from PIL import Image as PILImage, ImageOps

import style_registry
import style_runtime

STYLE_EDITOR_DIR = ROOT_DIR / "static" / "style_editor"
STYLE_UPLOAD_DIR = STYLE_EDITOR_DIR / "uploads"
_STYLE_ID_RE = re.compile(r"^[A-Z]\d{2,3}$")


def _style_path(sid: str) -> Path:
    sid = str(sid or "").strip().upper()
    if not _STYLE_ID_RE.match(sid):
        abort(400, description="非法版式 ID")
    return ROOT_DIR / "layouts" / f"{sid}.json"


def _validate_style_doc(doc) -> dict:
    """落盘前的最小结构校验（渲染期错误由 style_runtime 抛出）。"""
    if not isinstance(doc, dict):
        abort(400, description="样式必须是 JSON 对象")
    sid = str(doc.get("id") or "").strip().upper()
    if not _STYLE_ID_RE.match(sid):
        abort(400, description="非法版式 ID")
    if doc.get("orientation") not in ("portrait", "landscape"):
        abort(400, description="orientation 必须是 portrait/landscape")
    if not isinstance(doc.get("elements"), list):
        abort(400, description="elements 必须是数组")
    return sid


@bp.route('/style-editor')
def style_editor():
    return send_file(STYLE_EDITOR_DIR / "index.html")


@bp.route('/api/style_meta')
def api_style_meta():
    return jsonify({
        "veil_alpha": int(255 * float(getattr(cfg, "TEXT_VEIL_OPACITY", 0.85))),
        "veil_rgb": [246, 241, 231],
        "inset": int(getattr(cfg, "FRAME_MASK_INSET_PX", 40) or 0),
        "canvas": {"portrait": [480, 800], "landscape": [800, 480]},
        "palette": [[0, 0, 0], [255, 255, 255], [200, 0, 0], [220, 180, 0]],
        "fonts": [{"id": "", "label": "跟随全局"},
                  {"id": "smiley", "label": "得意黑"},
                  {"id": "lxgw", "label": "霞鹜文楷"}],
    })


@bp.route('/api/styles')
def api_styles():
    return jsonify([{"id": s["id"], "name": s.get("name", ""),
                     "orientation": s.get("orientation", "portrait"),
                     "elements": len(s.get("elements", []))}
                    for s in style_registry.all_styles().values()])


@bp.route('/api/styles', methods=['POST'])
def api_style_create():
    body = request.get_json(force=True, silent=True) or {}
    orientation = body.get("orientation", "portrait")
    if orientation not in ("portrait", "landscape"):
        abort(400, description="非法朝向")
    prefix = "P" if orientation == "portrait" else "L"
    ids = [k for k in style_registry.all_styles() if k.startswith(prefix)]
    n = max((int(k[1:]) for k in ids), default=0) + 1
    new_id = f"{prefix}{n:02d}"
    base = style_registry.get_style(body.get("base", "")) if body.get("base") else None
    doc = dict(base) if base else {}
    doc.update({"schema_version": 1, "id": new_id,
                "name": body.get("name") or f"新样式 {new_id}",
                "orientation": orientation})
    path = _style_path(new_id)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    style_registry.load_styles(force=True)
    print(f"[STYLE-EDITOR] 新建 {new_id}（基于 {body.get('base') or '空白'}）")
    return jsonify(doc)


@bp.route('/api/styles/<sid>')
def api_style_get(sid):
    style = style_registry.get_style(sid)
    if style is None:
        abort(404, description="版式不存在")
    return jsonify(style)


@bp.route('/api/styles/<sid>', methods=['PUT'])
def api_style_put(sid):
    path = _style_path(sid)
    doc = request.get_json(force=True, silent=True)
    doc_id = _validate_style_doc(doc)
    if doc_id != sid.upper():
        abort(400, description="body.id 与 URL 不一致")
    if not path.exists():
        abort(404, description="版式不存在")
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(path)
    style_registry.load_styles(force=True)
    print(f"[STYLE-EDITOR] 保存 {doc_id}（{len(doc.get('elements', []))} 元素）")
    return jsonify({"ok": True})


@bp.route('/api/styles/<sid>', methods=['DELETE'])
def api_style_delete(sid):
    path = _style_path(sid)
    if not path.exists():
        abort(404, description="版式不存在")
    path.unlink()
    style_registry.load_styles(force=True)
    print(f"[STYLE-EDITOR] 删除 {sid.upper()}")
    return jsonify({"ok": True})


@bp.route('/api/stress_photos')
def api_stress_photos():
    d = STYLE_EDITOR_DIR / "stress"
    files = sorted(d.glob("*.jpg")) if d.exists() else []
    return jsonify([{"name": f.stem, "url": f"/static/style_editor/stress/{f.name}"}
                    for f in files])


@bp.route('/api/upload_photo', methods=['POST'])
def api_upload_photo():
    f = request.files.get("photo")
    if f is None:
        abort(400, description="缺少 photo 字段")
    try:
        img = ImageOps.exif_transpose(PILImage.open(f.stream)).convert("RGB")
    except Exception:
        abort(400, description="无法解析图片")
    img.thumbnail((1600, 1600), PILImage.LANCZOS)
    STYLE_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex[:12]}.jpg"
    img.save(STYLE_UPLOAD_DIR / name, quality=88)
    return jsonify({"url": f"/static/style_editor/uploads/{name}"})


def _load_editor_photo(photo_url: str) -> PILImage.Image:
    """编辑器传来的照片 URL → 本地文件（防目录穿越）。
    允许 /static/style_editor/（压力照/上传）与 /images/（照片库，载入相框状态用）。"""
    if photo_url and photo_url.startswith("/static/style_editor/"):
        p = _safe_join(STYLE_EDITOR_DIR, photo_url[len("/static/style_editor/"):])
    elif photo_url and photo_url.startswith("/images/"):
        p = _safe_join(IMAGE_DIR, photo_url[len("/images/"):])
    else:
        abort(400, description="photo 必须是 /static/style_editor/ 或 /images/ 下的 URL")
    if not p.exists():
        abort(404, description="照片不存在")
    return ImageOps.exif_transpose(PILImage.open(p)).convert("RGB")


@bp.route('/api/style_photo', methods=['POST'])
def api_style_photo():
    """按版式避让区裁切照片（真实管线 _cover_crop 同一实现）：
    编辑器预览的取景/缩放必须与真机一致——居中 cover 会与实际出图偏差。"""
    body = request.get_json(force=True, silent=True) or {}
    photo = _load_editor_photo(body.get("photo", ""))
    orientation = body.get("orientation", "portrait")
    w, h = (800, 480) if orientation == "landscape" else (480, 800)
    item = {"subjects_json": body.get("subjects_json", "")}
    import render_daily_photo as rdp
    cropped, _ = rdp._cover_crop(photo, w, h, item,
                                 dict(body.get("crop_reserve") or {}))
    buf = io.BytesIO()
    cropped.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


@bp.route('/api/frame_state')
def api_frame_state():
    """相框当前显示状态：当日渲染产物（latest.*）→ 版式/朝向/照片/槽位文案。
    sidecar 带 item 摘要时为精确值（含 holiday_ai 覆写后的实际渲染文案），
    否则从 photos.db 兜底（分析期 side_caption）。"""
    out_dir = ROOT_DIR / str(getattr(cfg, "BIN_OUTPUT_DIR", "./output") or "./output")
    out_dir = (out_dir if out_dir.is_absolute() else ROOT_DIR / out_dir)
    sidecar_path, path_txt = out_dir / "latest.json", out_dir / "latest.path.txt"
    if not sidecar_path.exists() or not path_txt.exists():
        return jsonify({"error": "output/ 下没有 latest.*（尚未渲染过）"}), 404
    try:
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except Exception:
        sidecar = {}
    photo_path = path_txt.read_text(encoding="utf-8").strip()
    item = dict(sidecar.get("item") or {})
    exact = bool(item.get("side") is not None)
    if not item and photo_path:
        conn = _get_conn()
        if conn is not None:
            try:
                row = conn.execute(
                    "SELECT side_caption, exif_city, subjects_json FROM photo_scores WHERE path = ?",
                    (photo_path,)).fetchone()
                conn.close()
                if row:
                    item = {"side": row[0] or "", "city": row[1] or "",
                            "subjects_json": row[2] or ""}
            except Exception:
                pass
    import style_registry
    layout = str(sidecar.get("layout") or "")
    return jsonify({
        "layout": layout,
        "layout_name": style_registry.get_style(layout).get("name", "") if layout else "",
        "orientation": sidecar.get("orientation", "portrait"),
        "holiday": sidecar.get("holiday", ""),
        "item": item,
        "exact": exact,
        "photo_path": photo_path,
        "photo_url": _make_image_url(photo_path) if photo_path else "",
        "generated": datetime.fromtimestamp(
            sidecar_path.stat().st_mtime).isoformat(timespec="minutes"),
    })


@bp.route('/api/style_preview', methods=['POST'])
def api_style_preview():
    """真机对照：后端跑真实管线（cover 裁切含避让 → 版式层 → gamma → 量化）。"""
    body = request.get_json(force=True, silent=True) or {}
    doc = body.get("style")
    _validate_style_doc(doc)
    item = body.get("item") or {}
    orientation = doc["orientation"]
    w, h = (800, 480) if orientation == "landscape" else (480, 800)
    photo = _load_editor_photo(body.get("photo", ""))
    canvas = PILImage.new("RGB", (w, h), (255, 255, 255))
    # 与 _render_with_layout 同一口径：避让区并入 cover 裁切（无主体信号时居中）
    import render_daily_photo as rdp
    cropped, _ = rdp._cover_crop(photo, w, h, item,
                                 dict(doc.get("crop_reserve") or {}))
    canvas.paste(cropped, (0, 0))
    style_runtime.render_style(canvas, doc, item)
    if body.get("dither"):
        gamma, _ = rdp.choose_gamma(canvas)
        canvas = rdp.quantize_four_color(rdp.apply_gamma(canvas, gamma)).convert("RGB")
    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


@bp.route('/style-assets/seal/<hexcode>.png')
def style_asset_seal(hexcode):
    if not re.match(r"^[0-9A-Fa-f]{4,6}$", hexcode):
        abort(400)
    p = ROOT_DIR / "seal_glyphs" / f"{hexcode.upper()}.png"
    if not p.exists():
        abort(404)
    return send_file(p)


@bp.route('/style-assets/font/<fname>')
def style_asset_font(fname):
    if fname not in ("SmileySans-Oblique.ttf", "LXGWHeartSerifMN.ttf"):
        abort(400)
    return send_file(ROOT_DIR / "font" / fname)


# 编辑器自检页（selftest.html）的结果回传：无头 chromium 消费用
_SELFTEST_REPORT = {"pending": True}


@bp.route('/api/selftest_report', methods=['POST', 'GET'])
def api_selftest_report():
    if request.method == "POST":
        _SELFTEST_REPORT.update({"pending": False,
                                 "result": request.get_data(as_text=True)[:20000],
                                 "at": datetime.now().isoformat(timespec="seconds")})
        print(f"[SELFTEST] {_SELFTEST_REPORT['result'][:300]}")
        return jsonify({"ok": True})
    return jsonify(_SELFTEST_REPORT)


def create_standalone_app() -> Flask:
    """Create the optional legacy WebUI with the *same* admin security as server.py.

    The standalone instance intentionally does not allow device-route exceptions.
    """
    app = Flask(__name__)
    install_admin_security(app, allow_device_routes=False)
    app.register_blueprint(bp)

    @app.get("/")
    def _index():
        return redirect("/manager")

    return app


if __name__ == "__main__":
    # Legacy entrypoint only. Prefer server.py; never expose Werkzeug debugger.
    if not os.environ.get("INKTIME_ADMIN_PASSWORD"):
        raise SystemExit(
            "INKTIME_ADMIN_PASSWORD is required for standalone WebUI; "
            "use a private environment file and an HTTPS reverse proxy."
        )
    app = create_standalone_app()
    print("[InkTime] standalone WebUI running on 127.0.0.1 "
          "(localhost only, HTTPS reverse proxy required)")
    app.run(host="127.0.0.1", port=WEBUI_PORT, debug=False)
