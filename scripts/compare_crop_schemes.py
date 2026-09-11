#!/usr/bin/env python3
"""新旧裁剪方案对比：随机 20 张，双版本渲染 + 量化指标 + 对比图。

指标：
- 白边宽度：抖动成品上，从四边向内数"纯白像素占比>60%"的连续列/行数
- 白色像素占比：整图纯白(255,255,255)比例（衡量毛玻璃回填的"发白"程度）
- 主体保留率：按 subjects_json 的 bbox 经两版各自几何（缩放/裁切窗/回填偏移）
  映射到画布后，计算未被裁出画面的面积比例；另计被底部文字卡遮挡的比例
"""
import importlib.util
import json
import math
import random
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from PIL import Image, ImageDraw, ImageFont
from common import extract_date_from_exif

import render_daily_photo as new_mod

# 旧版渲染器快照：直接从修复提交(50102bf)的父提交提取，保证可复现
OLD_COMMIT = "50102bf^"
SNAPSHOT = ROOT / "tmp" / "render_daily_photo_old_snapshot.py"
if not SNAPSHOT.exists():
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    src = subprocess.run(["git", "-C", str(ROOT), "show", f"{OLD_COMMIT}:render_daily_photo.py"],
                         capture_output=True, text=True, check=True).stdout
    SNAPSHOT.write_text(src)

spec = importlib.util.spec_from_file_location("render_old_snapshot", str(SNAPSHOT))
old_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(old_mod)

SEED = 20260911
N = 20
COMBO_MAX_W = 1200  # 对比图最大宽度，超出则等比缩小（控制入库体积）
COMBO_QUALITY = 82
OUT_DIR = ROOT / "docs" / "crop-comparison-2026-09-11"
IMG_DIR = OUT_DIR / "img"
IMG_DIR.mkdir(parents=True, exist_ok=True)

conn = sqlite3.connect(new_mod.DB_PATH)
c = conn.cursor()

# ---------- 候选池（与 load_sim_rows 同样的过滤，只做 stat 不开图） ----------
rows = c.execute("""
    SELECT path, exif_json, side_caption, memory_score, exif_gps_lat, exif_gps_lon,
           exif_city, subjects_json FROM photo_scores WHERE exif_json IS NOT NULL
""").fetchall()

def resolve_path(raw):
    p = Path(raw)
    if p.exists():
        return p
    for old, new in getattr(new_mod.cfg, "PATH_MAP", {}).items():
        if raw.startswith(old):
            t = Path(raw.replace(old, new).replace("\\", "/"))
            if t.exists():
                return t
    return None

candidates = []
for raw, exif_json, side, mem, lat, lon, city, subjects in rows:
    if "screenshot" in str(raw).lower():
        continue
    date_str = extract_date_from_exif(exif_json, str(raw))
    if not date_str:
        continue
    if resolve_path(str(raw)) is None:
        continue
    candidates.append({"path": str(raw), "side": side or "", "date": date_str,
                       "lat": lat, "lon": lon, "city": city or "",
                       "memory": float(mem) if mem is not None else -1.0,
                       "subjects_json": subjects or ""})

print(f"[POOL] 可渲染候选: {len(candidates)}")
rng = random.Random(SEED)
sample = rng.sample(candidates, N)
conn.close()

# ---------- 主体过滤（与两版渲染器一致） ----------
def valid_subjects(subjects_json):
    if not subjects_json:
        return []
    try:
        subs = json.loads(subjects_json)
        weights = getattr(new_mod.cfg, "YOLO_SUBJECT_WEIGHTS", {"person": 5.0, "cat": 4.0, "dog": 4.0})
        return [s for s in subs if s.get("label") in weights and s.get("conf", 0) >= 0.2]
    except Exception:
        return []

