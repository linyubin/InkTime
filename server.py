#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

from pathlib import Path
from flask import Flask, abort, send_file, Response, request, redirect, g, render_template
import mimetypes
import sqlite3
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
from common import extract_date_from_exif, parse_dirty_type, resolve_path
from io import BytesIO
import render_daily_photo as rdp

ROOT_DIR = Path(__file__).resolve().parent

# --- config ---
DOWNLOAD_KEY = str(getattr(cfg, "DOWNLOAD_KEY", "") or "").strip()
if not DOWNLOAD_KEY:
    raise SystemExit("config.py 里没有配置 DOWNLOAD_KEY")

DB_PATH = resolve_path(getattr(cfg, "DB_PATH", "./photos.db"), "./photos.db")
IMAGE_DIR = resolve_path(getattr(cfg, "IMAGE_DIR", ""))
BIN_OUTPUT_DIR = resolve_path(getattr(cfg, "BIN_OUTPUT_DIR", "./output"), "./output")
BIN_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def _init_db_pragmas() -> None:
    """启动时建索引并切 WAL：/review 的排序/筛选此前对 3 万行做逐行 json_extract
    全表扫描（每页两次、实测 ~1.6s/次）；exif_datetime 列与 JSON 同源，建索引后
    毫秒级。WAL 让读写不再互斥。"""
    if not DB_PATH.exists():
        return
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_photo_scores_exif_datetime "
                     "ON photo_scores(exif_datetime)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_photo_scores_md "
                     "ON photo_scores(substr(exif_datetime, 6, 2) || '-' || substr(exif_datetime, 9, 2))")
        conn.commit()
        conn.close()
    except sqlite3.Error:
        pass


_init_db_pragmas()

# 传输日志 / 拉取检测哨兵目录（由 inktime_daily.sh 读写）
TMP_DIR = ROOT_DIR / "tmp"
TMP_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR = ROOT_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
TRANSFER_LOG = LOG_DIR / "transfer.log"           # 详细 HTTP 传输日志（ESP32 拉取等）
LAST_FETCH_SENTINEL = TMP_DIR / "last_fetch.json"  # 拉取成功哨兵，供 inktime_daily.sh 轮询

FLASK_HOST = str(getattr(cfg, "FLASK_HOST", "0.0.0.0") or "0.0.0.0")
FLASK_PORT = int(getattr(cfg, "FLASK_PORT", 8765) or 8765)

# 是否开启照片库 WebUI（跑通后建议关闭，只保留 ESP32 下载接口）
ENABLE_REVIEW_WEBUI = bool(getattr(cfg, "ENABLE_REVIEW_WEBUI", True))

DAILY_PHOTO_QUANTITY = int(getattr(cfg, "DAILY_PHOTO_QUANTITY", 5) or 5)
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
SIM_RENDER_CACHE_MAX = int(getattr(cfg, "SIM_RENDER_CACHE_MAX_ENTRIES", 300) or 300)


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

    conn = sqlite3.connect(DB_PATH)
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


def ensure_transfer_history() -> None:
    """创建传输日志表（对齐 analyze_photos.py 的 CREATE TABLE IF NOT EXISTS 模式）。"""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS transfer_history (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts           TEXT,
            method       TEXT,
            path         TEXT,
            idx          INTEGER,
            status       INTEGER,
            bytes        INTEGER,
            duration_ms  REAL,
            client_ip    TEXT,
            user_agent   TEXT
        )
        """
    )
    conn.commit()
    conn.close()


ensure_transfer_history()


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
        log_line = (
            f"{ts_str} | {request.remote_addr} | {request.method} {request.path} | "
            f"{resp.status_code} | {size}B | {dur_ms:.0f}ms | {request.headers.get('User-Agent', '')}"
        )
        try:
            with open(TRANSFER_LOG, "a", encoding="utf-8") as f:
                f.write(log_line + "\n")
        except Exception as e:
            print(f"[transfer_log] write file failed: {e}")
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute(
                "INSERT INTO transfer_history(ts,method,path,idx,status,bytes,duration_ms,client_ip,user_agent) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    datetime.now().isoformat(),
                    request.method,
                    request.path,
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
    p = (base / rel).resolve()
    if not str(p).startswith(str(base.resolve())):
        raise ValueError("path traversal blocked")
    return p


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

    conn = sqlite3.connect(DB_PATH)
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

    conn = sqlite3.connect(DB_PATH)
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

    conn = sqlite3.connect(DB_PATH)
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
    if ENABLE_REVIEW_WEBUI:
        return redirect("/review")
    return Response("InkTime server running. WebUI disabled.", mimetype="text/plain; charset=utf-8")


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
                conn = sqlite3.connect(DB_PATH)
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
        cache_path = _sim_render_cache_path(f"{p}|{p.stat().st_mtime_ns}|{orient}|{dither}")
        if cache_path.exists():
            return send_file(cache_path, mimetype="image/png", as_attachment=False)

        img = rdp.render_image(meta, force_orientation=orient)
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
    if key != DOWNLOAD_KEY:
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
    if key != DOWNLOAD_KEY:
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
    if key != DOWNLOAD_KEY:
        abort(404)
    if idx < 0 or idx >= DAILY_PHOTO_QUANTITY:
        abort(404)
    p = BIN_OUTPUT_DIR / f"photo_{idx}.json"
    return _send_static_file(p)


@app.get("/static/inktime/<key>/latest.json")
def esp_latest_sidecar(key: str):
    """返回 latest.bin 对应的朝向 sidecar。不标已用。"""
    if key != DOWNLOAD_KEY:
        abort(404)
    p = BIN_OUTPUT_DIR / "latest.json"
    return _send_static_file(p)


@app.get("/static/inktime/<key>/calib_p.bin")
def esp_calib_portrait(key: str):
    """竖屏标定卡（480×800），由 render_calib_cards.py 预生成。"""
    if key != DOWNLOAD_KEY:
        abort(404)
    p = BIN_OUTPUT_DIR / "calib_p.bin"
    return _send_static_file(p)


@app.get("/static/inktime/<key>/calib_l.bin")
def esp_calib_landscape(key: str):
    """横屏标定卡（800×480），由 render_calib_cards.py 预生成。"""
    if key != DOWNLOAD_KEY:
        abort(404)
    p = BIN_OUTPUT_DIR / "calib_l.bin"
    return _send_static_file(p)


@app.get("/static/inktime/<key>/preview.png")
def esp_preview(key: str):
    if key != DOWNLOAD_KEY:
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
    if key != DOWNLOAD_KEY:
        abort(404)
    return str(int(time.time()))


@app.post("/static/inktime/<key>/shutdown")
def esp_shutdown(key: str):
    """供 inktime_daily.sh 在检测到 ESP32 拉取后优雅关闭 server。"""
    if key != DOWNLOAD_KEY:
        abort(404)

    def _stop() -> None:
        time.sleep(0.3)  # 留时间让响应先返回
        print("[InkTime] shutdown via /shutdown")
        os._exit(0)

    threading.Thread(target=_stop, daemon=True).start()
    return "shutting down", 200


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
    print(f"[InkTime] key: {DOWNLOAD_KEY}")
    print(f"[InkTime] listen: {FLASK_HOST}:{FLASK_PORT}")
    print(f"[InkTime] open: http://127.0.0.1:{FLASK_PORT}/  (本机)")
    # threaded=True:NAS 大图流式传输时不阻塞其它请求(单线程时 /sim 会排在图片流后面,点击详情像"没反应")
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=False, threaded=True)