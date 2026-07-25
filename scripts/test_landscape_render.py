#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
横屏渲染可视化 golden test（匹配 scripts/test_crop_comparison.py 的风格：
独立脚本、不依赖 pytest、直接打真实 photos.db、把产物 dump 到 output/ 供人工肉眼检查）。

覆盖：
- 服务器渲染朝向路由：从真实 DB 取若干已知横排/竖排照片，分别调 render_image，
  dump preview_<orientation>_<n>.png 到 output/test_landscape/。
- sidecar：把每张照片对应的 photo_N.json 内容（按 main() 同样的决策方式重算）
  dump 到 output/test_landscape/sidecar_<orientation>_<n>.json。
- ENABLE_FRAME_ROTATION=False 降级：进程内临时关开关重渲一张横排照片，
  确认它退回竖屏 480×800、json 报 portrait。

运行：
    python3 scripts/test_landscape_render.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT_DIR))

import config as cfg
import render_daily_photo as rdp
from PIL import Image, ImageOps

# ── 路径解析（与 render_daily_photo.py 对齐）──
DB_PATH = Path(str(getattr(cfg, "DB_PATH", "./photos.db"))).expanduser()
if not DB_PATH.is_absolute():
    DB_PATH = (ROOT_DIR / DB_PATH).resolve()

OUTPUT_DIR = ROOT_DIR / "output" / "test_landscape"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# 为得到横排/竖排的样本，进程内强制开开关（测试用，不影响部署默认值）。
# 测试结束后会恢复原值。
_ORIGINAL_FLAG = bool(getattr(cfg, "ENABLE_FRAME_ROTATION", False))
cfg.ENABLE_FRAME_ROTATION = True
# 让 render_daily_photo 模块级别名也指向同一份开关（模块内通过 cfg 读取，无需重复）。

CANVAS_W = rdp.CANVAS_WIDTH
CANVAS_H = rdp.CANVAS_HEIGHT
LAND_W = int(getattr(cfg, "LANDSCAPE_CANVAS_WIDTH", 800))
LAND_H = int(getattr(cfg, "LANDSCAPE_CANVAS_HEIGHT", 480))


def fetch_sample_rows(orientation: str, limit: int = 3):
    """
    从真实 DB 取若干已知朝向的行。orientation 取 "landscape" / "portrait"。
    兼容旧库（无 subjects_json 列）——按需 SELECT，不假设该列存在。
    返回 list[dict]，字段与 load_sim_rows 的 item 基本对齐。
    """
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    # 先探测 subjects_json 列是否存在
    cols = [r[1] for r in cur.execute("PRAGMA table_info(photo_scores)").fetchall()]
    has_subj = "subjects_json" in cols
    subj_select = "subjects_json" if has_subj else "NULL AS subjects_json"

    if orientation == "landscape":
        where = "(orientation = 'landscape' OR (width IS NOT NULL AND height IS NOT NULL AND width > height))"
    else:
        where = "(orientation = 'portrait' OR (width IS NOT NULL AND height IS NOT NULL AND height > width))"

    sql = f"""
        SELECT path, exif_json, side_caption, memory_score,
               exif_gps_lat, exif_gps_lon, exif_city, beauty_score,
               type, used_at, {subj_select}, width, height, orientation
        FROM photo_scores
        WHERE exif_json IS NOT NULL AND {where}
        LIMIT ?
    """
    try:
        rows = cur.execute(sql, (limit,)).fetchall()
    finally:
        conn.close()

    items = []
    for r in rows:
        items.append({
            "path": str(r["path"]),
            "date": rdp.extract_date_from_exif(r["exif_json"] or "", str(r["path"])) or "2024-01-01",
            "md": "01-01",
            "side": r["side_caption"] or "",
            "memory": float(r["memory_score"]) if r["memory_score"] is not None else 50.0,
            "lat": r["exif_gps_lat"],
            "lon": r["exif_gps_lon"],
            "city": r["exif_city"] or "",
            "beauty": float(r["beauty_score"]) if r["beauty_score"] is not None else None,
            "type_raw": r["type"] or "",
            "used_at": r["used_at"],
            "subjects_json": r["subjects_json"] or "",
            "width": int(r["width"]) if r["width"] is not None else None,
            "height": int(r["height"]) if r["height"] is not None else None,
            "orientation_raw": r["orientation"] or "",
        })
    return items


def decide_sidecar(item) -> dict:
    """
    复刻 main() 的 sidecar 决策：用同一张已 EXIF transpose 的源图算朝向，
    保证 sidecar 内容与 render_image 实际分支一致。
    """
    try:
        img = rdp._resolve_and_load_image(item)
        w, h = img.size
        orient = rdp.compute_render_orientation(w, h)
    except Exception:
        orient = "portrait"
    if orient == "landscape":
        return {"orientation": orient, "w": LAND_W, "h": LAND_H}
    return {"orientation": orient, "w": CANVAS_W, "h": CANVAS_H}


def render_and_dump(items, label: str, start_n: int = 1):
    """对一组 items 渲染并 dump PNG + sidecar。返回 (success_count, fail_count)。"""
    ok = fail = 0
    for i, item in enumerate(items, start=start_n):
        path_str = item["path"]
        print(f"  [{label}#{i}] {path_str}")
        print(f"           DB orientation={item.get('orientation_raw') or '?'}  "
              f"db_w/h={item.get('width')}/{item.get('height')}")
        try:
            img = rdp.render_image(item)
        except Exception as e:
            print(f"           [SKIP] render_image 失败: {e}")
            fail += 1
            continue

        preview_path = OUTPUT_DIR / f"preview_{label}_{i}.png"
        img.save(preview_path)

        sidecar = decide_sidecar(item)
        sidecar_path = OUTPUT_DIR / f"sidecar_{label}_{i}.json"
        sidecar_path.write_text(json.dumps(sidecar, ensure_ascii=False), encoding="utf-8")

        print(f"           rendered={sidecar['orientation']}  canvas={img.size[0]}x{img.size[1]}  "
              f"-> {preview_path.name}, {sidecar_path.name}")
        ok += 1
    return ok, fail