# ---------- 两版几何复算（严格对照各自源码） ----------
def geometry(version, img_w, img_h, subjects_json):
    landscape = new_mod.compute_render_orientation(img_w, img_h) == "landscape"
    cw = int(getattr(new_mod.cfg, "LANDSCAPE_CANVAS_WIDTH", 800)) if landscape else new_mod.CANVAS_WIDTH
    ch = int(getattr(new_mod.cfg, "LANDSCAPE_CANVAS_HEIGHT", 480)) if landscape else new_mod.CANVAS_HEIGHT
    ratio_w, ratio_h = cw / img_w, ch / img_h
    scale = max(ratio_w, ratio_h)
    subs = valid_subjects(subjects_json)
    text_h = 110 if landscape else 0
    if version == "old" and subs:
        xs, ys = [], []
        for s in subs:
            cx, cy, bw, bh = s["bbox"]
            xs += [cx - bw / 2, cx + bw / 2]
            ys += [cy - bh / 2, cy + bh / 2]
        gminx, gmaxx = max(0.0, min(xs) - 0.05), min(1.0, max(xs) + 0.05)
        gminy, gmaxy = max(0.0, min(ys) - 0.05), min(1.0, max(ys) + 0.05)
        sub_w, sub_h = (gmaxx - gminx) * img_w, (gmaxy - gminy) * img_h
        msw = cw / sub_w if sub_w > 0 else scale
        msh = (ch - text_h) / sub_h if landscape and sub_h > 0 else (ch / sub_h if sub_h > 0 else scale)
        max_safe = min(msw, msh)
        min_scale = (1.0 - 0.15) * cw / img_w if landscape else ratio_w
        if scale > max_safe:
            scale = max(max_safe, min_scale)
    if version == "new":
        draw_w = max(cw, math.ceil(img_w * scale))
        draw_h = max(ch, math.ceil(img_h * scale))
    else:
        draw_w = int(img_w * scale)
        draw_h = int(img_h * scale)
    L, T = new_mod.compute_crop_window(draw_w, draw_h, cw, ch, subjects_json,
                                       bottom_reserve=text_h)
    # 旧版越界守卫 + 居中 letterbox 回填偏移
    if version == "old":
        L = L if draw_w >= cw else 0
        T = T if draw_h >= ch else 0
        px = (cw - draw_w) // 2 if draw_w < cw else 0
        py = (ch - draw_h) // 2 if draw_h < ch else 0
    else:
        px = py = 0
    letterbox = draw_w < cw or draw_h < ch
    return {"landscape": landscape, "cw": cw, "ch": ch, "scale": scale,
            "draw_w": draw_w, "draw_h": draw_h, "L": L, "T": T, "px": px, "py": py,
            "letterbox": letterbox, "n_subs": len(subs)}

def card_rect(landscape):
    inset = int(getattr(new_mod.cfg, "FRAME_MASK_INSET_PX", 0) or 0)
    if landscape:
        return (14 + inset, 480 - 110 - 4, 800 - 14 - inset, 480 - 14)
    vb = 800 - inset
    return (10, vb - 120 + 15 - 10, 470, vb - 10)

def subject_fractions(bbox, img_w, img_h, g):
    cx, cy, bw, bh = bbox
    x1 = (cx - bw / 2) * img_w * g["scale"] - g["L"] + g["px"]
    x2 = (cx + bw / 2) * img_w * g["scale"] - g["L"] + g["px"]
    y1 = (cy - bh / 2) * img_h * g["scale"] - g["T"] + g["py"]
    y2 = (cy + bh / 2) * img_h * g["scale"] - g["T"] + g["py"]
    area = (x2 - x1) * (y2 - y1)
    if area <= 0:
        return 0.0, 0.0
    ix1, iy1, ix2, iy2 = max(x1, 0), max(y1, 0), min(x2, g["cw"]), min(y2, g["ch"])
    on_canvas = max(0, ix2 - ix1) * max(0, iy2 - iy1) / area
    kx1, ky1 = g["landscape"] and card_rect(True) or card_rect(False), None
    cr = card_rect(g["landscape"])
    kx1, ky1 = max(ix1, cr[0]), max(iy1, cr[1])
    kx2, ky2 = min(ix2, cr[2]), min(iy2, cr[3])
    behind = max(0, kx2 - kx1) * max(0, ky2 - ky1) / area
    return on_canvas, behind

# ---------- 成品图指标 ----------
def band_metrics(img):
    """抖动成品：四边向内的连续白带宽度 + 整图纯白占比。"""
    im = img.convert("RGB")
    w, h = im.size
    p = im.load()
    def col_white(x):
        ys = range(0, h, 4)
        return sum(1 for y in ys if p[x, y] == (255, 255, 255)) / len(list(range(0, h, 4)))
    def row_white(y):
        xs = range(0, w, 4)
        return sum(1 for x in xs if p[x, y] == (255, 255, 255)) / len(list(range(0, w, 4)))
    L = R = T = B = 0
    for x in range(w):
        if col_white(x) > 0.6: L += 1
        else: break
    for x in range(w - 1, -1, -1):
        if col_white(x) > 0.6: R += 1
        else: break
    for y in range(h):
        if row_white(y) > 0.6: T += 1
        else: break
    for y in range(h - 1, -1, -1):
        if row_white(y) > 0.6: B += 1
        else: break
    px = list(im.getdata())
    white = sum(1 for v in px if v == (255, 255, 255))
    return {"L": L, "R": R, "T": T, "B": B, "white_ratio": white / len(px)}

# ---------- 渲染与汇总 ----------
def render_one(mod, item):
    img = mod.render_image(dict(item))
    gamma, _ = mod.choose_gamma(img)
    dith = mod.quantize_four_color(mod.apply_gamma(img, gamma))
    return dith

