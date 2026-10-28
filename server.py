#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

from pathlib import Path
from flask import Flask, abort, send_file, Response, request, redirect, g, render_template, jsonify
import mimetypes
from contextlib import closing
import json
import html
import hashlib
import re
import urllib.parse
import unicodedata
import time
import os
import threading
from datetime import datetime
import config as cfg
from server_config_validation import validate_server_settings, ConfigurationError
from path_security import safe_join
from device_endpoint_security import validate_device_endpoint
from download_key_security import resolve_download_keys, authorized_download_key
from device_log_storage import device_log_paths, device_log_lock, append_private_device_log, read_device_log_lines
from db_schema import (
    MigrationError, require_current_schema, open_current_database,
    open_current_readonly_database,
)
from admin_security import install_admin_security
from common import extract_date_from_exif, parse_dirty_type, resolve_path
from io import BytesIO

ROOT_DIR = Path(__file__).resolve().parent

# --- config ---
try:
    SERVER_SETTINGS = validate_server_settings(cfg)
except ConfigurationError as exc:
    raise SystemExit(f"Invalid server configuration: {exc}") from None
try:
    DOWNLOAD_KEYS = resolve_download_keys(getattr(cfg, "DOWNLOAD_KEY", ""), os.environ)
except ValueError as exc:
    raise SystemExit(f"Invalid download-key configuration: {exc}") from None
DOWNLOAD_KEY = DOWNLOAD_KEYS[0]  # active; previous key requires explicit opt-in

DB_PATH = resolve_path(SERVER_SETTINGS["DB_PATH"], "./photos.db")
# Startup is read-only until the schema is verified. Do not silently create
# photos.db or apply legacy DDL: migrations require an explicit verified backup.
try:
    require_current_schema(DB_PATH)
except MigrationError as exc:
    raise SystemExit(f"InkTime database startup blocked: {exc}") from None

IMAGE_DIR = resolve_path(SERVER_SETTINGS["IMAGE_DIR"])
BIN_OUTPUT_DIR = resolve_path(SERVER_SETTINGS["BIN_OUTPUT_DIR"], "./output")
BIN_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def _init_db_pragmas() -> None:
    """Validate writable v2 connection and WAL before serving requests."""
    try:
        with closing(open_current_database(DB_PATH)):
            pass  # shared helper validates schema, busy_timeout and WAL
    except MigrationError as exc:
        raise SystemExit(f"InkTime database WAL initialization failed: {exc}") from None


_init_db_pragmas()

# Rendering imports create output directories; load only after the config,
# schema and WAL startup gates have completed successfully.
import render_daily_photo as rdp

# 传输日志 / 拉取检测哨兵目录（由 inktime_daily.sh 读写）
TMP_DIR = ROOT_DIR / "tmp"
TMP_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR = ROOT_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
TRANSFER_LOG = LOG_DIR / "transfer.log"           # 详细 HTTP 传输日志（ESP32 拉取等）
LAST_FETCH_SENTINEL = TMP_DIR / "last_fetch.json"  # 拉取成功哨兵，供 inktime_daily.sh 轮询

FLASK_HOST = SERVER_SETTINGS["FLASK_HOST"]
FLASK_PORT = SERVER_SETTINGS["FLASK_PORT"]

# 是否开启照片库 WebUI（跑通后建议关闭，只保留 ESP32 下载接口）
ENABLE_REVIEW_WEBUI = SERVER_SETTINGS["ENABLE_REVIEW_WEBUI"]

DAILY_PHOTO_QUANTITY = SERVER_SETTINGS["DAILY_PHOTO_QUANTITY"]
if DAILY_PHOTO_QUANTITY < 1:
    DAILY_PHOTO_QUANTITY = 1


# review 分页：每页 100 张
REVIEW_PAGE_SIZE = 100

# /review 日期筛选的可用 MM-DD 列表缓存（避免每次都扫全库）
_MD_CACHE: dict[str, object] = {"md_list": [], "built_at": 0.0}
_MD_CACHE_TTL_SEC = 300.0  # 5 分钟

# /sim_render 磁盘缓存：同一 (图片, mtime, 朝向, 抖动) 组合的渲染结果不变，
# 直接命中缓存文件，省掉 2-6s 的 render+dither。
SIM_RENDER_CACHE_DIR = ROOT_DIR / ".cache" / "sim_render"
SIM_RENDER_CACHE_MAX = SERVER_SETTINGS["SIM_RENDER_CACHE_MAX_ENTRIES"]


def _sim_render_cache_path(key_src: str) -> Path:
    return SIM_RENDER_CACHE_DIR / (hashlib.sha1(key_src.encode("utf-8")).hexdigest() + ".png")


def _prune_sim_render_cache() -> None:
    files = sorted(SIM_RENDER_CACHE_DIR.glob("*.png"),
                   key=lambda f: f.stat().st_mtime, reverse=True)
    for f in files[SIM_RENDER_CACHE_MAX:]:
        try:
            f.unlink()
        except OSError:
            pass


# /review 网格缩略图磁盘缓存：网格卡片只有 ~300px 宽，没必要每次从 NAS 拉 1-3MB 原图。
# 首次访问某张照片时读原图生成 480px 缩略图落盘，之后直接命中本地文件（~30-60KB）。
THUMB_CACHE_DIR = ROOT_DIR / ".cache" / "thumbs"
THUMB_CACHE_MAX = int(getattr(cfg, "THUMB_CACHE_MAX_ENTRIES", 8000) or 8000)
THUMB_MAX_EDGE = 480


def _thumb_cache_path(key_src: str) -> Path:
    return THUMB_CACHE_DIR / (hashlib.sha1(key_src.encode("utf-8")).hexdigest() + ".jpg")


def _prune_thumb_cache() -> None:
    files = sorted(THUMB_CACHE_DIR.glob("*.jpg"),
                   key=lambda f: f.stat().st_mtime, reverse=True)
    for f in files[THUMB_CACHE_MAX:]:
        try:
            f.unlink()
        except OSError:
            pass


# /sim 页 HTML 缓存：页面生成要对 DB 做日期窗口查询 + 序列化大 JSON（~2s），
# 同一照片的页面在 TTL 内直接复用。内存占用 ~0.5MB/条。
_SIM_HTML_TTL = 300.0
_SIM_HTML_CACHE_MAX = 16
_SIM_HTML_CACHE: dict[str, tuple[float, str]] = {}


def _load_all_md_list() -> list[str]:
    """从全库提取所有存在的 MM-DD（去重、排序）。用于前端“随机一天”。"""
    if not DB_PATH.exists():
        return []

    # 简单 TTL 缓存
    import time
    now = time.time()
    try:
        built_at = float(_MD_CACHE.get("built_at") or 0.0)
    except Exception:
        built_at = 0.0
    if (now - built_at) < _MD_CACHE_TTL_SEC:
        cached = _MD_CACHE.get("md_list")
        if isinstance(cached, list):
            return [str(x) for x in cached]

    conn = open_current_readonly_database(DB_PATH)
    c = conn.cursor()
    rows = c.execute("SELECT path, exif_json FROM photo_scores").fetchall()
    conn.close()

    s: set[str] = set()
    for (path, exif_json) in rows:
        d = extract_date_from_exif(exif_json, str(path))
        if d and len(d) >= 10:
            md = d[5:10]
            if len(md) == 5 and md[2] == "-":
                s.add(md)

    md_list = sorted(s)
    _MD_CACHE["md_list"] = md_list
    _MD_CACHE["built_at"] = now
    return md_list

app = Flask(__name__)
install_admin_security(app, allow_device_routes=True)

# ── 合并版式管理 WebUI（原 8766 端口的 webui.py）──────────────
# webui.py 现以 Blueprint 提供：/manager（相册管理）、/style-editor（版式编辑器）、
# /devlog（设备日志）等；/images 由本文件统一提供，blueprint 内不再重复注册。
from webui import bp as webui_bp
app.register_blueprint(webui_bp)


# transfer_history is created by explicit v2 migration, never by server import.

@app.before_request
def _before_request() -> None:
    g.t0 = time.time()


@app.after_request
def _after_request(resp):
    # 仅记录 /static/inktime/ 前缀的传输类请求，避免 /review 图片刷屏
    if request.path.startswith("/static/inktime/"):
        dur_ms = (time.time() - getattr(g, "t0", time.time())) * 1000.0
        view_args = request.view_args or {}
        idx_val = view_args.get("idx")
        try:
            size = int(resp.content_length or 0)
        except Exception:
            size = 0
        ts_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # 不将 URL 中的 DOWNLOAD_KEY 写入日志或 SQLite 历史记录。
        safe_path = re.sub(r"^/static/inktime/[^/]+", "/static/inktime/[REDACTED]", request.path)
        log_line = (
            f"{ts_str} | {request.remote_addr} | {request.method} {safe_path} | "
            f"{resp.status_code} | {size}B | {dur_ms:.0f}ms | {request.headers.get('User-Agent', '')}"
        )
        try:
            with open(TRANSFER_LOG, "a", encoding="utf-8") as f:
                f.write(log_line + "\n")
        except Exception as e:
            print(f"[transfer_log] write file failed: {e}")
        try:
            conn = open_current_database(DB_PATH)
            conn.execute(
                "INSERT INTO transfer_history(ts,method,path,idx,status,bytes,duration_ms,client_ip,user_agent) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    datetime.now().isoformat(),
                    request.method,
                    safe_path,
                    idx_val,
                    resp.status_code,
                    size,
                    dur_ms,
                    request.remote_addr,
                    request.headers.get("User-Agent", ""),
                ),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[transfer_log] db insert failed: {e}")
    return resp