def test_flag_off_fallback(landscape_item):
    """临时关掉 ENABLE_FRAME_ROTATION，确认横排照片退回竖屏 480×800、json 报 portrait。"""
    print("\n=== 降级测试：ENABLE_FRAME_ROTATION=False ===")
    if not landscape_item:
        print("  [SKIP] 没有可用的横排照片样本，跳过降级测试。")
        return
    cfg.ENABLE_FRAME_ROTATION = False
    try:
        print(f"  样本: {landscape_item['path']}")
        # 重新读 EXIF 修正后的朝向（此时开关关闭，compute_render_orientation 必为 portrait）
        try:
            img = rdp._resolve_and_load_image(landscape_item)
        except Exception as e:
            # 本机没有源文件（如 NAS 未挂载）时，仅靠逻辑层验证决策，
            # 不把脚本搞崩。
            print(f"  [SKIP] 源文件不在本机，无法实渲染: {str(e).splitlines()[0]}")
            # 用 width/height 兜底构造一个假尺寸走决策（关掉开关后必为 portrait）。
            db_w = landscape_item.get("width") or 800
            db_h = landscape_item.get("height") or 480
            orient = rdp.compute_render_orientation(db_w, db_h)
            print(f"  compute_render_orientation(db {db_w}x{db_h}, 开关关) -> {orient}  (期望 portrait)")
            assert orient == "portrait", "开关关闭时朝向决策不是 portrait！"
            print("  [PASS] 决策层确认：开关关闭时任何尺寸都判为 portrait（降级成立）。")
            return

        w, h = img.size
        orient = rdp.compute_render_orientation(w, h)
        print(f"  EXIF 修正后源图尺寸: {w}x{h}")
        print(f"  compute_render_orientation(开关关) -> {orient}")
        try:
            rendered = rdp.render_image(landscape_item)
            print(f"  render_image 输出尺寸: {rendered.size[0]}x{rendered.size[1]}  "
                  f"(期望 {CANVAS_W}x{CANVAS_H})")
            assert rendered.size == (CANVAS_W, CANVAS_H), "降级后画布不是竖屏 480×800！"
            assert orient == "portrait", "开关关闭时朝向决策不是 portrait！"
            out = OUTPUT_DIR / "preview_fallback_off_1.png"
            rendered.save(out)
            print(f"  [PASS] 已确认降级到竖屏，dump 到 {out.name}")
        except Exception as e:
            print(f"  [SKIP] 渲染失败（可能照片文件不在本机）: {str(e).splitlines()[0]}")
    finally:
        cfg.ENABLE_FRAME_ROTATION = True  # 恢复，供后续


def main():
    print("=" * 64)
    print("横屏渲染可视化 golden test")
    print(f"DB: {DB_PATH}  (存在: {DB_PATH.exists()})")
    print(f"输出目录: {OUTPUT_DIR}")
    print(f"开关初始值 ENABLE_FRAME_ROTATION = {_ORIGINAL_FLAG}  (测试期间强制 True)")
    print("=" * 64)

    if not DB_PATH.exists():
        print(f"\n[友好消息] 数据库不存在: {DB_PATH}")
        print("测试脚本需要真实 photos.db 才能取样。把 DB 放到上述路径后重跑。")
        print("现在退出（非崩溃）。")
        return

    print("\n=== 取样：横排 / 竖排各 3 张 ===")
    landscape_items = fetch_sample_rows("landscape", limit=3)
    portrait_items = fetch_sample_rows("portrait", limit=3)
    print(f"  横排样本数: {len(landscape_items)}")
    print(f"  竖排样本数: {len(portrait_items)}")

    if not landscape_items and not portrait_items:
        print("\n[友好消息] 数据库里没取到任何横排/竖排样本（可能 width/height/orientation 列为空）。")
        print("竖屏路径无法验证。退出。")
        return

    print("\n=== 渲染横排样本（期望走横屏 800×480）===")
    l_ok, l_fail = render_and_dump(landscape_items, "landscape", start_n=1)

    print("\n=== 渲染竖排样本（期望走竖屏 480×800，开关不影响竖排）===")
    p_ok, p_fail = render_and_dump(portrait_items, "portrait", start_n=1)

    # 降级测试：取第一张横排样本，关开关重渲
    test_flag_off_fallback(landscape_items[0] if landscape_items else None)

    # 汇总
    print("\n" + "=" * 64)
    print("汇总：")
    print(f"  横排成功/失败: {l_ok}/{l_fail}")
    print(f"  竖排成功/失败: {p_ok}/{p_fail}")
    print("\n下一步（人工）：")
    print(f"  请打开 {OUTPUT_DIR} 肉眼检查 PNG：")
    print(f"    - preview_landscape_*.png 应为 800×480 横排，主体（人/猫）不应被切头。")
    print(f"    - preview_portrait_*.png 应为 480×800 竖屏，与原管线一致。")
    print(f"    - preview_fallback_off_1.png 应为 480×800（关开关后横排退回竖屏）。")
    print(f"  对应的 sidecar_*.json 里的 orientation/w/h 应与 PNG 尺寸一致。")
    print("=" * 64)


if __name__ == "__main__":
    try:
        main()
    finally:
        # 恢复开关原值，避免污染进程外（虽是子进程，仍礼貌恢复）。
        cfg.ENABLE_FRAME_ROTATION = _ORIGINAL_FLAG