results = []
replaced = 0
pool = list(candidates)
for i, item in enumerate(list(sample)):
    try:
        img_src = new_mod._resolve_and_load_image(dict(item))
    except Exception as e:
        print(f"[SKIP] 无法加载 {item['path']}: {e}")
        sample[i] = None
        # 从池中补抽
        while pool:
            cand = rng.choice(pool)
            pool.remove(cand)
            try:
                new_mod._resolve_and_load_image(dict(cand))
                sample[i] = cand
                replaced += 1
                break
            except Exception:
                continue
sample = [s for s in sample if s]

font = None
fp = str(new_mod.FONT_PATH)
if fp and Path(fp).exists():
    try:
        font = ImageFont.truetype(fp, 26)
    except Exception:
        font = None
if font is None:
    font = ImageFont.load_default()

summary = []
for idx, item in enumerate(sample):
    raw = item["path"]
    name = Path(raw.replace("\\", "/")).name
    img_src = new_mod._resolve_and_load_image(dict(item))
    img_w, img_h = img_src.size
    go = geometry("old", img_w, img_h, item["subjects_json"])
    gn = geometry("new", img_w, img_h, item["subjects_json"])

    old_d = render_one(old_mod, item)
    new_d = render_one(new_mod, item)
    mo, mn = band_metrics(old_d), band_metrics(new_d)

    subs = valid_subjects(item["subjects_json"])
    worst_old = worst_new = 1.0
    behind_new = 0.0
    for s in subs:
        fo, _ = subject_fractions(s["bbox"], img_w, img_h, go)
        fn_, bn = subject_fractions(s["bbox"], img_w, img_h, gn)
        worst_old = min(worst_old, fo)
        worst_new = min(worst_new, fn_)
        behind_new = max(behind_new, bn)

    # 左右对比图
    a, b = old_d.convert("RGB"), new_d.convert("RGB")
    aw, ah = a.size
    gap, head = 8, 40
    combo = Image.new("RGB", (aw * 2 + gap, ah + head), (24, 24, 24))
    combo.paste(a, (0, head))
    combo.paste(b, (aw + gap, head))
    d = ImageDraw.Draw(combo)
    d.text((12, 8), f"旧方案", font=font, fill=(255, 255, 255))
    d.text((aw + gap + 12, 8), f"新方案", font=font, fill=(255, 255, 255))
    img_path = IMG_DIR / f"p{idx + 1:02d}.jpg"
    if combo.width > COMBO_MAX_W:
        ratio = COMBO_MAX_W / combo.width
        combo = combo.resize((COMBO_MAX_W, int(combo.height * ratio)), Image.LANCZOS)
    combo.save(img_path, quality=COMBO_QUALITY)

    band_old = f"{mo['L']}/{mo['R']}/{mo['T']}/{mo['B']}" if max(mo['L'], mo['R'], mo['T'], mo['B']) else "无"
    band_new = f"{mn['L']}/{mn['R']}/{mn['T']}/{mn['B']}" if max(mn['L'], mn['R'], mn['T'], mn['B']) else "无"
    rec = {
        "idx": idx + 1, "name": name, "img": f"img/p{idx + 1:02d}.jpg",
        "ori": "横屏" if go["landscape"] else "竖屏",
        "size": f"{img_w}x{img_h}",
        "n_subs": len(subs),
        "zoom": round(gn["scale"] / go["scale"], 3),
        "band_old": band_old, "band_new": band_new,
        "white_old": round(mo["white_ratio"] * 100, 1),
        "white_new": round(mn["white_ratio"] * 100, 1),
        "worst_old": round(worst_old * 100), "worst_new": round(worst_new * 100),
        "behind_new": round(behind_new * 100),
        "letterbox_old": go["letterbox"], "letterbox_new": gn["letterbox"],
    }
    summary.append(rec)
    print(f"[{idx + 1:02d}] {name[:30]:<30} {rec['ori']} 主体{rec['n_subs']} "
          f"白边 旧{band_old} 新{band_new} | 白% {rec['white_old']}→{rec['white_new']} | "
          f"主体最差保留 {rec['worst_old']}%→{rec['worst_new']}% (新方案卡后最大 {rec['behind_new']}%)")

# 交叉校验：几何复算预测旧版 letterbox 的，实测旧版白边必须 >0
mismatch = [r for r in summary if r["letterbox_old"] and
            max(int(r["band_old"].split("/")[0]), int(r["band_old"].split("/")[1]),
                int(r["band_old"].split("/")[2]), int(r["band_old"].split("/")[3])) == 0]
print(f"\n[CROSSCHECK] 复算 letterbox={sum(1 for r in summary if r['letterbox_old'])} 张, "
      f"实测有白边={sum(1 for r in summary if r['band_old'] != '无')} 张, 不一致={len(mismatch)} 张")
print(f"[POOL] 补抽 {replaced} 张")

(ROOT / "tmp/crop_comparison_summary.json").write_text(
    json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
print("[OK] tmp/crop_comparison_summary.json")
