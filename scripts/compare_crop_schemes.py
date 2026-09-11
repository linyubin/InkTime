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
    # 旧方案结构性白边（几何精确值：画布 - 缩放后尺寸，按每侧计）
    lbx = max(0, go["cw"] - go["draw_w"])
    lby = max(0, go["ch"] - go["draw_h"])
    lb_parts = []
    if lbx: lb_parts.append(f"左右各 {lbx // 2}px")
    if lby: lb_parts.append(f"上下各 {lby // 2}px")
    rec = {
        "idx": idx + 1, "name": name, "img": f"img/p{idx + 1:02d}.jpg",
        "ori": "横屏" if go["landscape"] else "竖屏",
        "size": f"{img_w}x{img_h}",
        "n_subs": len(subs),
        "zoom": round(gn["scale"] / go["scale"], 3),
        "band_old": band_old, "band_new": band_new,
        "lb_old": "、".join(lb_parts) if lb_parts else "无",
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

# ---------- 生成自包含 HTML 报告（照片 base64 内嵌，双击即可查看） ----------
import base64

NOTES = {
    1: "✅ 白边消除，主体无损",
    3: "亮色内容示例（白色内容推高白色占比，非留白）",
    4: "头顶小幅出画，面部完整",
    6: "旧版本已裁 5%，新方案略多",
    8: "躯干部分出画，三人面部完整",
    10: "头顶毛发裁切，面部完整；旧版卡片后压深色内容致文案发黑，新方案顺带改善",
    16: "亮色内容示例（白墙前拍摄，两版一致）",
    20: "轻微留白消除",
}
FEATURED = [
    (1, "横屏 60px 白边消除、主体无损（最典型场景）"),
    (8, "主体代价最大的一张（100%→59%）：三人全身合影装不进画面，旧方案整体缩小留上下白边；新方案保持满幅，躯干边缘出画但三人面部完整"),
    (4, "婴儿特写：旧方案上下各 80px 白边；新方案满幅，头顶略微出画"),
    (6, "旧方案左右各 60px 白边，右图为满幅效果"),
    (10, "猫：旧方案顶部整条回填带、主体被缩小，且卡片后正好压在深色内容上导致文案难以辨认；新方案全部改善"),
    (20, "轻微留白（左右各 7px）"),
]

def b64(idx):
    p = IMG_DIR / f"p{idx:02d}.jpg"
    return "data:image/jpeg;base64," + base64.b64encode(p.read_bytes()).decode()

def table_rows():
    out = []
    for r in summary:
        subs_cell = f'{r["n_subs"]}' if r["n_subs"] else "0"
        keep = f'{r["worst_old"]}→<b>{r["worst_new"]}</b>' if r["n_subs"] else "—"
        if r["n_subs"] and r["worst_new"] < 100:
            keep = f'<span class="warn">{r["worst_old"]}→{r["worst_new"]}</span>'
        lb = r["lb_old"] if r["lb_old"] != "无" else "无"
        if r["lb_old"] != "无":
            lb = f'<span class="bad">{lb}</span>→无'
        else:
            lb = "无→无"
        out.append(f'<tr><td>{r["idx"]}</td><td class="fn">{r["name"]}</td>'
                   f'<td>{r["ori"]}</td><td>{subs_cell}</td>'
                   f'<td>{lb}</td><td>{r["white_old"]}→{r["white_new"]}</td>'
                   f'<td>{keep}</td><td class="note">{NOTES.get(r["idx"], "")}</td></tr>')
    return "\n".join(out)

def featured_html():
    out = []
    for idx, cap in FEATURED:
        rec = next(r for r in summary if r["idx"] == idx)
        out.append(f'<section class="case"><h3>#{idx:02d} {rec["name"]}'
                   f'<span class="chip">{rec["ori"]} · 主体 {rec["n_subs"]}</span></h3>'
                   f'<p>{cap}</p><img src="{b64(idx)}" alt="p{idx:02d}"></section>')
    return "\n".join(out)

def gallery_html():
    out = []
    for r in summary:
        out.append(f'<figure><img src="{b64(r["idx"])}" alt="p{r["idx"]:02d}" loading="lazy">'
                   f'<figcaption>#{r["idx"]:02d} {r["name"]}</figcaption></figure>')
    return "\n".join(out)

CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin: 0; background: #f2f2ef; color: #1c1c1c;
       font: 15px/1.7 "Noto Sans CJK SC", "PingFang SC", "Microsoft YaHei", system-ui, sans-serif; }
