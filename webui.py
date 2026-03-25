#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations
from flask import Flask, render_template, jsonify, send_file, abort
import sqlite3
import json
from pathlib import Path
import config as cfg
import re
import urllib.parse

app = Flask(__name__)

# Config loading
ROOT_DIR = Path(__file__).resolve().parent

DB_PATH = Path(str(getattr(cfg, "DB_PATH", "./photos.db") or "./photos.db")).expanduser()
if not DB_PATH.is_absolute():
    DB_PATH = (ROOT_DIR / DB_PATH).resolve()

IMAGE_DIR = Path(str(getattr(cfg, "IMAGE_DIR", "") or "")).expanduser()
if not IMAGE_DIR.is_absolute():
    IMAGE_DIR = (ROOT_DIR / IMAGE_DIR).resolve()

# Using a distinct port for the webui manager
WEBUI_HOST = str(getattr(cfg, "WEBUI_HOST", "0.0.0.0") or "0.0.0.0")
WEBUI_PORT = int(getattr(cfg, "WEBUI_PORT", 8766) or 8766)

# Performance tuning: Precompute IMAGE_DIR string for fast image url prefix checking
_IMAGE_DIR_FAST = str(IMAGE_DIR).replace("\\", "/")

# Pre-compile regex for dating
_RE_DATE1 = re.compile(r'(20\d{2}|19\d{2})[-_ \.](0[1-9]|1[0-2])[-_ \.](0[1-9]|[12]\d|3[01])')
_RE_DATE2 = re.compile(r'(?:^|[^0-9])((?:20|19)\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?:[^0-9]|$)')
_RE_DATE3 = re.compile(r'(?:^|[^0-9])((?:20|19)\d{2})(0[1-9]|1[0-2])(?:[^0-9]|$)')

def extract_date_from_exif(exif_json: str | None, filepath: str = "") -> str:
    date_str = ""
    if exif_json:
        # JSON parsing overhead is acceptable (~0.05ms per operation)
        try:
            data = json.loads(exif_json)
            dtv = data.get("datetime")
            if dtv:
                date_part = str(dtv).split()[0]
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


def _make_image_url(path_str: str) -> str:
    """把长绝对路径转为前端能访问的 /images 开头的路由，利用前缀匹配极致缩短渲染时间"""
    clean_path = path_str.replace("\\", "/")
    # Fast path: basic string strip (happens in >99% cases because DB stores absolute paths directly)
    if clean_path.startswith(_IMAGE_DIR_FAST):
        rel = clean_path[len(_IMAGE_DIR_FAST):].lstrip("/")
        return "/images/" + rel
    # Fallback to slow filesystem validation
    try:
        p = Path(path_str).expanduser().resolve()
        rel = p.relative_to(IMAGE_DIR.resolve())
        return "/images/" + str(rel).replace("\\", "/")
    except Exception:
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


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/photos')
def api_photos():
    if not DB_PATH.exists():
        return jsonify({"data": []})

    uri = f"file:{DB_PATH.as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.OperationalError:
        conn = sqlite3.connect(DB_PATH)
        
    c = conn.cursor()
    rows = c.execute('''
        SELECT path,
               caption,
               type,
               memory_score,
               beauty_score,
               exif_city,
               reason,
               side_caption,
               used_at,
               exif_json
        FROM photo_scores
    ''').fetchall()
    conn.close()

    data = []
    # Hot loop containing 16,000+ items - precompute function refs where helpful, though minor
    for r in rows:
        path, caption, p_type, memory_score, beauty_score, city, reason, side_caption, used_at, exif_json = r
        
        date_str = extract_date_from_exif(exif_json, path)

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
            "exif_json": exif_json or "{}"
        })
        
    return jsonify({"data": data})


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