def _write_fetch_sentinel(idx, p: Path) -> None:
    """ESP32 成功拉取后写哨兵，供 inktime_daily.sh 轮询得知“已拉走”。"""
    try:
        size = p.stat().st_size if p.exists() else 0
        LAST_FETCH_SENTINEL.write_text(
            json.dumps(
                {
                    "ts": datetime.now().isoformat(),
                    "ip": request.remote_addr,
                    "idx": idx,
                    "bytes": size,
                    "file": p.name,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"[sentinel] write failed: {e}")


def _require_webui_enabled() -> None:
    if not ENABLE_REVIEW_WEBUI:
        abort(404)


def _safe_join(base: Path, rel: str) -> Path:
    """防目录穿越：只允许 base 下的相对路径"""
    return safe_join(base, rel)


def _send_static_file(p: Path) -> Response:
    if not p.exists() or not p.is_file():
        abort(404)

    if p.suffix.lower() == ".bin":
        return send_file(p, mimetype="application/octet-stream", as_attachment=False)

    mt, _ = mimetypes.guess_type(str(p))
    if mt:
        return send_file(p, mimetype=mt, as_attachment=False)
    return send_file(p, as_attachment=False)


def _to_local_path(path_str: str) -> Path:
    """
    把数据库里的 path（可能是 Windows UNC 路径）转换成本机可访问的 Path。
    依次尝试：
      1. 原样使用（同平台写入的库）
      2. config.PATH_MAP 前缀替换（跨平台迁移场景，与 render_daily_photo 对齐）
      3. 用 IMAGE_DIR 的末级目录名做后缀重连兜底
    找不到则返回原样解析的 Path（交由调用方判定）。
    """
    raw = str(path_str)
    p = Path(raw).expanduser()

    image_dir_resolved = IMAGE_DIR.resolve()

    # 1) 原样命中
    try:
        if p.resolve().relative_to(image_dir_resolved):
            return p
    except Exception:
        pass

    # 2) PATH_MAP 前缀替换
    path_map = getattr(cfg, "PATH_MAP", {}) or {}
    for old_prefix, new_prefix in path_map.items():
        if raw.startswith(old_prefix):
            mapped = Path(raw.replace(old_prefix, new_prefix).replace("\\", "/"))
            try:
                if mapped.resolve().relative_to(image_dir_resolved):
                    return mapped
            except Exception:
                continue

    # 3) base_name 后缀重连兜底
    base_name = image_dir_resolved.name
    norm_raw = raw.replace("\\", "/")
    pattern = f"/{base_name}/"
    if pattern in norm_raw:
        suffix = norm_raw.split(pattern, 1)[1]
        guessed = (image_dir_resolved / suffix).resolve()
        try:
            if guessed.relative_to(image_dir_resolved):
                return guessed
        except Exception:
            pass

    return p


def _to_db_path(path_str: str) -> str:
    """
    把"本机可访问的路径"反推成数据库 photo_scores.path 里实际存储的格式。
    用于 SQL 查询：调用方手里可能是本地路径（如 /sim 从 /images/ 反解出来），
    也可能本来就是 DB 原值；两者都要能命中 DB。
    做法：取相对 IMAGE_DIR 的部分，用 PATH_MAP 的反向前缀（DB->本地）重新拼成 DB 形式。
    若无法映射，返回原值（同平台写入的库可原样匹配）。
    """
    raw = str(path_str)
    try:
        p = Path(raw).expanduser().resolve()
        rel = p.relative_to(IMAGE_DIR.resolve())
        rel_str = str(rel).replace("\\", "/")
    except Exception:
        return raw

    # PATH_MAP: {DB前缀: 本地前缀}，反推时把本地前缀替换成 DB 前缀
    path_map = getattr(cfg, "PATH_MAP", {}) or {}
    for db_prefix, local_prefix in path_map.items():
        local_prefix_norm = local_prefix.replace("\\", "/").rstrip("/")
        db_prefix_norm = db_prefix.replace("\\", "/").rstrip("/")
        # 检查 IMAGE_DIR 是否恰好落在某个 local_prefix 下（本机挂载点 == PATH_MAP 目标）
        image_dir_norm = str(IMAGE_DIR.resolve()).replace("\\", "/").rstrip("/")
        if image_dir_norm == local_prefix_norm or image_dir_norm.startswith(local_prefix_norm + "/"):
            # 把 IMAGE_DIR 部分替换成对应的 DB 前缀，再拼上相对部分
            # 但 DB 路径用的分隔符保留 PATH_MAP key 的风格（通常是反斜杠）
            return db_prefix.rstrip("\\/") + "\\" + rel_str.replace("/", "\\") if "\\" in db_prefix else db_prefix_norm + "/" + rel_str

    # 兜底：直接返回本地路径原值
    return raw


def _make_image_url(path_str: str) -> str:
    """
    把数据库里的本地图片路径转换成 HTTP 可访问的 /images/... 路径。
    要求图片在 IMAGE_DIR 目录下；不在则返回空，避免 file:// 污染与 canvas 跨域。
    """
    try:
        p = _to_local_path(path_str)
        rel = p.resolve().relative_to(IMAGE_DIR.resolve())
        return "/images/" + str(rel).replace("\\", "/")
    except Exception:
        return ""


# --------------------------
# DB helpers
# --------------------------


def load_rows(page: int = 1, page_size: int = REVIEW_PAGE_SIZE, md: str = "", sort: str = "memory", path_inc: str = "", path_exc: str = ""):
    """分页读取 review 数据。支持按 MM-DD 过滤、路径包含/排除过滤与排序。返回 (rows, total_count)."""
    if not DB_PATH.exists():
        raise SystemExit(f"找不到数据库文件: {DB_PATH}")

    if page < 1:
        page = 1
    if page_size < 1:
        page_size = REVIEW_PAGE_SIZE

    offset = (page - 1) * page_size

    conn = open_current_readonly_database(DB_PATH)
    c = conn.cursor()

    # exif_datetime 列与 exif_json.$.datetime 同源（analyze 管线写入时同步），
    # 且已建索引；避免对全表逐行 json_extract。
    # 列存 EXIF 冒号格式（"YYYY:MM:DD HH:MM:SS"），月/日的 substr 位置与旧 JSON 表达式一致。
    dt_expr = "exif_datetime"
    md_expr = f"(substr({dt_expr}, 6, 2) || '-' || substr({dt_expr}, 9, 2))"

    where_clauses = []
    params: list[object] = []

    md = (md or "").strip()
    if md and len(md) == 5 and md[2] == "-":
        where_clauses.append(f"{dt_expr} IS NOT NULL AND {md_expr} = ?")
        params.append(md)

    path_inc = (path_inc or "").strip()
    if path_inc:
        where_clauses.append("path LIKE ?")
        params.append(f"%{path_inc}%")

    path_exc = (path_exc or "").strip()
    if path_exc:
        where_clauses.append("path NOT LIKE ?")
        params.append(f"%{path_exc}%")

    where_sql = ""
    if where_clauses:
        where_sql = "WHERE " + " AND ".join(where_clauses)

    # total_count 也要跟随过滤
    if where_sql:
        total_count = c.execute(f"SELECT COUNT(1) FROM photo_scores {where_sql}", params).fetchone()[0]
    else:
        total_count = c.execute("SELECT COUNT(1) FROM photo_scores").fetchone()[0]

    # 排序
    sort = (sort or "memory").strip()
    if sort == "beauty":
        order_sql = "ORDER BY COALESCE(beauty_score, -1) DESC, COALESCE(memory_score, -1) DESC, path"
    elif sort == "time_new":
        # 直接按 datetime 字符串排序（固定格式下可按字典序比较）；NULL 放最后
        order_sql = f"ORDER BY ({dt_expr} IS NULL) ASC, {dt_expr} DESC, path"
    elif sort == "time_old":
        order_sql = f"ORDER BY ({dt_expr} IS NULL) ASC, {dt_expr} ASC, path"
    else:
        # 默认 memory
        order_sql = "ORDER BY COALESCE(memory_score, -1) DESC, COALESCE(beauty_score, -1) DESC, path"

    base_sql = f"""
        SELECT path,
               caption,
               type,
               memory_score,
               beauty_score,
               reason,
               exif_json,
               width,
               height,
               orientation,
               used_at,
               side_caption
        FROM photo_scores
        {where_sql}
        {order_sql}
        LIMIT ? OFFSET ?
    """

    q_params = list(params) + [page_size, offset]
    rows = c.execute(base_sql, q_params).fetchall()

    conn.close()
    return rows, int(total_count)


# 新增：只加载指定日期集合的照片，加速 /sim
def load_sim_rows_for_dates(dates: list[str]):
    """只加载指定日期（YYYY-MM-DD）集合内的照片，用于 /sim 加速。"""
    if not dates:
        return []
    if not DB_PATH.exists():
        raise SystemExit(f"找不到数据库文件: {DB_PATH}")

    # 过滤掉不合法日期字符串，避免 SQL 注入（虽然我们用参数化，但也别喂垃圾）
    safe_dates = []
    for d in dates:
        d = (d or "").strip()
        if len(d) == 10 and d[4] == "-" and d[7] == "-":
            safe_dates.append(d)
    if not safe_dates:
        return []

    conn = open_current_readonly_database(DB_PATH)
    c = conn.cursor()

    dt_expr = "json_extract(exif_json, '$.datetime')"
    # exif datetime 形如 YYYY:MM:DD HH:MM:SS，取前 10 位并把 : 替换成 - -> YYYY-MM-DD
    date_expr = f"replace(substr({dt_expr}, 1, 10), ':', '-')"

    placeholders = ",".join(["?"] * len(safe_dates))
    sql = f"""
        SELECT path,
               caption,
               type,
               memory_score,
               beauty_score,
               reason,
               side_caption,
               exif_json,
               width,
               height,
               orientation,
               used_at,
               exif_gps_lat,
               exif_gps_lon,
               exif_city,
               subjects_json
        FROM photo_scores
        WHERE {dt_expr} IS NOT NULL
          AND {date_expr} IN ({placeholders})
    """

    rows = c.execute(sql, tuple(safe_dates)).fetchall()
    conn.close()
    return rows

def get_photo_meta_by_path(abs_path: str):
    """
    从 DB 找到渲染需要的字段：date/side/lat/lon/city。
    abs_path 可以是数据库里 photo_scores.path 的原值（UNC 路径等），
    也可以是本机可访问的本地路径（会被 _to_db_path 反推成 DB 格式）。
    """
    if not abs_path:
        return None
    abs_path = unicodedata.normalize('NFC', abs_path)
    if not DB_PATH.exists():
        return None

    # 候选 key：DB 原值 + 本地路径反推的 DB 格式（跨平台场景）
    candidates = [abs_path]
    db_path_guess = _to_db_path(abs_path)
    if db_path_guess and db_path_guess != abs_path:
        candidates.append(db_path_guess)

    conn = open_current_readonly_database(DB_PATH)
    c = conn.cursor()
    row = None
    for key in candidates:
        row = c.execute(
            """
            SELECT path,
                   exif_json,
                   side_caption,
                   memory_score,
                   exif_gps_lat,
                   exif_gps_lon,
                   exif_city,
                   subjects_json
            FROM photo_scores
            WHERE path = ? COLLATE NOCASE
            LIMIT 1
            """,
            (key,),
        ).fetchone()
        if row:
            break
    conn.close()

    if not row:
        return None

    path, exif_json, side_caption, memory_score, gps_lat, gps_lon, exif_city, subjects_json = row
    date_str = extract_date_from_exif(exif_json, str(path)) or ""

    return {
        "path": str(path),
        "date": date_str,
        "side": side_caption or "",
        "memory": float(memory_score) if memory_score is not None else None,
        "lat": gps_lat,
        "lon": gps_lon,
        "city": exif_city or "",
        "subjects_json": subjects_json or "",
    }

def summarize_exif(exif_json: str | None) -> str:
    if not exif_json:
        return ""

    try:
        data = json.loads(exif_json)
    except Exception:
        return ""

    dtv = data.get("datetime")
    make = data.get("make")
    model = data.get("model")
    iso = data.get("iso")
    exp = data.get("exposure_time")
    fnum = data.get("f_number")
    fl = data.get("focal_length")
    lat = data.get("gps_lat")
    lon = data.get("gps_lon")

    parts = []
    if dtv:
        parts.append(f"时间: {dtv}")
    if make or model:
        cam = f"{make or ''} {model or ''}".strip()
        if cam:
            parts.append(f"设备: {cam}")
    exp_parts = []
    if iso:
        exp_parts.append(f"ISO {iso}")
    if exp:
        exp_parts.append(f"快门 {exp}")
    if fnum:
        exp_parts.append(f"光圈 {fnum}")
    if fl:
        exp_parts.append(f"焦距 {fl}")
    if exp_parts:
        parts.append(" / ".join(exp_parts))
    if lat is not None and lon is not None:
        try:
            parts.append(f"GPS: {float(lat):.5f}, {float(lon):.5f}")
        except Exception:
            parts.append(f"GPS: {lat}, {lon}")

    return "；".join(str(p) for p in parts if p)


# --------------------------
# HTML builders
# --------------------------

def build_html(rows, page: int, page_size: int, total_count: int, path_inc: str = "", path_exc: str = ""):
    items = []

    for path, caption, ptype, m_score, b_score, reason, exif_json, width, height, orientation, used_at, side_caption in rows:
        safe_caption = html.escape(caption or "").replace("\n", "<br>")
        safe_side = html.escape(side_caption or "").replace("\n", "<br>")
        safe_type = html.escape(ptype or "")
        safe_reason = html.escape(reason or "")
        exif_summary = summarize_exif(exif_json)
        safe_exif = html.escape(exif_summary or "")

        date_str = extract_date_from_exif(exif_json, str(path))
        safe_date = html.escape(date_str or "")

        md_str = ""
        if date_str and len(date_str) >= 10:
            md_str = date_str[5:10]
        safe_md = html.escape(md_str or "")

        res_str = ""
        if width and height:
            try:
                res_str = f"{int(width)} x {int(height)}"
            except Exception:
                res_str = f"{width} x {height}"
        orient_str = orientation or ""
        used_str = used_at or ""

        img_uri = _make_image_url(str(path))
        if not img_uri:
            continue

        score_line = ""
        if m_score is not None or b_score is not None:
            parts = []
            if m_score is not None:
                parts.append(f"回忆度: {m_score:.1f}")
            if b_score is not None:
                parts.append(f"美观度: {b_score:.1f}")
            score_line = " / ".join(parts)

        extra = []
        if safe_date:
            extra.append(f"拍摄日期: {safe_date}")
        if res_str:
            extra.append(f" · 分辨率: {html.escape(res_str)}")
        if orient_str:
            extra.append(f" · 方向: {html.escape(orient_str)}")
        if used_str:
            extra.append(f" · 已上屏: {html.escape(used_str)}")

        items.append({
            "date": safe_date,
            "md": safe_md,
            "memory_attr": m_score if m_score is not None else "",
            "beauty_attr": b_score if b_score is not None else "",
            "img_uri": img_uri,
            "thumb_uri": "/images_thumb/" + img_uri[len("/images/"):],
            "sim_q": urllib.parse.quote(img_uri),
            "side": safe_side or None,
            "path_esc": html.escape(str(path)),
            "type": safe_type or None,
            "score": score_line,
            "reason": safe_reason or None,
            "exif": safe_exif or None,
            "extra": extra,
            "caption": safe_caption,
        })

    total_pages = (total_count + page_size - 1) // page_size

    # 从请求参数回填（用于显示）
    md_q = (request.args.get("md", "") or "").strip()
    md_hint = f" · 筛选日期 {html.escape(md_q)}" if (md_q and len(md_q) == 5) else ""

    return render_template(
        "review.html",
        items=items,
        db_path=str(DB_PATH),
        md_hint=md_hint,
        page=page,
        item_count=len(rows),
        total_count=total_count,
        page_size=page_size,
        total_pages=total_pages,
        path_inc=path_inc,
        path_exc=path_exc,
    )


def build_simulator_html(sim_rows, selected_img: str = ""):
    # 空数据时不要做任何无意义的循环，避免前端 JS 大对象
    if not sim_rows:
        sim_rows = []
    items = []

    for (
        path,
        caption,
        ptype,
        memory_score,
        beauty_score,
        reason,
        side_caption,
        exif_json,
        width,
        height,
        orientation,
        used_at,
        gps_lat,
        gps_lon,
        exif_city,
        subjects_json,
    ) in sim_rows:
        date_str = extract_date_from_exif(exif_json, str(path)) or ""
        img_uri = _make_image_url(str(path))
        if not img_uri:
            continue

        # tags: 保证为数组，优先解析 JSON/容错
        type_value = parse_dirty_type(ptype)

        items.append({
            "path": img_uri,
            "date": date_str,
            "memory": float(memory_score) if memory_score is not None else None,
            "beauty": float(beauty_score) if beauty_score is not None else None,
            "city": exif_city or "",
            "lat": gps_lat,
            "lon": gps_lon,
            "side": side_caption or "",
            "caption": caption or "",
            "type": type_value,
            "reason": reason or "",
            "exif_json": exif_json or "",
            "exif_summary": summarize_exif(exif_json) if exif_json else "",
            "width": width if width is not None else "",
            "height": height if height is not None else "",
            "orientation": orientation or "",
            "used_at": used_at or "",
            "subjects_json": subjects_json or "",
        })

    data_json = json.dumps(items, ensure_ascii=False).replace("</", "<\\/") if items else "[]"
    selected_json = json.dumps(selected_img or "", ensure_ascii=False).replace("</", "<\\/")
    memory_threshold = float(getattr(cfg, "MEMORY_THRESHOLD", 70.0) or 70.0)

    return render_template("simulator.html", data_json=data_json, selected_json=selected_json, memory_threshold=memory_threshold)


# --------------------------
# Routes
# --------------------------

@app.get("/")
def index():
    """统一入口首页：分发照片库 / 选片推送 / 相册管理 / 版式编辑器 / 设备日志等子页。"""
    holiday_name = ""
    try:
        from holidays import get_today_holiday
        h = get_today_holiday(datetime.now().date())
        if h:
            holiday_name = str(h.get("name") or "")
    except Exception:
        holiday_name = ""
    return render_template(
        "home.html",
        today=datetime.now().strftime("%Y-%m-%d"),
        holiday=holiday_name,
        db_path=str(DB_PATH),
        image_dir=str(IMAGE_DIR),
        quantity=DAILY_PHOTO_QUANTITY,
        review_enabled=ENABLE_REVIEW_WEBUI,
    )


@app.get("/review")
def review():
    _require_webui_enabled()
    try:
        page = int(request.args.get("page", "1"))
    except Exception:
        page = 1

    md = (request.args.get('md', '') or '').strip()
    sort = (request.args.get('sort', '') or 'memory').strip() or 'memory'
    path_inc = (request.args.get('path_inc', '') or '').strip()
    path_exc = (request.args.get('path_exc', '') or '').strip()

    rows, total_count = load_rows(page=page, page_size=REVIEW_PAGE_SIZE, md=md, sort=sort, path_inc=path_inc, path_exc=path_exc)
    if not rows:
        return Response(
            "数据库里没有可展示的数据。请先运行你的分析脚本生成评分与文案。",
            status=404,
            mimetype="text/plain; charset=utf-8",
        )

    html_str = build_html(rows, page=page, page_size=REVIEW_PAGE_SIZE, total_count=total_count, path_inc=path_inc, path_exc=path_exc)
    return Response(html_str, mimetype="text/html; charset=utf-8")


# API endpoint for md list
@app.get('/api/md_list')
def api_md_list():
    _require_webui_enabled()
    md_list = _load_all_md_list()
    return Response(json.dumps(md_list, ensure_ascii=False), mimetype='application/json; charset=utf-8')


@app.get("/sim")
def sim():
    _require_webui_enabled()
    selected_img = request.args.get("img", "")

    # 默认不再全库加载，避免 /sim 页面巨大 JSON 导致浏览器转圈
    sim_rows = []

    # 仅当从 /review 点进来且参数合法时，按“该日期 + 向前 30 天”加载
    if selected_img and isinstance(selected_img, str) and selected_img.startswith("/images/"):
        subpath = selected_img[len("/images/"):]
        try:
            p = _safe_join(IMAGE_DIR, subpath)
        except Exception:
            p = None

        if p is not None and p.exists() and p.is_file():
            meta = get_photo_meta_by_path(str(p))
            base_date = meta.get("date") if meta else ""

            if base_date:
                try:
                    from datetime import datetime, timedelta
                    dt0 = datetime.strptime(base_date, "%Y-%m-%d")
                    dates = [(dt0 - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(0, 31)]
                except Exception:
                    dates = [base_date]

                sim_rows = load_sim_rows_for_dates(dates)

            # 关键：无论 base_date 是否由正则产生，SQL 可能都查不到它（如果 JSON 里的 datetime 是空的）
            # 所以我们必须确保 selected_img 对应的这行数据一定在 sim_rows 里。
            # 注意 p 是本地路径，DB 里存的可能是 UNC 路径，比较时要用 DB 格式归一化。
            db_p = _to_db_path(str(p))
            if not any(_to_db_path(str(r[0])).lower() == db_p.lower() for r in sim_rows):
                conn = open_current_readonly_database(DB_PATH)
                c = conn.cursor()
                row = c.execute("""
                    SELECT path, caption, type, memory_score, beauty_score,
                           reason, side_caption, exif_json, width, height,
                           orientation, used_at, exif_gps_lat, exif_gps_lon, exif_city, subjects_json
                    FROM photo_scores
                    WHERE path = ? COLLATE NOCASE
                    LIMIT 1
                """, (db_p,)).fetchone()
                conn.close()
                if row:
                    sim_rows.insert(0, row)

    now = time.time()
    hit = _SIM_HTML_CACHE.get(selected_img)
    if hit and now - hit[0] < _SIM_HTML_TTL:
        return Response(hit[1], mimetype="text/html; charset=utf-8")

    html_str = build_simulator_html(sim_rows, selected_img=selected_img)
    _SIM_HTML_CACHE[selected_img] = (now, html_str)
    if len(_SIM_HTML_CACHE) > _SIM_HTML_CACHE_MAX:
        for k in sorted(_SIM_HTML_CACHE, key=lambda k: _SIM_HTML_CACHE[k][0])[:-_SIM_HTML_CACHE_MAX]:
            _SIM_HTML_CACHE.pop(k, None)
    return Response(html_str, mimetype="text/html; charset=utf-8")


@app.get("/images/<path:subpath>")
def images(subpath: str):
    _require_webui_enabled()
    try:
        p = _safe_join(IMAGE_DIR, subpath)
    except Exception:
        abort(400)
    return _send_static_file(p)

@app.get("/images_thumb/<path:subpath>")
def images_thumb(subpath: str):
    _require_webui_enabled()
    try:
        p = _safe_join(IMAGE_DIR, subpath)
    except Exception:
        abort(400)
    if not p.exists() or not p.is_file():
        abort(404)

    try:
        cache_path = _thumb_cache_path(f"{p}|{p.stat().st_mtime_ns}")
    except OSError:
        abort(404)

    if cache_path.exists() and cache_path.stat().st_size > 0:
        return send_file(cache_path, mimetype="image/jpeg", as_attachment=False)

    try:
        from PIL import Image, ImageOps
        with Image.open(p) as im:
            im = ImageOps.exif_transpose(im)
            im.thumbnail((THUMB_MAX_EDGE, THUMB_MAX_EDGE))
            if im.mode != "RGB":
                im = im.convert("RGB")
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache_path.with_name(cache_path.name + ".tmp")
            im.save(tmp, "JPEG", quality=80)
            tmp.replace(cache_path)  # 原子替换，避免并发请求读到半张图
        _prune_thumb_cache()
        return send_file(cache_path, mimetype="image/jpeg", as_attachment=False)
    except Exception:
        # 缩略图生成失败（NAS 抖动/文件损坏）时回退原图，功能不回退
        return _send_static_file(p)

@app.get("/sim_render")
def sim_render():
    _require_webui_enabled()

    img_uri = request.args.get("img", "")
    if not img_uri or not img_uri.startswith("/images/"):
        abort(400)

    subpath = img_uri[len("/images/"):]
    try:
        p = _safe_join(IMAGE_DIR, subpath)
    except Exception:
        abort(400)

    if not p.exists() or not p.is_file():
        abort(404)

    meta = get_photo_meta_by_path(str(p))
    if meta is None:
        # 兜底：DB 没命中就渲染纯图（不建议长期这样）
        meta = {
            "path": str(p),
            "date": "",
            "side": "",
            "memory": None,
            "lat": None,
            "lon": None,
            "city": "",
            "subjects_json": "",
        }

    # 朝向覆盖：auto / portrait / landscape，默认 auto（按照片宽高比自动判定）
    orient = (request.args.get("orient", "auto") or "auto").strip().lower()
    if orient not in ("auto", "portrait", "landscape"):
        orient = "auto"

    # 抖动算法：floyd_steinberg / atkinson / bayer / none，默认 floyd_steinberg
    dither = (request.args.get("dither", "floyd_steinberg") or "floyd_steinberg").strip().lower()
    if dither not in ("floyd_steinberg", "atkinson", "bayer", "none"):
        dither = "floyd_steinberg"

    try:
        # 节假日命中结果计入缓存键：节日层随日期变化，避免跨天串缓存
        from holidays import get_today_holiday
        holiday = get_today_holiday(datetime.now().date())
        cache_path = _sim_render_cache_path(
            f"{p}|{p.stat().st_mtime_ns}|{orient}|{dither}|{holiday['name'] if holiday else ''}")
        if cache_path.exists():
            return send_file(cache_path, mimetype="image/png", as_attachment=False)

        img = rdp.render_image(meta, force_orientation=orient, holiday=holiday)
        img_dithered = rdp.apply_dither(img, dither)

        bio = BytesIO()
        img_dithered.save(bio, format="PNG")
        try:
            SIM_RENDER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            tmp_path = cache_path.with_suffix(".tmp")
            tmp_path.write_bytes(bio.getvalue())
            tmp_path.replace(cache_path)
            _prune_sim_render_cache()
        except Exception:
            pass  # 缓存写失败不影响渲染结果
        bio.seek(0)
        return send_file(bio, mimetype="image/png", as_attachment=False)
    except Exception:
        abort(500)

@app.get("/static/inktime/<key>/photo_<int:idx>.bin")
def esp_photo(key: str, idx: int):
    if not authorized_download_key(key, DOWNLOAD_KEYS):
        abort(404)
    if idx < 0 or idx >= DAILY_PHOTO_QUANTITY:
        abort(404)
    
    # 被实际拉取/推送时，标记为已展示
    path_txt = BIN_OUTPUT_DIR / f"photo_{idx}.path.txt"
    if path_txt.exists():
        try:
            target_path = path_txt.read_text(encoding="utf-8").strip()
            if target_path:
                rdp.mark_photo_used(target_path)
        except Exception as e:
            print(f"Failed to mark photo used: {e}")

    p = BIN_OUTPUT_DIR / f"photo_{idx}.bin"
    _write_fetch_sentinel(idx, p)
    return _send_static_file(p)


@app.get("/static/inktime/<key>/latest.bin")
def esp_latest(key: str):
    if not authorized_download_key(key, DOWNLOAD_KEYS):
        abort(404)

    # 被实际拉取/推送时，标记为已展示
    path_txt = BIN_OUTPUT_DIR / "latest.path.txt"
    if path_txt.exists():
        try:
            target_path = path_txt.read_text(encoding="utf-8").strip()
            if target_path:
                rdp.mark_photo_used(target_path)
        except Exception as e:
            print(f"Failed to mark photo used: {e}")

    p = BIN_OUTPUT_DIR / "latest.bin"
    _write_fetch_sentinel(-1, p)
    return _send_static_file(p)


# ── 相框旋转：sidecar json（朝向）与标定卡 ───────────────────
# 注意：只有 .bin 的 fetch 调 mark_photo_used；json / calib 不触发，
# 以免单纯取朝向就把照片误标为"已展示"。

@app.get("/static/inktime/<key>/photo_<int:idx>.json")
def esp_photo_sidecar(key: str, idx: int):
    """返回 photo_<idx>.bin 对应的朝向 sidecar（orientation/w/h）。不标已用。"""
    if not authorized_download_key(key, DOWNLOAD_KEYS):
        abort(404)
    if idx < 0 or idx >= DAILY_PHOTO_QUANTITY:
        abort(404)
    p = BIN_OUTPUT_DIR / f"photo_{idx}.json"
    return _send_static_file(p)


@app.get("/static/inktime/<key>/latest.json")
def esp_latest_sidecar(key: str):
    """返回 latest.bin 对应的朝向 sidecar。不标已用。"""
    if not authorized_download_key(key, DOWNLOAD_KEYS):
        abort(404)
    p = BIN_OUTPUT_DIR / "latest.json"
    return _send_static_file(p)


@app.get("/static/inktime/<key>/calib_p.bin")
def esp_calib_portrait(key: str):
    """竖屏标定卡（480×800），由 render_calib_cards.py 预生成。"""
    if not authorized_download_key(key, DOWNLOAD_KEYS):
        abort(404)
    p = BIN_OUTPUT_DIR / "calib_p.bin"
    return _send_static_file(p)


@app.get("/static/inktime/<key>/calib_l.bin")
def esp_calib_landscape(key: str):
    """横屏标定卡（800×480），由 render_calib_cards.py 预生成。"""
    if not authorized_download_key(key, DOWNLOAD_KEYS):
        abort(404)
    p = BIN_OUTPUT_DIR / "calib_l.bin"
    return _send_static_file(p)


@app.get("/static/inktime/<key>/preview.png")
def esp_preview(key: str):
    if not authorized_download_key(key, DOWNLOAD_KEYS):
        abort(404)
    p = BIN_OUTPUT_DIR / "preview.png"
    return _send_static_file(p)


@app.get("/static/inktime/<key>/time")
def esp_time(key: str):
    """服务器授时：返回当前 Unix epoch（纯数字字符串）。

    供 ESP32 在公网 NTP 失败时回退使用——设备每天都要连本 server 拉照片，
    顺带从这里拿准确时间，避免 NTP 失败导致 sleepUntilNextSchedule 算错下次唤醒。
    复用与其他 ESP32 接口相同的 <key> 鉴权。
    """
    if not authorized_download_key(key, DOWNLOAD_KEYS):
        abort(404)
    return str(int(time.time()))


# Legacy /shutdown endpoint removed: DOWNLOAD_KEY cannot grant process termination.


# ── 设备日志（黑盒）上传 ─────────────────────────────────────
# ESP32 在每次入睡前把增量事件（JSONL，一行一条）POST 上来；收到 200 才推进
# 设备侧上传游标。设计见 docs/adr/0001-esp32-flash-device-journal.md。
# 新日志使用 logs/device/device-<sha256(key)>.log，历史明文键名文件仅只读兼容。
# 每行 = 设备原始 JSON 事件补上接收时刻与来源 IP。

DEVICE_LOG_DIR = LOG_DIR / "device"
DEVICE_LOG_MAX_BYTES = 5 * 1024 * 1024   # >5MB 轮转为 .log.1（保留一代）
_devlog_max_ls: dict[str, int] = {}      # 仅诊断缓存；每次请求必须在跨进程锁内从磁盘重读


def _devlog_path(key: str) -> Path:
    """Never create a log filename containing the bearer download key."""
    return device_log_paths(DEVICE_LOG_DIR, key)[1]


def _devlog_load_max_ls(key: str) -> int:
    """Recover the highest ls from legacy and opaque-name logs after restart.

    Old plaintext-key filenames are read-only: they are never renamed,
    overwritten or newly created by the upload handler.
    """
    max_ls = 0
    for p in device_log_paths(DEVICE_LOG_DIR, key):
        # Include the previous generation: a crash between rename and first
        # append must not reset the accepted upload cursor.
        for candidate in (p.with_suffix(".log.1"), p):
            lines = read_device_log_lines(candidate)
            if lines is None:
                continue
            for ln in lines[-500:]:
                try:
                    ls = int(json.loads(ln).get("ls", 0))
                except (ValueError, TypeError, AttributeError):
                    continue
                if ls > max_ls:
                    max_ls = ls
    return max_ls


@app.post("/api/device_log/<key>")
def esp_device_log(key: str):
    if not authorized_download_key(key, DOWNLOAD_KEYS):
        abort(404)

    raw = request.get_data(as_text=True)
    if not raw:
        return "empty", 400

    recv_iso = datetime.now().isoformat(timespec="seconds")
    ip = request.remote_addr or ""
    try:
        # Lock covers cursor recovery, deduplication and append/rotation.
        # A process-local cursor cannot be trusted across server workers.
        with device_log_lock(_devlog_path(key)):
            try:
                max_ls = _devlog_load_max_ls(key)
            except OSError:
                print("[device_log] historical journal read failed")
                return "journal unavailable", 500

            out_lines: list[str] = []
            for ln in raw.splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    ev = json.loads(ln)
                    ls = int(ev.get("ls", 0))
                    if not ls or ls <= max_ls:
                        continue
                    max_ls = ls
                except Exception:
                    continue
                ev["rcv"] = recv_iso
                ev["ip"] = ip
                out_lines.append(json.dumps(ev, ensure_ascii=False, separators=(",", ":")))

            if out_lines:
                try:
                    append_private_device_log(
                        _devlog_path(key), out_lines, max_bytes=DEVICE_LOG_MAX_BYTES
                    )
                except (OSError, ValueError) as exc:
                    print(f"[device_log] write failed: {type(exc).__name__}")
                    return "write failed", 500
            _devlog_max_ls[key] = max_ls  # diagnostics only; never authoritative
            return f"OK n={len(out_lines)}", 200
    except (OSError, ValueError) as exc:
        print(f"[device_log] journal lock failed: {type(exc).__name__}")
        return "journal unavailable", 500


# ── 常开设备注册（LAN 推送目标）─────────────────────────────
# ESP32 固件在 debug 页开启「常开模式」后不再休眠，上线时 POST 本接口
# 登记自身（IP 取连接源地址，端口在 body 里）。选片推送勾选「LAN 刷新」
# 时读这里的记录，HTTP 回调设备 /fetch?idx=0 触发拉取渲染。
# 每次进入常开、以及常开期间每日刷新后设备都会重登，DHCP 换 IP 可自愈。

DEVICE_ENDPOINT_FILE = DEVICE_LOG_DIR / "endpoint.json"


def _load_device_endpoint() -> dict:
    """Reject malformed or legacy unsafe endpoints before any HTTP callback."""
    try:
        rec = json.loads(DEVICE_ENDPOINT_FILE.read_text(encoding="utf-8"))
        if not isinstance(rec, dict):
            return {}
        ip, port = validate_device_endpoint(rec.get("ip"), rec.get("port", 80))
        return {**rec, "ip": ip, "port": port}
    except (OSError, ValueError, TypeError):
        return {}


@app.post("/api/device_register/<key>")
def esp_device_register(key: str):
    if not authorized_download_key(key, DOWNLOAD_KEYS):
        abort(404)

    # Use the direct peer address, never a client-supplied Forwarded/XFF header.
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        return "invalid registration", 400
    try:
        ip, port = validate_device_endpoint(request.remote_addr, body.get("port", 80))
    except ValueError:
        return "invalid LAN endpoint", 400
    rec = {
        "ip": ip,
        "port": port,
        "alwayson": bool(body.get("alwayson")),
        "ts": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        DEVICE_LOG_DIR.mkdir(parents=True, exist_ok=True)
        DEVICE_ENDPOINT_FILE.write_text(
            json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"[device_register] write failed: {e}")
        return "write failed", 500
    print(f"[InkTime] device register: {rec['ip']}:{rec['port']} alwayson={rec['alwayson']}")
    return "OK", 200


# ── 选片推送（/select）───────────────────────────────────────
# 展示"今日选中"（output/ 里当日管线渲染的批次）与"候选池"（used_md 当日
# memory>阈值、连拍去重后按 final_score 排序的照片），支持勾选若干张手动
# 重渲染落盘（photo_*.bin/json/path.txt + latest.*），可选触发 BLE 立即刷新；
# ESP32 设备下次唤醒即拉取新批次。渲染复用 render_daily_photo.render_photo_to_bin，
# 与每日管线产物完全一致。

import subprocess
import sys

import requests


def _device_request(method: str, url: str, *, timeout: float,
                    data: dict | None = None):
    """Never follow redirects or environment HTTP proxies for device callbacks."""
    with requests.Session() as session:
        session.trust_env = False
        return session.request(method, url, timeout=timeout, data=data,
                               allow_redirects=False)


SELECT_CANDIDATE_LIMIT = 120        # 候选池最多返回给前端的张数（按 final_score 截断）
_SELECT_CACHE: dict[str, object] = {"built_at": 0.0, "payload": None}
_SELECT_CACHE_TTL_SEC = 600.0       # load_sim_rows 全库扫描较慢（~秒级），10 分钟内复用
_SELECT_CACHE_LOCK = threading.Lock()

_BLE_PUSH_TIMEOUT_SEC = 300         # 与 inktime_daily.sh 的 PUSH_TIMEOUT 同量级，留足余量
_LAN_PUSH_TIMEOUT_SEC = 180         # 设备拉取+渲染一轮约 10-40s，留足余量
_PUSH_JOB: dict[str, object] = {
    "state": "idle",                # idle | running | done | error
    "total": 0,
    "done": 0,
    "ble": False,
    "lan": False,
    "log": [],
    "photos": [],
    "error": "",
    "started": "",
    "finished": "",
}
_PUSH_JOB_LOCK = threading.Lock()


def _push_log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(f"[select-push] {msg}")
    with _PUSH_JOB_LOCK:
        log = _PUSH_JOB.setdefault("log", [])
        log.append(line)
        if len(log) > 400:
            del log[:-400]


def _select_item_view(p: dict) -> dict | None:
    """把 load_sim_rows 形状的候选照片序列化为选片页卡片数据（含得分明细）。"""
    img_uri = _make_image_url(str(p["path"]))
    if not img_uri:
        return None

    fs = p.get("_final_score")
    if not fs:
        fs = rdp.compute_final_score(p)
        p["_final_score"] = fs
    pb = rdp.compute_portrait_boost(p.get("type_raw", ""))
    memory = p.get("memory")
    beauty = p.get("beauty")

    return {
        "path": str(p["path"]),
        "image_url": img_uri,
        "thumb_url": "/images_thumb/" + img_uri[len("/images/"):],
        "date": p.get("date") or "",
        "memory": round(float(memory), 1) if memory is not None else None,
        "beauty": round(float(beauty), 1) if beauty is not None else None,
        "final": round(float(fs), 2),
        "display": round(float(fs) * pb, 2),
        # final_score 的加权成分，与每日管线 [SELECT] 日志同口径
        "weights": {
            "path": round(rdp.compute_path_weight(p["path"]), 2),
            "category": round(rdp.compute_category_weight(p.get("type_raw", "")), 2),
            "recency": round(rdp.compute_recency_penalty(p.get("used_at")), 2),
            "crop": round(rdp.compute_crop_penalty(p.get("subjects_json", "")), 2),
            "portrait": round(pb, 2),
        },
        "type_raw": p.get("type_raw") or "",
        "types": parse_dirty_type(p.get("type_raw")),
        "city": p.get("city") or "",
        "caption": p.get("caption") or "",
        "side": p.get("side") or "",
        "reason": p.get("reason") or "",
        "used_at": p.get("used_at") or "",
        "width": p.get("width"),
        "height": p.get("height"),
        "orientation_raw": p.get("orientation_raw") or "",
        "exif_summary": summarize_exif(p.get("exif_json")) if p.get("exif_json") else "",
    }


def _current_batch_view() -> dict:
    """读取 output/ 里当前渲染批次（今日选中的落盘结果）。"""
    photos = []
    bin0 = BIN_OUTPUT_DIR / "photo_0.bin"
    for idx in range(DAILY_PHOTO_QUANTITY):
        bin_p = BIN_OUTPUT_DIR / f"photo_{idx}.bin"
        if not bin_p.exists():
            continue

        db_path = ""
        pt = BIN_OUTPUT_DIR / f"photo_{idx}.path.txt"
        if pt.exists():
            db_path = pt.read_text(encoding="utf-8").strip()

        sidecar: dict = {}
        sj = BIN_OUTPUT_DIR / f"photo_{idx}.json"
        if sj.exists():
            try:
                sidecar = json.loads(sj.read_text(encoding="utf-8"))
            except Exception:
                sidecar = {}

        img_uri = _make_image_url(db_path) if db_path else ""
        preview_p = BIN_OUTPUT_DIR / f"preview_{idx}.png"
        photos.append({
            "idx": idx,
            "db_path": db_path,
            "image_url": img_uri,
            "thumb_url": ("/images_thumb/" + img_uri[len("/images/"):]) if img_uri else "",
            "preview_url": f"/files/preview_{idx}.png" if preview_p.exists() else "",
            "orientation": sidecar.get("orientation", ""),
            "layout": sidecar.get("layout", ""),
            "holiday": sidecar.get("holiday", ""),
            "item": dict(sidecar.get("item") or {}),
            "generated": datetime.fromtimestamp(bin_p.stat().st_mtime).strftime("%m-%d %H:%M"),
        })

    generated = ""
    is_today = False
    if bin0.exists():
        mtime = datetime.fromtimestamp(bin0.stat().st_mtime)
        generated = mtime.isoformat(timespec="minutes")
        is_today = mtime.date() == datetime.now().date()
    return {"generated": generated, "is_today": is_today, "photos": photos}


def _compute_selection_payload() -> dict:
    """跑一次与每日管线同口径的选片：chosen 信息 + 候选池 + 当前批次。

    候选池口径与 choose_photos_for_today 内部一致：
    同一 used_md 月日内 memory>阈值 → 算 final_score → 连拍去重 → 按分排序。
    """
    today = datetime.now().date()
    items = rdp.load_sim_rows()
    photos, info = rdp.choose_photos_for_today(items, today, count=DAILY_PHOTO_QUANTITY)

    threshold = float(rdp.MEMORY_THRESHOLD)
    if info.get("fallback_global_max"):
        # 兜底模式：全库按 final 排序即候选（choose_photos_for_today 已给全部 item 打分）
        pool = sorted(items, key=lambda x: x.get("_final_score") or 0.0, reverse=True)
    else:
        by_md: dict[str, list] = {}
        for it in items:
            by_md.setdefault(it["md"], []).append(it)
        pool = [p for p in by_md.get(str(info.get("used_md") or ""), [])
                if p.get("memory", -1.0) > threshold]
        for p in pool:
            p["_final_score"] = rdp.compute_final_score(p)
        pool.sort(key=lambda x: x["_final_score"], reverse=True)
        if rdp.BURST_DEDUP_WINDOW_SEC > 0:
            pool = rdp.dedup_bursts(pool, rdp.BURST_DEDUP_WINDOW_SEC)

    candidates = []
    for p in pool:
        v = _select_item_view(p)
        if v:
            candidates.append(v)

    holiday = None
    try:
        from holidays import get_today_holiday
        h = get_today_holiday(today)
        if h:
            holiday = {"name": str(h.get("name") or ""), "template": str(h.get("template") or "")}
    except Exception:
        holiday = None

    return {
        "today": today.isoformat(),
        "quantity": DAILY_PHOTO_QUANTITY,
        "threshold": threshold,
        "high_score_threshold": float(rdp.HIGH_SCORE_THRESHOLD),
        "holiday": holiday,
        "info": info,
        "current": _current_batch_view(),
        "candidates": candidates[:SELECT_CANDIDATE_LIMIT],
        "candidates_total": len(candidates),
        "candidates_truncated": len(candidates) > SELECT_CANDIDATE_LIMIT,
        "built_at": datetime.now().isoformat(timespec="seconds"),
    }


def _get_selection_payload(force_refresh: bool = False) -> dict:
    now = time.time()
    with _SELECT_CACHE_LOCK:
        if not force_refresh:
            built_at = float(_SELECT_CACHE.get("built_at") or 0.0)
            payload = _SELECT_CACHE.get("payload")
            if payload and (now - built_at) < _SELECT_CACHE_TTL_SEC:
                return payload
    payload = _compute_selection_payload()
    with _SELECT_CACHE_LOCK:
        _SELECT_CACHE["built_at"] = time.time()
        _SELECT_CACHE["payload"] = payload
    return payload


@app.get("/select")
def select_page():
    return render_template(
        "select.html",
        quantity=DAILY_PHOTO_QUANTITY,
        ble_default=bool(getattr(cfg, "ENABLE_BLE_PUSH", False)),
    )


@app.get("/api/select/overview")
def api_select_overview():
    force = (request.args.get("refresh", "") or "").strip() in ("1", "true")
    try:
        payload = _get_selection_payload(force_refresh=force)
    except Exception as e:
        return jsonify({"error": f"选片计算失败: {e}"}), 500
    return jsonify(payload)


@app.post("/api/select/push")
def api_select_push():
    body = request.get_json(force=True, silent=True) or {}
    raw_paths = body.get("paths") or []
    if not isinstance(raw_paths, list) or not raw_paths:
        return jsonify({"error": "paths 不能为空"}), 400

    # 去重、保序、限量（ESP32 拉取接口只认 0..DAILY_PHOTO_QUANTITY-1 的槽位）
    seen: set[str] = set()
    paths: list[str] = []
    for p in raw_paths:
        s = unicodedata.normalize("NFC", str(p or "").strip())
        if s and s not in seen:
            seen.add(s)
            paths.append(s)
    if not paths:
        return jsonify({"error": "paths 全为空"}), 400
    if len(paths) > DAILY_PHOTO_QUANTITY:
        return jsonify({"error": f"最多选择 {DAILY_PHOTO_QUANTITY} 张（相框槽位上限）"}), 400

    ble = bool(body.get("ble"))
    lan = bool(body.get("lan"))

    with _PUSH_JOB_LOCK:
        if _PUSH_JOB.get("state") == "running":
            return jsonify({"error": "已有推送任务在进行中，请稍候"}), 409
        _PUSH_JOB.update({
            "state": "running",
            "total": len(paths),
            "done": 0,
            "ble": ble,
            "lan": lan,
            "log": [],
            "photos": [],
            "error": "",
            "started": datetime.now().isoformat(timespec="seconds"),
            "finished": "",
        })

    threading.Thread(target=_run_push_job, args=(paths, ble, lan), daemon=True).start()
    return jsonify({"ok": True, "total": len(paths), "ble": ble, "lan": lan})


def _lan_push_now() -> None:
    """向已注册的常开设备推送：HTTP 回调设备 /fetch?idx=0 触发拉取渲染。

    设备 IP/端口来自 /api/device_register 的最近一次注册（常开模式下设备
    上线及每次每日刷新后都会重登）。设备返回的拉取日志尾部回显到任务日志。
    """
    base = _device_base_url()
    if not base:
        _push_log("[LAN] 未找到有效的局域网设备地址（需设备重新注册）")
        return

    url = f"{base}/fetch?idx=0"
    _push_log(f"[LAN] GET {url} （设备拉取+渲染约 10-40 秒，超时 {_LAN_PUSH_TIMEOUT_SEC}s）...")
    try:
        resp = _device_request("GET", url, timeout=_LAN_PUSH_TIMEOUT_SEC)
    except requests.RequestException as e:
        _push_log(f"[LAN] 推送失败：{e.__class__.__name__}: {e}")
        return
    if resp.status_code != 200:
        _push_log(f"[LAN] 推送失败 HTTP {resp.status_code}")
        return
    tail = [ln for ln in resp.text.splitlines() if ln.strip()][-15:]
    for ln in tail:
        _push_log(f"[LAN] {ln}")
    _push_log("[LAN] 推送成功 ✅")


def _run_push_job(paths: list[str], ble: bool, lan: bool = False) -> None:
    try:
        items = rdp.load_sim_rows()
        lookup = {unicodedata.normalize("NFC", it["path"]): it for it in items}

        resolved = []
        for db_path in paths:
            it = lookup.get(db_path)
            if it is None:
                it = lookup.get(unicodedata.normalize("NFC", _to_db_path(db_path)))
            if it is None:
                _push_log(f"[SKIP] 数据库中找不到，已跳过: {db_path}")
                continue
            resolved.append(it)
        if not resolved:
            raise RuntimeError("所选照片均未在数据库命中，未渲染任何内容")

        holiday = None
        from holidays import get_today_holiday
        holiday = get_today_holiday(datetime.now().date())
        if holiday:
            _push_log(f"[HOLIDAY] 命中{holiday['name']}，渲染将叠加节日层")

        for idx, it in enumerate(resolved):
            _push_log(f"[RENDER] 第 {idx + 1}/{len(resolved)} 张: {Path(str(it['path'])).name}")
            rdp.render_photo_to_bin(it, idx, holiday, log=_push_log)
            with _PUSH_JOB_LOCK:
                _PUSH_JOB["done"] = idx + 1
                _PUSH_JOB["photos"].append({"idx": idx, "db_path": it["path"]})

        # 手动选择整体替换当日批次：清掉多余槽位的旧产物，避免 ESP32 拉到残留旧照片
        for idx in range(len(resolved), DAILY_PHOTO_QUANTITY):
            removed = []
            for suffix in ("bin", "json", "path.txt"):
                p = BIN_OUTPUT_DIR / f"photo_{idx}.{suffix}"
                if p.exists():
                    p.unlink()
                    removed.append(p.name)
            pv = BIN_OUTPUT_DIR / f"preview_{idx}.png"
            if pv.exists():
                pv.unlink()
                removed.append(pv.name)
            if removed:
                _push_log(f"[CLEAN] 移除槽位 {idx} 的旧批次残留: {', '.join(removed)}")

        rdp.sync_latest_from_photo0(log=_push_log)

        if lan:
            _lan_push_now()

        if ble:
            _push_log(f"[BLE] 调用 push_to_epd_ble.py --idx 0（超时 {BLE_PUSH_TIMEOUT_SEC}s）...")
            try:
                proc = subprocess.run(
                    [sys.executable, "push_to_epd_ble.py", "--idx", "0"],
                    cwd=str(ROOT_DIR), capture_output=True, text=True,
                    timeout=BLE_PUSH_TIMEOUT_SEC,
                )
                tail = [ln for ln in ((proc.stdout or "") + (proc.stderr or "")).splitlines() if ln.strip()][-10:]
                for ln in tail:
                    _push_log(f"[BLE] {ln}")
                if proc.returncode == 0:
                    _push_log("[BLE] 推送成功 ✅")
                else:
                    _push_log(f"[BLE] 推送失败（exit={proc.returncode}），BIN 已就绪，设备下次唤醒仍会拉取")
            except subprocess.TimeoutExpired:
                _push_log("[BLE] 推送超时 ⚠️，BIN 已就绪，设备下次唤醒仍会拉取")

        with _PUSH_JOB_LOCK:
            _PUSH_JOB["state"] = "done"
        _push_log(f"[DONE] 已落盘 {len(resolved)} 张，latest.* 指向 photo_0")
    except Exception as e:
        with _PUSH_JOB_LOCK:
            _PUSH_JOB["state"] = "error"
            _PUSH_JOB["error"] = str(e)
        _push_log(f"[ERROR] {e}")
    finally:
        with _PUSH_JOB_LOCK:
            _PUSH_JOB["finished"] = datetime.now().isoformat(timespec="seconds")


@app.get("/api/select/push_status")
def api_select_push_status():
    with _PUSH_JOB_LOCK:
        return jsonify(dict(_PUSH_JOB))


# ── 相框设备控制台（/device）────────────────────────────────
# 把 ESP32 唤醒窗口 / 常开模式自带 WebUI 的能力（配置、拉取、测试、debug/
# 常开开关）代理进统一控制台：页面在本端渲染（控制台风格），操作经
# /api/device/* 转发到设备。设备地址来自 /api/device_register 的最近注册
# （设备手动唤醒窗口与常开模式上线时都会注册）；设备休眠后代理会报离线。

_DEVICE_PING_TIMEOUT_SEC = 4      # ping/status 类快速探测
_DEVICE_SCAN_TIMEOUT_SEC = 20     # 设备现场扫描约 2-4s，留余量
_DEVICE_RENDER_TIMEOUT_SEC = 150  # 拉取/渲染/标定测试一轮 10-40s，留足余量


def _device_base_url() -> str | None:
    ep = _load_device_endpoint()  # already validated; no arbitrary URL parts
    if not ep:
        return None
    return f"http://{ep['ip']}:{ep['port']}"


def _device_http(method: str, path: str, timeout: float,
                 data: dict | None = None) -> tuple[int, str]:
    """代理请求设备，返回 (http_code, 响应文本)；网络失败返回 (503, 原因)。"""
    base = _device_base_url()
    if not base:
        return 503, "设备未注册（需唤醒设备或开启常开模式，设备上线后会自动注册）"
    try:
        resp = _device_request(method, base + path, timeout=timeout, data=data)
    except requests.RequestException as e:
        return 503, f"设备请求失败：{e.__class__.__name__}: {e}"
    resp.encoding = "utf-8"   # ESP32 输出恒为 UTF-8；防 Content-Type 未带 charset 时被按 latin-1 解码
    return resp.status_code, resp.text


def _device_err_resp(code: int, text: str):
    """设备代理失败的统一 JSON 错误：503=不可达，其余按 502 上游异常。"""
    return jsonify({"error": text or f"设备 HTTP {code}"}), (503 if code == 503 else 502)


def _html_log_to_text(html_text: str) -> str:
    """设备部分接口返回 <br> 分隔的 HTML 日志，清洗成纯文本供控制台展示。"""
    txt = re.sub(r"<br\s*/?>", "\n", html_text, flags=re.I)
    txt = re.sub(r"<[^>]+>", "", txt)
    return html.unescape(txt).strip()


@app.get("/device")
def device_console_page():
    return render_template("device.html", quantity=DAILY_PHOTO_QUANTITY)


@app.get("/api/device/home_status")
def api_device_home_status():
    """主页设备卡片的轻量探测：单次 /ping（不拉完整 /api/status）。

    /ping 文本自带 ip/debug/alwayson 字段，这里解析成 JSON。设备休眠时
    连接失败也只等一个 ping 超时，适合主页轮询与手动"检测上线"。
    """
    registered = bool(_device_base_url())
    code, text = _device_http("GET", "/ping", _DEVICE_PING_TIMEOUT_SEC)
    if code != 200:
        return jsonify({"online": False, "registered": registered,
                        "alwayson": None, "debug": None, "ip": "",
                        "error": text or f"HTTP {code}"})
    try:
        info = dict(re.findall(r"(\w+)=([^\s]+)", text or ""))
        uptime_ms = int(info.get("uptime_ms", "0") or 0)
    except ValueError:
        info, uptime_ms = {}, 0
    return jsonify({
        "online": True,
        "registered": True,
        "alwayson": info.get("alwayson") == "1",
        "debug": info.get("debug") == "1",
        "ip": info.get("ip", ""),
        "uptime_ms": uptime_ms,
    })


@app.post("/api/device/set_endpoint")
def api_device_set_endpoint():
    """手动指定设备地址（写入与 /api/device_register 相同的 endpoint.json）。

    设备自动注册失败时的兜底：注册成功后设备会以真实连接源覆盖此记录。
    """
    try:
        ip, port = validate_device_endpoint(
            request.form.get("ip", ""), request.form.get("port", "80"))
    except ValueError:
        return jsonify({"error": "仅允许有效的局域网 IPv4 地址和端口"}), 400

    old = _load_device_endpoint()
    rec = {"ip": ip, "port": port,
           "alwayson": bool(old.get("alwayson")),
           "ts": datetime.now().isoformat(timespec="seconds"),
           "manual": True}
    try:
        DEVICE_LOG_DIR.mkdir(parents=True, exist_ok=True)
        DEVICE_ENDPOINT_FILE.write_text(
            json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        return jsonify({"error": f"写入失败：{e}"}), 500
    return jsonify({"ok": True, "endpoint": rec})


@app.get("/api/device/console_status")
def api_device_console_status():
    """注册信息 + 设备在线探测 + 设备状态（控制台页面加载/刷新时拉取）。"""
    ep = _load_device_endpoint()
    payload: dict = {
        "registered": bool(ep.get("ip")),
        "endpoint": {k: ep.get(k) for k in ("ip", "port", "alwayson", "ts")},
        "online": False,
        "device": None,
        "error": "",
    }
    code, text = _device_http("GET", "/api/status", _DEVICE_PING_TIMEOUT_SEC)
    if code == 200:
        try:
            payload["device"] = json.loads(text)
            payload["online"] = True
        except ValueError:
            payload["error"] = "设备状态解析失败（固件异常？）"
    else:
        # 旧固件没有 /api/status：用 /ping 至少判定在线
        pcode, ptext = _device_http("GET", "/ping", _DEVICE_PING_TIMEOUT_SEC)
        if pcode == 200:
            payload["online"] = True
            payload["ping"] = ptext
            payload["error"] = "设备在线但固件较旧（缺少 /api/status），请更新固件使用完整控制台"
        else:
            payload["error"] = text or "设备无响应（已休眠或离线）"
    return jsonify(payload)


@app.get("/api/device/scan")
def api_device_scan():
    code, text = _device_http("GET", "/api/scan", _DEVICE_SCAN_TIMEOUT_SEC)
    if code != 200:
        return _device_err_resp(code, text)
    try:
        return jsonify(json.loads(text))
    except ValueError:
        return jsonify({"error": "扫描结果解析失败"}), 502


@app.post("/api/device/config")
def api_device_config():
    """转发网络/刷新配置到设备 POST /save。设备保存后会自行重启。"""
    allowed = ("ssid0", "pass0", "ssid1", "pass1", "ssid2", "pass2",
               "hostport", "hour", "tz", "rot180")
    data = {k: request.form.get(k, "") for k in allowed}
    code, text = _device_http("POST", "/save", _DEVICE_RENDER_TIMEOUT_SEC, data=data)
    if code != 200:
        return _device_err_resp(code, text)
    return jsonify({"ok": True, "message": "已保存，设备正在重启…"})


@app.post("/api/device/fetch")
def api_device_fetch():
    """手动拉取指定 idx 照片并渲染（设备 /fetch?idx=N，返回纯文本日志）。"""
    try:
        idx = int(request.form.get("idx", "0"))
    except (TypeError, ValueError):
        return jsonify({"error": "idx 需为整数"}), 400
    code, text = _device_http("GET", f"/fetch?idx={idx}", _DEVICE_RENDER_TIMEOUT_SEC)
    if code != 200:
        return _device_err_resp(code, text)
    return jsonify({"ok": True, "log": text})


@app.post("/api/device/screen_test")
def api_device_screen_test():
    """4 色测试条（设备 /screen?run=1，约 12-16s）。"""
    code, text = _device_http("GET", "/screen?run=1", _DEVICE_RENDER_TIMEOUT_SEC)
    if code != 200:
        return _device_err_resp(code, text)
    return jsonify({"ok": True, "log": text})


@app.post("/api/device/servo_test")
def api_device_servo_test():
    """简单转动测试（设备 /servo_test?run=1，纯转动不保存）。"""
    try:
        angle = float(request.form.get("angle", "0"))
        speed = float(request.form.get("speed", "40"))
    except (TypeError, ValueError):
        return jsonify({"error": "angle/speed 需为数字"}), 400
    code, text = _device_http(
        "GET", f"/servo_test?run=1&angle={angle}&speed={speed}",
        _DEVICE_RENDER_TIMEOUT_SEC)
    if code != 200:
        return _device_err_resp(code, text)
    return jsonify({"ok": True, "log": text})


@app.post("/api/device/servo_test_ori")
def api_device_servo_test_ori():
    """标定测试：转舵机 + 拉标定卡渲染（设备 /servo?test=portrait|landscape）。"""
    ori = request.form.get("ori", "")
    if ori not in ("portrait", "landscape"):
        return jsonify({"error": "ori 需为 portrait|landscape"}), 400
    q = "&".join(f"{k}={request.form.get(k, '')}"
                 for k in ("p_deg", "l_deg", "spd", "inv", "rot180"))
    code, text = _device_http(
        "GET", f"/servo?test={ori}&{q}", _DEVICE_RENDER_TIMEOUT_SEC)
    if code != 200:
        return _device_err_resp(code, text)
    return jsonify({"ok": True, "log": _html_log_to_text(text)})


@app.post("/api/device/servo_save")
def api_device_servo_save():
    """转发标定保存到设备 POST /servo_save。"""
    data = {k: request.form.get(k, "") for k in ("p_deg", "l_deg", "spd", "inv", "rot180")}
    code, text = _device_http("POST", "/servo_save", _DEVICE_PING_TIMEOUT_SEC, data=data)
    if code != 200:
        return _device_err_resp(code, text)
    return jsonify({"ok": True, "log": _html_log_to_text(text)})


@app.post("/api/device/servo_sync")
def api_device_servo_sync():
    """位置校正（设备 /servo_sync?actual=，校正后重拉 photo_0 渲染）。"""
    actual = request.form.get("actual", "")
    if actual not in ("portrait", "landscape"):
        return jsonify({"error": "actual 需为 portrait|landscape"}), 400
    code, text = _device_http(
        "GET", f"/servo_sync?actual={actual}", _DEVICE_RENDER_TIMEOUT_SEC)
    if code != 200:
        return _device_err_resp(code, text)
    return jsonify({"ok": True, "log": _html_log_to_text(text)})


@app.post("/api/device/debug")
def api_device_debug():
    """debug / 常开开关（转发设备 /debug?state=...），返回切换后最新状态。"""
    state = request.form.get("state", "")
    if state not in ("on", "off", "confirm_off", "aon_on", "aon_off"):
        return jsonify({"error": "非法 state"}), 400
    code, text = _device_http("GET", f"/debug?state={state}", _DEVICE_PING_TIMEOUT_SEC)
    if code != 200:
        return _device_err_resp(code, text)
    st: dict = {}
    c2, t2 = _device_http("GET", "/api/status", _DEVICE_PING_TIMEOUT_SEC)
    if c2 == 200:
        try:
            st = json.loads(t2)
        except ValueError:
            st = {}
    return jsonify({"ok": True, "device": st or None})


@app.get("/files/")
@app.get("/files/<path:subpath>")
def browse(subpath: str = ""):
    _require_webui_enabled()
    try:
        p = _safe_join(BIN_OUTPUT_DIR, subpath)
    except Exception:
        abort(400)

    if p.is_file():
        return _send_static_file(p)

    if not p.exists() or not p.is_dir():
        abort(404)

    items = []
    for child in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        name = child.name + ("/" if child.is_dir() else "")
        rel = child.relative_to(BIN_OUTPUT_DIR)
        href = "/files/" + str(rel).replace("\\", "/")
        items.append(f'<li><a href="{html.escape(href)}">{html.escape(name)}</a></li>')

    up = ""
    if p != BIN_OUTPUT_DIR:
        parent_rel = p.parent.relative_to(BIN_OUTPUT_DIR)
        up_href = "/files/" + str(parent_rel).replace("\\", "/")
        up = f'<a href="{html.escape(up_href)}">⬅ 返回上级</a><br><br>'

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>InkTime Files</title>
<style>
body {{ font-family: -apple-system,BlinkMacSystemFont,system-ui,sans-serif; padding: 24px; }}
ul {{ line-height: 1.8; }}
code {{ background:#f2f2f2; padding:2px 6px; border-radius:4px; }}
</style>
</head>
<body>
<h3>输出目录浏览</h3>
<p>当前：<code>{html.escape(str(p.relative_to(BIN_OUTPUT_DIR) if p != BIN_OUTPUT_DIR else "."))}</code></p>
{up}
<ul>
{''.join(items)}
</ul>
</body>
</html>
"""


if __name__ == "__main__":
    mimetypes.add_type("application/octet-stream", ".bin")
    print(f"[InkTime] DB: {DB_PATH}")
    print(f"[InkTime] IMAGE_DIR: {IMAGE_DIR}")
    print(f"[InkTime] OUT: {BIN_OUTPUT_DIR}")
    print("[InkTime] download key: [REDACTED]")
    print(f"[InkTime] listen: {FLASK_HOST}:{FLASK_PORT}")
    print(f"[InkTime] open: http://127.0.0.1:{FLASK_PORT}/  (本机)")
    # threaded=True:NAS 大图流式传输时不阻塞其它请求(单线程时 /sim 会排在图片流后面,点击详情像"没反应")
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=False, threaded=True)