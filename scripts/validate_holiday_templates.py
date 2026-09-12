#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
节假日模式验证（2026-09-12 设计 §10.3）：
  venv/bin/python scripts/validate_holiday_templates.py

四部分：
  1. holidays-example.json 匹配验证 —— 每条目已知真值命中/不命中断言
  2. 全年扫描 —— 2027 全年逐日命中分布（范围内天数合理性）
  3. 模板样张 —— 5 模板 × 横竖屏 × 关键版式（含 L02/L06/P05 让位特例），
     每张给出 RGB 与四色抖动双版本
  4. AI 连通性（可选）—— 合成图 + 假节日走一次真实 GLM 调用
结果输出自包含 HTML 报告（图片内嵌 base64，双击即看）：
  docs/holiday-template-validation/index.html
"""

from __future__ import annotations

import base64
import datetime as dt
import io
import json
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import holidays as holidays_mod  # noqa: E402
import holiday_ai  # noqa: E402
import holiday_render  # noqa: E402
import render_layouts  # noqa: E402
import render_daily_photo as rdp  # noqa: E402

EXAMPLE = ROOT / "holidays-example.json"
REPORT_DIR = ROOT / "docs" / "holiday-template-validation"

results: list = []   # {"part", "desc", "pass", "detail"}
samples: list = []   # {"title", "rgb_b64", "dith_b64", "note"}


def record(part: str, desc: str, ok: bool, detail: str = "") -> None:
    results.append({"part": part, "desc": desc, "pass": ok, "detail": detail})
    print(f"  {'✓' if ok else '✗'} {desc}" + (f"  [{detail}]" if detail else ""))


# ========== 1. 匹配验证 ==========

def validate_matching(entries: list) -> None:
    print("== 1. holidays-example.json 匹配验证 ==")
    # (期望节日名 or ""=不命中, 日期, 附注)
    truth = [
        ("春节", dt.date(2027, 2, 6), "农历 1-1"),
        ("春节", dt.date(2027, 2, 11), "范围端点 1-6"),
        ("", dt.date(2027, 2, 12), "范围外 1-7"),
        ("除夕", dt.date(2027, 2, 5), "2027 春节前一天"),
        ("除夕", dt.date(2026, 2, 16), "2026 春节(2-17)前一天"),
        ("中秋节", dt.date(2026, 9, 25), "农历 8-15"),
        ("", dt.date(2026, 9, 24), "中秋前一天"),
        ("端午节", dt.date(2026, 6, 19), "农历 5-5"),
        ("国庆节", dt.date(2026, 10, 1), "范围起点"),
        ("国庆节", dt.date(2026, 10, 7), "范围终点"),
        ("", dt.date(2026, 10, 8), "范围外"),
        ("元旦", dt.date(2027, 1, 1), "公历 1-1"),
        ("儿童节", dt.date(2026, 6, 1), "公历 6-1"),
        ("女儿的生日", dt.date(2027, 6, 18), "带年份一次性"),
        ("", dt.date(2028, 6, 18), "一次性日期次年不命中"),
        ("爸爸70大寿", dt.date(2027, 3, 5), "带年份一次性"),
        ("妈妈生日", dt.date(2026, 8, 20), "每年循环"),
        ("我的生日", dt.date(2026, 11, 11), "每年循环"),
        ("结婚纪念日", dt.date(2026, 5, 20), "公历 5-20"),
    ]
    npass = 0
    for expect, d, note in truth:
        hit = holidays_mod.get_today_holiday(d, entries)
        got = hit["name"] if hit else ""
        ok = got == expect
        npass += ok
        actual = got or "不命中"
        record("匹配", f"{d} → {'不命中' if not expect else expect}"
                       f"{'' if ok else f'（实际: {actual}）'}",
               ok, note)
    # 模板字段
    for d, tpl in [(dt.date(2026, 6, 1), "child_birthday"),
                   (dt.date(2027, 3, 5), "elder_birthday"),
                   (dt.date(2026, 11, 11), "adult_birthday"),
                   (dt.date(2026, 5, 20), "anniversary")]:
        hit = holidays_mod.get_today_holiday(d, entries)
        record("匹配", f"{d} 模板字段 = {tpl}", hit and hit["template"] == tpl)
        npass += hit and hit["template"] == tpl
    rate = npass / (len(truth) + 4)
    record("匹配", f"匹配验证总通过率 {npass}/{len(truth) + 4}", rate == 1.0)


def yearly_scan(entries: list) -> dict:
    print("== 2. 2027 全年扫描 ==")
    counter: dict = {}
    d = dt.date(2027, 1, 1)
    while d.year == 2027:
        hit = holidays_mod.get_today_holiday(d, entries)
        if hit:
            counter.setdefault(hit["name"], []).append(d.isoformat())
        d += dt.timedelta(days=1)
    for name, days in sorted(counter.items(), key=lambda kv: -len(kv[1])):
        record("扫描", f"{name}: {len(days)} 天（首日 {days[0]}）",
               True, "、".join(days[:6]) + ("…" if len(days) > 6 else ""))
    expected = {"春节": 6, "除夕": 1, "元旦": 1, "国庆节": 7,
                "女儿的生日": 1, "爸爸70大寿": 1}
    for name, n in expected.items():
        got = len(counter.get(name, []))
        record("扫描", f"{name} 2027 年命中 {got} 天（期望 {n}）", got == n)
    return counter


# ========== 3. 模板样张 ==========

def make_test_photo(w: int = 1200, h: int = 900) -> Image.Image:
    """合成测试照片：天空渐变 + 山丘 + 太阳 + 主体色块（无真实隐私内容）。"""
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        t = y / h
        r, g, b = int(120 + 100 * t), int(170 - 40 * t), int(220 - 90 * t)
        for x in range(w):
            px[x, y] = (r, g, b)
    d = ImageDraw.Draw(img)
    d.ellipse((w * 0.68, h * 0.10, w * 0.80, h * 0.28), fill=(255, 236, 160))
    d.polygon([(0, h), (0, h * 0.62), (w * 0.30, h * 0.40), (w * 0.55, h * 0.66),
               (w * 0.78, h * 0.46), (w, h * 0.64), (w, h)], fill=(74, 96, 68))
    d.ellipse((w * 0.30, h * 0.48, w * 0.44, h * 0.72), fill=(198, 134, 96))
    d.ellipse((w * 0.33, h * 0.50, w * 0.37, h * 0.545), fill=(60, 44, 36))
    return img


def _cover(img: Image.Image, cw: int, ch: int) -> Image.Image:
    scale = max(cw / img.width, ch / img.height)
    dw, dh = max(cw, round(img.width * scale)), max(ch, round(img.height * scale))
    r = img.resize((dw, dh), Image.LANCZOS)
    return r.crop(((dw - cw) // 2, (dh - ch) // 2,
                   (dw - cw) // 2 + cw, (dh - ch) // 2 + ch))


def _jpg_b64(img: Image.Image, quality: int = 82) -> str:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def render_samples(entries: list) -> None:
    print("== 3. 模板样张（RGB + 四色抖动）==")
    photo = make_test_photo()
    item = {
        "path": "synthetic", "date": "2027-02-06", "side": "全家在爷爷家吃年夜饭，孩子们在院子里放烟花",
        "city": "厦门市", "lat": None, "lon": None, "memory": 92,
        "subjects_json": json.dumps([
            {"label": "person", "conf": 0.9, "bbox": [0.32, 0.5, 0.12, 0.24]}]),
    }
    matrix = {
        "portrait": (rdp.CANVAS_WIDTH, rdp.CANVAS_HEIGHT, "P01",
                     [("P05", "general_ribbon"), ("P05", "anniversary"),
                      ("P10", "general_ribbon"), ("P10", "anniversary")]),
        "landscape": (int(getattr(rdp.cfg, "LANDSCAPE_CANVAS_WIDTH", 800)),
                      int(getattr(rdp.cfg, "LANDSCAPE_CANVAS_HEIGHT", 480)), "L01",
                      [("L02", "general_ribbon"), ("L02", "anniversary"),
                       ("L06", "general_ribbon"), ("L06", "anniversary"),
                       ("L07", "general_ribbon")]),
    }
    for orient, (cw, ch, base, extras) in matrix.items():
        layout_tpl = [(base, t) for t in
                      ("general_ribbon", "child_birthday", "elder_birthday",
                       "adult_birthday", "anniversary")] + extras
        for layout, tpl in layout_tpl:
            holiday = {"name": "春节" if tpl.startswith("general") else _tpl_cn(tpl),
                       "greeting": _tpl_greeting(tpl),
                       "template": tpl, "ai_hint": "测试"}
            canvas = Image.new("RGB", (cw, ch), (255, 255, 255))
            canvas.paste(_cover(photo, cw, ch), (0, 0))
            render_layouts.render_layout(canvas, layout, item)
            holiday_render.apply_holiday_layer(canvas, layout, orient, holiday)
            dithered = rdp.quantize_four_color(canvas)
            samples.append({
                "title": f"{orient[:1].upper()}{orient[1:]} · {layout} · {holiday['name']}（{tpl}）",
                "rgb_b64": _jpg_b64(canvas),
                "dith_b64": _jpg_b64(dithered.convert("RGB"), 90),
                "note": _placement_note(layout),
            })
            print(f"  · {orient} {layout} {tpl} 渲染完成")


def _tpl_cn(tpl: str) -> str:
    return {"child_birthday": "女儿的生日", "elder_birthday": "爸爸70大寿",
            "adult_birthday": "我的生日", "anniversary": "结婚纪念日",
            "general_ribbon": "春节"}.get(tpl, "春节")


def _tpl_greeting(tpl: str) -> str:
    return {"general_ribbon": "新春快乐 阖家团圆", "child_birthday": "生日快乐",
            "elder_birthday": "福如东海 寿比南山", "adult_birthday": "又长大一岁",
            "anniversary": "永结同心"}.get(tpl, "")


def _placement_note(layout: str) -> str:
    return {"L02": "让位：元素右移 x≥240", "L06": "让位：元素右移 x≥390",
            "P05": "让位：右缘截短 x≤370"}.get(layout, "")


# ========== 4. AI 连通性（可选）==========

def validate_ai() -> None:
    print("== 4. AI 连通性 ==")
    if not holiday_ai.enabled():
        record("AI", "未启用（HOLIDAY_AI_* 未配置），跳过", True, "skip")
        return
    import config as cfg
    url, proto = holiday_ai._endpoint(str(cfg.HOLIDAY_AI_API_URL))
    model = str(cfg.HOLIDAY_AI_MODEL)
    print(f"  endpoint={url} proto={proto} model={model}")
    tmp = ROOT / "tmp" / "holiday_validate"
    tmp.mkdir(parents=True, exist_ok=True)
    cands = []
    for i in range(2):
        p = tmp / f"fake_{i}.jpg"
        make_test_photo(640, 480).save(p, format="JPEG", quality=85)
        cands.append({"path": str(p), "side": f"合成测试图 {i + 1}", "city": "厦门市"})
    holiday = {"name": "中秋节", "greeting": "", "template": "general_ribbon",
               "ai_hint": "中秋节赏月（合成图连通性测试）"}
    t0 = time.time()
    try:
        out = holiday_ai.select_and_describe(cands, holiday)
        dt_s = time.time() - t0
        if out:
            record("AI", f"GLM 调用成功（{dt_s:.1f}s）", True,
                   f"model={model} greeting=\"{out.get('greeting', '')}\" "
                   f"描述1=\"{cands[0].get('side', '')[:30]}\"")
        else:
            record("AI", "GLM 调用失败（返回 None，运行日志见上方警告）", False,
                   f"耗时 {dt_s:.1f}s")
    except Exception as e:
        record("AI", f"GLM 调用异常: {type(e).__name__}: {e}", False)
    finally:
        for c in cands:
            Path(c["path"]).unlink(missing_ok=True)


# ========== HTML 报告 ==========

def write_html(counter: dict) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    n_pass = sum(1 for r in results if r["pass"])
    n_all = len(results)
    parts = ["匹配", "扫描", "样张", "AI"]
    part_rows = ""
    for part in parts:
        rs = [r for r in results if r["part"] == part]
        if not rs:
            continue
        ok = sum(1 for r in rs if r["pass"])
        color = "#1a7f37" if ok == len(rs) else "#cf222e"
        part_rows += (f"<tr><td>{part}</td><td>{ok}/{len(rs)}</td>"
                      f"<td style='color:{color}'>{'通过' if ok == len(rs) else '存在失败'}</td></tr>")
    match_rows = "".join(
        f"<tr><td>{r['desc']}</td>"
        f"<td style='color:{'#1a7f37' if r['pass'] else '#cf222e'}'>"
        f"{'✓' if r['pass'] else '✗'}</td><td>{r['detail']}</td></tr>"
        for r in results if r["part"] in ("匹配", "扫描"))
    cards = ""
    for s in samples:
        cards += f"""
        <figure class="card">
          <figcaption>{s['title']}{f"<span class='note'>{s['note']}</span>" if s['note'] else ""}</figcaption>
          <div class="pair">
            <img src="data:image/jpeg;base64,{s['rgb_b64']}" alt="RGB">
            <img src="data:image/jpeg;base64,{s['dith_b64']}" alt="抖动">
          </div>
          <p class="cap">左：RGB 成品 · 右：四色抖动后</p>
        </figure>"""
    ai_rs = [r for r in results if r["part"] == "AI"]
    ai_html = "".join(
        f"<p class='{'ok' if r['pass'] else 'bad'}'>{'✓' if r['pass'] else '✗'} "
        f"{r['desc']} {r['detail']}</p>" for r in ai_rs) or "<p>未执行</p>"
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    html = f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>节假日模式验证报告 {now}</title>
<style>
body{{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;
     margin:24px auto;max-width:1080px;color:#1f2328;line-height:1.6}}
h1{{font-size:22px}} h2{{font-size:18px;border-bottom:2px solid #d0d7de;padding-bottom:6px;margin-top:36px}}
table{{border-collapse:collapse;width:100%;font-size:14px}}
td,th{{border:1px solid #d0d7de;padding:6px 10px;text-align:left}}
th{{background:#f6f8fa}}
.badge{{display:inline-block;padding:2px 12px;border-radius:12px;color:#fff;
       background:{'#1a7f37' if n_pass == n_all else '#cf222e'}}}
.pair img{{width:49%;border:1px solid #d0d7de;border-radius:6px}}
.card{{margin:18px 0;padding:12px;border:1px solid #d0d7de;border-radius:10px}}
figcaption{{font-weight:600}} .note{{margin-left:10px;font-weight:400;color:#9a6700;font-size:13px}}
.cap{{color:#656d76;font-size:12px;margin:6px 0 0}}
.ok{{color:#1a7f37}} .bad{{color:#cf222e}}
code{{background:#f6f8fa;padding:1px 5px;border-radius:4px}}
</style></head><body>
<h1>节假日模式验证报告</h1>
<p>生成时间：{now} · 配置文件：<code>holidays-example.json</code>（{len(counter) and ''}模板样张 {len(samples)} 张）</p>
<p>总判定：<span class="badge">{n_pass}/{n_all} 项通过</span></p>
<h2>摘要</h2>
<table><tr><th>部分</th><th>通过</th><th>判定</th></tr>{part_rows}</table>
<h2>1/2. 匹配验证与全年扫描</h2>
<table><tr><th>用例</th><th>结果</th><th>说明</th></tr>{match_rows}</table>
<h2>3. 模板样张</h2>{cards}
<h2>4. AI 连通性</h2>{ai_html}
<p class="cap">API Key 不写入本报告。</p>
</body></html>"""
    out = REPORT_DIR / "index.html"
    out.write_text(html, encoding="utf-8")
    print(f"\n[OK] 报告已生成: {out}（{out.stat().st_size / 1024:.0f} KB）")


def main() -> None:
    entries = holidays_mod.load_holidays(EXAMPLE)
    if not entries:
        raise SystemExit("holidays-example.json 加载失败")
    print(f"载入 {len(entries)} 条节假日配置\n")
    validate_matching(entries)
    counter = yearly_scan(entries)
    render_samples(entries)
    validate_ai()
    write_html(counter)
    failed = [r for r in results if not r["pass"] and r["part"] != "AI"]
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