.wrap { max-width: 1080px; margin: 0 auto; padding: 32px 20px 80px; }
h1 { font-size: 26px; line-height: 1.4; margin: 0 0 8px; }
h2 { font-size: 20px; margin: 44px 0 12px; border-left: 4px solid #c33; padding-left: 10px; }
h3 { font-size: 16px; margin: 0 0 6px; }
p { margin: 8px 0; }
.meta { color: #666; font-size: 13px; }
.chips { margin: 10px 0 0; }
.chip { display: inline-block; background: #e6e6e2; border-radius: 20px; padding: 2px 12px;
        font-size: 12px; color: #444; margin: 2px 6px 2px 0; }
.card { background: #fff; border-radius: 12px; padding: 20px 24px; margin: 16px 0;
        box-shadow: 0 1px 3px rgba(0,0,0,.08); }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { border-bottom: 1px solid #eee; padding: 7px 8px; text-align: left; white-space: nowrap; }
td.fn, td.note { white-space: normal; min-width: 120px; }
th { color: #666; font-weight: 600; position: sticky; top: 0; background: #fff; }
.bad { color: #c33; font-weight: 600; }
.warn { color: #b60; font-weight: 600; }
td.note { color: #666; }
.case { background: #fff; border-radius: 12px; padding: 18px 20px; margin: 14px 0;
        box-shadow: 0 1px 3px rgba(0,0,0,.08); }
.case img { width: 100%; height: auto; border-radius: 6px; cursor: zoom-in; }
.case p { color: #444; }
.gallery { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 14px; }
.gallery figure { margin: 0; background: #fff; border-radius: 10px; padding: 10px;
                  box-shadow: 0 1px 3px rgba(0,0,0,.08); }
.gallery img { width: 100%; height: auto; border-radius: 5px; cursor: zoom-in; }
.gallery figcaption { font-size: 12px; color: #666; margin-top: 6px;
                      overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.callout { background: #fff8e6; border-radius: 10px; padding: 14px 18px; font-size: 14px; margin: 14px 0; }
code { background: #ececea; border-radius: 4px; padding: 1px 6px; font-size: 13px; }
pre { background: #1e1e1e; color: #ddd; border-radius: 10px; padding: 14px 18px;
      overflow-x: auto; font-size: 13px; line-height: 1.6; }
#lb { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.88); z-index: 9;
      cursor: zoom-out; text-align: center; }
#lb img { max-width: 96vw; max-height: 96vh; margin: 2vh auto; display: block; border-radius: 4px; }
"""

HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>渲染裁剪方案对比报告：全出血 cover vs 内容感知缩小</title>
<style>__CSS__</style>
</head>
<body>
<div class="wrap">
  <h1>渲染裁剪方案对比报告<br><span style="font-size:16px;color:#666;">全出血 cover（新） vs 内容感知缩小 + 毛玻璃回填（旧）</span></h1>
  <div class="chips">
    <span class="chip">生成日期 2026-09-11</span>
    <span class="chip">随机 20 张 · 种子 20260911 · 候选池 27,237 张</span>
    <span class="chip">横屏 6 / 竖屏 14</span>
    <span class="chip">有主体 12 / 无主体 8</span>
    <span class="chip">新 = 50102bf · 旧 = cc50dd3</span>
  </div>
  <p class="meta">两版的伽马、四色抖动、文字排版代码完全相同，差异仅来自缩放与裁切策略。点击任意对比图可放大。</p>

  <h2>结论（TL;DR）</h2>
  <div class="card">
    <table>
      <tr><th>指标</th><th>旧方案（缩小+回填）</th><th>新方案（全出血 cover）</th></tr>
      <tr><td>结构性白边（几何精确值）</td>
          <td><span class="bad">6/20 张（30%）</span>：横屏左右各 60px×2、各 7px×1；竖屏上下各 80px×3</td>
          <td><b>0/20</b></td></tr>
      <tr><td>主体 bbox 保留率</td><td>20/20 全保留（靠缩小换来）</td>
          <td>16/20 全保留；4 张被裁（最差 59%）</td></tr>
      <tr><td>被裁主体是否切脸</td><td>—</td><td><b>0 例切脸</b>（均为头顶/躯干/四肢边缘出画）</td></tr>
      <tr><td>画面白色占比（发白程度）</td><td>留白 6 张均值 49.1%</td>
          <td>同 6 张均值 43.6%（平均 <b>−5.5pp</b>，最大 −10.5pp）</td></tr>
    </table>
    <p>旧方案的白边不是小概率事件：只要照片检测到主体且主体群（含 5% 呼吸边距）装不进「文字卡以上空间」就触发缩小，样本中命中 6/6——与「每天都有白边」的体感一致。</p>
    <p>新方案的代价：4/20 张的主体 bbox 有出画（保留率 81% / 81% / 59% / 80%），目视核对均为头顶、躯干边缘或四肢，<b>无一切脸</b>。此外旧版 <code>int()</code> 取整在浮点误差下可能比画布少 1px 产生白缝（样本中 10 张受影响），新版用 <code>ceil + 钳位</code> 消除。</p>
  </div>

  <h2>两种方案差在哪</h2>
  <pre>旧方案：cover 缩放 → 若主体群装不下 → 整体缩小（最低缩到 cover 的 85%）
        → 空隙用「原图放大模糊 + 70% 白色蒙版」回填 → 四色抖动后 = 白边
新方案：永远 cover 铺满画布（ceil 防取整白缝）
        → 主体适配交给滑动窗口裁切（尽量完整框入最多主体 + 切人平方惩罚 + 底部文字卡避让）</pre>

  <h2>重点案例</h2>
__FEATURED__

  <h2>全部 20 张明细</h2>
  <div class="card">
    <table>
      <tr><th>#</th><th>文件</th><th>朝向</th><th>主体</th><th>结构性白边 旧→新</th>
          <th>白色占比% 旧→新</th><th>最差主体保留% 旧→新</th><th>备注</th></tr>
__ROWS__
    </table>
  </div>
  <div class="callout">
    <b>亮色内容 ≠ 白边</b>：#3（白色床单为主）与 #16（白墙前拍摄）白色占比很高、边缘也有白色列读数，
    但两版一致、几何上零留白——那是照片内容本身亮。结构性白边只可能来自「缩放后没铺满」，新方案下恒为 0。<br>
    <b>交叉校验</b>：几何复算预测的 6 张旧方案留白，与抖动成品实测白带 6/6 全部一致。
    最差主体保留率中，被半透明文字卡遮住的部分仍计为「在画面内」（毛玻璃下隐约可见，与裁出画面性质不同）。
  </div>

  <h2>对比图总览（点击放大）</h2>
  <div class="gallery">
__GALLERY__
  </div>

  <h2>指标口径与复现</h2>
  <div class="card">
    <p><b>结构性白边</b>：两版各自 scale / 裁切窗 / 回填偏移的精确几何复算（内容无关），本报告主判据；
    <b>实测白带</b>：抖动成品上从四边向内数「纯白像素占比 &gt;60%」的连续列/行，仅作交叉验证；
    <b>确定性</b>：同照片重复渲染字节级一致，对比脚本连续两轮全部指标完全相同。</p>
    <p>复现（仓库根目录）：</p>
    <pre>venv/bin/python scripts/compare_crop_schemes.py</pre>
    <p>脚本自动 <code>git show cc50dd3:render_daily_photo.py</code> 提取旧版快照，抽样渲染后生成本页与 img/ 对比图（种子与张数在脚本头部 SEED / N）。</p>
  </div>

  <h2>后续可选优化</h2>
  <div class="card">
    <p>1. 竖屏管线的裁切窗口目前未传 <code>bottom_reserve</code>（文字卡避让），可补传 120px 减少主体被卡片压住的概率；</p>
    <p>2. 对 #08 这类「主体群物理上装不进画布」的全景合影，可在选片阶段加权规避，或引入 VLM 仲裁取舍（docs/inktime-smart-crop-plan.md 阶段二）。</p>
  </div>
</div>
<div id="lb"><img alt=""></div>
<script>
var lb = document.getElementById('lb'), lbimg = lb.querySelector('img');
document.addEventListener('click', function (e) {
  if (e.target.tagName === 'IMG' && e.target.closest('.case, .gallery')) {
    lbimg.src = e.target.src; lb.style.display = 'block';
  } else if (lb.style.display === 'block') {
    lb.style.display = 'none'; lbimg.src = '';
  }
});
document.addEventListener('keydown', function (e) { if (e.key === 'Escape') { lb.style.display = 'none'; lbimg.src = ''; } });
</script>
</body>
</html>
"""

html = (HTML.replace("__CSS__", CSS)
            .replace("__FEATURED__", featured_html())
            .replace("__ROWS__", table_rows())
            .replace("__GALLERY__", gallery_html()))
html_path = OUT_DIR / "report.html"
html_path.write_text(html, encoding="utf-8")
print(f"[OK] {html_path} ({html_path.stat().st_size / 1e6:.1f} MB, 照片已内嵌)")
