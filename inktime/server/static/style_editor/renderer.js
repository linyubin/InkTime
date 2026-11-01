/* InkTime 样式编辑器 · canvas2d 镜像渲染器（ADR 0003 高保真预览）
 *
 * 与 Python 侧逐原语对齐（render_layouts.py / render_daily_photo.py）：
 * - 槽位文案拼装口径与 style_runtime.resolve_slot 一致；
 * - 文字用 Pillow "la" 锚点语义（y + fontBoundingBoxAscent 模拟）；
 * - 折行/截断复刻 wrap_text_chinese / _fit_text；
 * - 钤印小篆字形走 /style-assets/seal/<hex>.png；
 * - 墨水屏模式复刻 choose_gamma 二分 + Floyd–Steinberg 四色量化。
 * 已知差异（以真机对照为最终裁判）：文本栅格化/抗锯齿、毛玻璃模糊核。
 */
"use strict";

/* ---------- 槽位文案（与 Python 同口径） ---------- */
const Renderer = (() => {
  const SEAL_GLYPHS = {"厦": "5EC8", "门": "9580", "市": "5E02",
                       "木": "6728", "渎": "7006", "镇": "93AE"};
  const SEAL_STRIP = ["市", "镇", "县", "区"];
  const sealImgCache = {};

  function ensureSealImg(hex) {
    if (!sealImgCache[hex]) {
      const img = new Image();
      img.onload = () => document.dispatchEvent(new CustomEvent("style-render"));
      img.src = `/style-assets/seal/${hex}.png`;
      sealImgCache[hex] = img;
    }
    return sealImgCache[hex];
  }

  function formatDateDisplay(dateStr) {
    if (!dateStr) return "";
    const parts = String(dateStr).split("-");
    if (parts.length < 3) return dateStr;
    const m = parseInt(parts[1], 10), d = parseInt(parts[2], 10);
    if (isNaN(m) || isNaN(d)) return dateStr;
    return `${parts[0]}.${m}.${d}`;
  }

  function weekdayCn(dateStr) {
    try {
      const [y, m, d] = String(dateStr).split("-").slice(0, 3).map(Number);
      if (!y || !m || !d) return "";
      const wd = new Date(y, m - 1, d).getDay();
      return "星期" + "日一二三四五六"[wd];
    } catch (e) { return ""; }
  }

  function cityOf(item) { return String(item.city || "").trim(); }

  function resolveSlot(el, item) {
    const slot = el.slot || "static";
    const city = cityOf(item), full = formatDateDisplay(item.date),
          wd = weekdayCn(item.date);
    switch (slot) {
      case "static": return el.static_text || "";
      case "side": return item.side || "";
      case "city": return city;
      case "date_full": return full;
      case "date_year": return full.split(".")[0] || "";
      case "date_rest": { const t = full.split("."); return (t.slice(1).join(".")) || full; }
      case "weekday": return wd;
      case "meta_wd_city": return [full, wd, city].filter(Boolean).join(" · ");
      case "meta_city": return [full, city].filter(Boolean).join(" · ");
      case "date_wd": return [full, wd].filter(Boolean).join(" · ");
      case "wd_city": return [wd, city].filter(Boolean).join(" · ");
      default: return el.static_text || "";
    }
  }

  /* ---------- 字体 ---------- */
  function fontFamily(el) { return el.font === "lxgw" ? "LXGW" : "SmileySans"; }
  const ascCache = {};
  function fontAscent(ctx, size, family) {
    const key = `${family}|${size}`;
    if (!(key in ascCache)) {
      ctx.font = `${size}px ${family}`;
      ascCache[key] = ctx.measureText("国").fontBoundingBoxAscent || size;
    }
    return ascCache[key];
  }

  /* ---------- 折行 / 截断（复刻 Python） ---------- */
  function fitText(ctx, text, maxW) {
    if (!text) return "";
    if (ctx.measureText(text).width <= maxW) return text;
    while (text && ctx.measureText(text + "…").width > maxW) text = text.slice(0, -1);
    return text + "…";
  }

  function wrapLines(ctx, text, maxW, maxLines) {
    if (!text) return [];
    const lines = [];
    for (const para of String(text).replace(/\r\n/g, "\n").split("\n")) {
      if (lines.length >= maxLines) break;
      if (!para) continue;
      let line = "";
      for (const ch of para) {
        if (ctx.measureText(line + ch).width <= maxW) line += ch;
        else { if (line) lines.push(line); line = ch;
               if (lines.length >= maxLines) break; }
      }
      if (line && lines.length < maxLines) lines.push(line);
    }
    return lines;
  }

  /* ---------- 文字绘制（Pillow "la" 锚点语义） ---------- */
  function drawDark(ctx, s, x, y, size, el, item) {
    const family = fontFamily(el);
    ctx.font = `${size}px ${family}`;
    let fill = el.fill || [0, 0, 0];
    const col = `rgb(${fill[0]},${fill[1]},${fill[2]})`;
    let w = ctx.measureText(s).width;
    let stroke = el.stroke !== undefined ? el.stroke : 0.3;
    const align = el.align || "l";
    if (align === "c") x = (el.cx !== undefined && el.cx !== null ? el.cx : x) - w / 2;
    else if (align === "r") x = (el.cx !== undefined && el.cx !== null ? el.cx : x) - w;
    if (el.bold) stroke = Math.max(stroke, 0.6);
    const yb = y + fontAscent(ctx, size, family);
    if (stroke > 0) {
      ctx.lineWidth = stroke * 2;
      ctx.strokeStyle = col;
      ctx.strokeText(s, x, yb);
    }
    ctx.fillStyle = col;
    ctx.fillText(s, x, yb);
  }

  /* ---------- 元素绘制 ---------- */
  function roundRect(ctx, x0, y0, x1, y1, r, noBegin = false) {
    r = Math.max(0, Math.min(r, (x1 - x0) / 2, (y1 - y0) / 2));
    if (!noBegin) ctx.beginPath();
    ctx.moveTo(x0 + r, y0);
    ctx.arcTo(x1, y0, x1, y1, r);
    ctx.arcTo(x1, y1, x0, y1, r);
    ctx.arcTo(x0, y1, x0, y0, r);
    ctx.arcTo(x0, y0, x1, y0, r);
    ctx.closePath();
  }

  function drawGlass(ctx, el, photo) {
    const [x0, y0, x1, y1] = el.rect, w = x1 - x0, h = y1 - y0;
    if (w <= 0 || h <= 0) return;
    const radius = el.radius ?? 14, alpha = el.alpha ?? 184, blur = el.blur ?? 12;
    if (el.shadow) {
      const p = 24, off = document.createElement("canvas");
      off.width = w + 2 * p; off.height = h + 2 * p;
      const o = off.getContext("2d");
      o.filter = "blur(8px)";
      o.fillStyle = "rgba(0,0,0,0.373)";
      roundRect(o, p + 6, p + 6, p + 6 + w, p + 6 + h, radius); o.fill();
      ctx.drawImage(off, x0 - p, y0 - p);
    }
    if (photo) {
      const m = Math.max(8, blur * 2), off = document.createElement("canvas");
      off.width = w; off.height = h;
      const o = off.getContext("2d");
      o.filter = `blur(${blur}px)`;
      o.drawImage(photo, x0 - m, y0 - m, w + 2 * m, h + 2 * m, -m, -m, w + 2 * m, h + 2 * m);
      o.filter = "none";
      o.fillStyle = `rgba(255,255,255,${(alpha / 255).toFixed(4)})`;
      o.fillRect(0, 0, w, h);
      ctx.save();
      roundRect(ctx, x0, y0, x1, y1, radius); ctx.clip();
      ctx.drawImage(off, x0, y0);
      ctx.restore();
    }
    if (el.highlight !== false) {
      ctx.strokeStyle = "#fff"; ctx.lineWidth = 1;
      roundRect(ctx, x0, y0, x1, y1, radius); ctx.stroke();
    }
  }

  function drawVeil(ctx, el, veilAlphaDefault) {
    const [x0, y0, x1, y1] = el.rect, w = x1 - x0, h = y1 - y0;
    const a0 = el.a0 || 0, a1 = (el.a1 === null || el.a1 === undefined)
      ? veilAlphaDefault : el.a1;
    const p = el.ramp_px || h;
    const rgb = el.rgb || [246, 241, 231];
    for (let yy = 0; yy < h; yy++) {
      const t = yy < p ? yy / p : 1;
      const k = t * t * (3 - 2 * t);
      const a = a0 + (a1 - a0) * k;
      if (a > 0) {
        ctx.fillStyle = `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${(a / 255).toFixed(4)})`;
        ctx.fillRect(x0, y0 + yy, w, 1);
      }
    }
  }

  function drawTextEl(ctx, el, item, peers) {
    let s = resolveSlot(el, item);
    let cx = el.cx, fit = el.fit;
    const pp = el.peer_pad;
    if (pp && peers[pp.peer]) {
      if (cx !== undefined && cx !== null) cx += (pp.dx_cx || 0);
      if (fit !== undefined && fit !== null) fit += (pp.dx_fit || 0);
    }
    if (!s) return;
    const size = el.size || 18;
    ctx.font = `${size}px ${fontFamily(el)}`;
    if (fit !== undefined && fit !== null) s = fitText(ctx, s, fit);
    if (el.wrap) {
      const { max_w, max_lines, line_step } = el.wrap;
      const lines = wrapLines(ctx, s, max_w, max_lines);
      let y = el.y;
      for (const ln of lines) { drawDark(ctx, ln, el.x, y, size, { ...el, cx }); y += line_step; }
      return;
    }
    drawDark(ctx, s, el.x, el.y, size, { ...el, cx });
  }

  function drawVTextEl(ctx, el, item) {
    const s = resolveSlot(el, item);
    if (!s) return;
    const colH = el.col_h, pitch = el.pitch, colGap = el.col_gap;
    const maxCols = el.max_cols || 2;
    const perCol = Math.floor(colH / pitch);
    const n = Math.min(s.length, perCol * maxCols);
    const cols = [];
    for (let i = 0; i < n; i += perCol) cols.push(s.slice(i, i + perCol));
    const size = el.size || 24, family = fontFamily(el);
    ctx.font = `${size}px ${family}`;
    const fill = el.fill || [0, 0, 0];
    ctx.fillStyle = `rgb(${fill[0]},${fill[1]},${fill[2]})`;
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    cols.forEach((col, ci) => {
      const colCx = el.x - ci * colGap;
      for (let j = 0; j < col.length; j++)
        ctx.fillText(col[j], colCx, el.y + pitch / 2 + j * pitch);
    });
    ctx.textAlign = "left"; ctx.textBaseline = "alphabetic";
  }

  function sealHexesFor(city) {
    let name = String(city || "").trim();
    for (const suf of SEAL_STRIP)
      if (name.endsWith(suf) && name.length > suf.length) { name = name.slice(0, -suf.length); break; }
    if (!(name.length >= 1 && name.length <= 3)) return null;
    const hexes = [...name].map(ch => SEAL_GLYPHS[ch] || null);
    return hexes.includes(null) ? null : hexes;
  }

  function drawSeal(ctx, el, item) {
    const city = resolveSlot(el, item);
    if (!city) return;
    const { x, y, w, h } = el;
    const radius = el.radius ?? 6;
    ctx.fillStyle = "rgb(200,0,0)";
    roundRect(ctx, x, y, x + w, y + h, radius); ctx.fill();
    const name = (() => {
      let n = city;
      for (const suf of SEAL_STRIP)
        if (n.endsWith(suf) && n.length > suf.length) { n = n.slice(0, -suf.length); break; }
      return n;
    })();
    const hexes = sealHexesFor(city);
    const glyphPx = el.glyph_px || 26;
    if (hexes) {
      const gap = Math.max(2, glyphPx >> 3);
      const total = glyphPx * hexes.length + gap * (hexes.length - 1);
      let gx, gy;
      if (el.vertical) { gx = x + (w - glyphPx) / 2; gy = y + (h - total) / 2; }
      else { gx = x + (w - total) / 2; gy = y + (h - glyphPx) / 2; }
      ctx.imageSmoothingEnabled = true; ctx.imageSmoothingQuality = "high";
      hexes.forEach((hx, i) => {
        const img = ensureSealImg(hx);
        if (!img.complete || !img.naturalWidth) return;
        if (el.vertical) ctx.drawImage(img, Math.floor(gx), Math.floor(gy + i * (glyphPx + gap)), glyphPx, glyphPx);
        else ctx.drawImage(img, Math.floor(gx + i * (glyphPx + gap)), Math.floor(gy), glyphPx, glyphPx);
      });
      return;
    }
    /* 字体回退（无篆形城市） */
    let size = Math.max(12, glyphPx - 4);
    const family = fontFamily(el);
    ctx.font = `${size}px ${family}`;
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    if (el.vertical && name.length > 1) {
      const pitch = h / (name.length + 0.4);
      for (let i = 0; i < Math.min(4, name.length); i++)
        ctx.fillText(name[i], x + w / 2, y + pitch * (i + 0.55));
    } else {
      let s = name.length <= 3 ? name : name.slice(0, 2);
      while (ctx.measureText(s).width > w - 8 && size > 11) {
        size -= 1; ctx.font = `${size}px ${family}`; s = name.length <= 3 ? name : name.slice(0, 2);
      }
      ctx.fillStyle = "#fff";
      ctx.fillText(s, x + w / 2, y + h / 2);
    }
    ctx.textAlign = "left"; ctx.textBaseline = "alphabetic";
  }

  function drawBar(ctx, el) {
    const [x0, y0, x1, y1] = el.rect, f = el.fill || [200, 0, 0];
    ctx.fillStyle = `rgb(${f[0]},${f[1]},${f[2]})`;
    ctx.fillRect(x0, y0, x1 - x0, y1 - y0);
  }

  function drawFrame(ctx, el, cw, ch) {
    let x0, y0, x1, y1;
    if (el.rect) [x0, y0, x1, y1] = el.rect;
    else { const m = el.margin ?? 12; x0 = m; y0 = m; x1 = cw - m; y1 = ch - m; }
    const f = el.fill || [255, 255, 255];
    ctx.strokeStyle = `rgb(${f[0]},${f[1]},${f[2]})`;
    ctx.lineWidth = el.stroke_w || 2;
    ctx.strokeRect(x0, y0, x1 - x0, y1 - y0);
    if (el.double) {
      const g = el.double_gap || 4;
      ctx.lineWidth = 1;
      ctx.strokeRect(x0 + g, y0 + g, x1 - x0 - 2 * g, y1 - y0 - 2 * g);
    }
  }

  function drawBadge(ctx, el, item) {
    const { x, y, r } = el;
    const f = el.fill || [200, 0, 0];
    ctx.fillStyle = `rgb(${f[0]},${f[1]},${f[2]})`;
    ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.fill();
    const s = resolveSlot(el, item);
    if (s) {
      const tf = el.text_fill || [255, 255, 255];
      const size = el.size || 16;
      ctx.font = `${size}px ${fontFamily(el)}`;
      ctx.fillStyle = `rgb(${tf[0]},${tf[1]},${tf[2]})`;
      ctx.textAlign = "center"; ctx.textBaseline = "middle";
      ctx.fillText(s, x, y);
      ctx.textAlign = "left"; ctx.textBaseline = "alphabetic";
    }
  }

  /* ---------- 命中框 ---------- */
  function elBBox(ctx, el, item) {
    const k = el.kind;
    if (k === "glass" || k === "veil" || k === "bar")
      return { x0: el.rect[0], y0: el.rect[1], x1: el.rect[2], y1: el.rect[3] };
    if (k === "seal") return { x0: el.x, y0: el.y, x1: el.x + el.w, y1: el.y + el.h };
    if (k === "badge") return { x0: el.x - el.r, y0: el.y - el.r, x1: el.x + el.r, y1: el.y + el.r };
    if (k === "frame") {
      if (el.rect) return { x0: el.rect[0], y0: el.rect[1], x1: el.rect[2], y1: el.rect[3] };
      const m = el.margin ?? 12;
      return { x0: m, y0: m, x1: 1e9, y1: 1e9 };
    }
    if (k === "vtext") {
      const s = resolveSlot(el, item); if (!s) return null;
      const perCol = Math.floor(el.col_h / el.pitch);
      const maxCols = el.max_cols || 2;
      const cols = Math.min(2, Math.ceil(Math.min(s.length, perCol * maxCols) / Math.max(1, perCol)));
      const w = (cols - 1) * el.col_gap + (el.size || 24);
      const hh = Math.min(s.length, perCol) * el.pitch;
      return { x0: el.x - w, y0: el.y, x1: el.x + (el.size || 24), y1: el.y + hh };
    }
    /* text */
    const s = resolveSlot(el, item); if (!s) return null;
    const size = el.size || 18;
    ctx.font = `${size}px ${fontFamily(el)}`;
    let w = ctx.measureText(s).width, lines = 1;
    if (el.fit !== undefined && el.fit !== null) w = Math.min(w, el.fit);
    if (el.wrap) { lines = wrapLines(ctx, s, el.wrap.max_w, el.wrap.max_lines).length || 1;
      w = Math.min(w, el.wrap.max_w); }
    const align = el.align || "l";
    let x0 = el.x;
    if (align === "c") x0 = (el.cx ?? el.x) - w / 2;
    if (align === "r") x0 = (el.cx ?? el.x) - w;
    const lineH = el.wrap ? el.wrap.line_step : size * 1.35;
    return { x0: x0 - 2, y0: el.y - 2, x1: x0 + w + 2, y1: el.y + lines * lineH + 2 };
  }

  /* ---------- 墨水屏模式（gamma 二分 + FS 四色抖动） ---------- */
  const PALETTE = [[0, 0, 0], [255, 255, 255], [200, 0, 0], [220, 180, 0]];

  function gammaLut(gamma) {
    const lut = new Uint8Array(256);
    for (let i = 0; i < 256; i++)
      lut[i] = Math.min(255, Math.floor(255 * Math.pow(i / 255, gamma) + 0.5));
    return lut;
  }

  function chooseGamma(hist) {
    const target = 0.45, gMin = 0.60, gMax = 1.00;
    const total = hist.reduce((a, b) => a + b, 0) || 1;
    const pred = (g) => {
      const lut = gammaLut(g);
      let mean = 0;
      for (let v = 0; v < 256; v++) mean += lut[v] * hist[v];
      mean /= total;
      const mid = (82.0 + 179.0 * 2) / 3.0;
      return Math.max(0, Math.min(1, (mean - mid * 0.07) / 255));
    };
    let lo = gMin, hi = gMax;
    for (let i = 0; i < 24; i++) {
      const mid = (lo + hi) / 2;
      if (pred(mid) > target) lo = mid; else hi = mid;
    }
    return (lo + hi) / 2;
  }

  function einkDither(ctx, w, h) {
    const imgd = ctx.getImageData(0, 0, w, h), d = imgd.data;
    const hist = new Array(256).fill(0);
    for (let i = 0; i < d.length; i += 4)
      hist[Math.min(255, Math.round((d[i] * 299 + d[i + 1] * 587 + d[i + 2] * 114) / 1000))]++;
    const lut = gammaLut(chooseGamma(hist));
    const buf = new Float32Array(d.length);
    for (let i = 0; i < d.length; i += 4) {
      buf[i] = lut[d[i]]; buf[i + 1] = lut[d[i + 1]]; buf[i + 2] = lut[d[i + 2]];
    }
    const nearest = (r, g, b) => {
      let bi = 0, bd = Infinity;
      for (let p = 0; p < 4; p++) {
        const dr = r - PALETTE[p][0], dg = g - PALETTE[p][1], db = b - PALETTE[p][2];
        const dist = dr * dr + dg * dg + db * db;
        if (dist < bd) { bd = dist; bi = p; }
      }
      return bi;
    };
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        const i = (y * w + x) * 4;
        const pi = nearest(buf[i], buf[i + 1], buf[i + 2]);
        const pc = PALETTE[pi];
        const er = buf[i] - pc[0], eg = buf[i + 1] - pc[1], eb = buf[i + 2] - pc[2];
        d[i] = pc[0]; d[i + 1] = pc[1]; d[i + 2] = pc[2]; d[i + 3] = 255;
        const push = (xx, yy, fr) => {
          if (xx < 0 || xx >= w || yy >= h) return;
          const j = (yy * w + xx) * 4;
          buf[j] += er * fr; buf[j + 1] += eg * fr; buf[j + 2] += eb * fr;
        };
        push(x + 1, y, 7 / 16); push(x - 1, y + 1, 3 / 16);
        push(x, y + 1, 5 / 16); push(x + 1, y + 1, 1 / 16);
      }
    }
    ctx.putImageData(imgd, 0, 0);
  }

  /* ---------- 主入口 ---------- */
  function render(canvas, style, item, photo, opts) {
    opts = opts || {};
    const ctx = canvas.getContext("2d");
    const [w, h] = opts.canvasSize;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = "#fff"; ctx.fillRect(0, 0, w, h);
    ctx.textAlign = "left"; ctx.textBaseline = "alphabetic";
    ctx.imageSmoothingEnabled = true; ctx.imageSmoothingQuality = "high";
    if (photo) {
      const s = Math.max(w / photo.width, h / photo.height);
      const dw = Math.max(w, photo.width * s), dh = Math.max(h, photo.height * s);
      ctx.drawImage(photo, (w - dw) / 2, (h - dh) / 2, dw, dh);
    }
    const peers = {};
    for (const el of style.elements || [])
      if (el.kind === "seal") peers[el.name || ""] = !!resolveSlot(el, item);
    const boxes = [];
    (style.elements || []).forEach((el, i) => {
      switch (el.kind) {
        case "glass": drawGlass(ctx, el, photo); break;
        case "veil": drawVeil(ctx, el, opts.veilAlphaDefault ?? 216); break;
        case "text": drawTextEl(ctx, el, item, peers); break;
        case "vtext": drawVTextEl(ctx, el, item); break;
        case "seal": drawSeal(ctx, el, item); break;
        case "bar": drawBar(ctx, el); break;
        case "frame": drawFrame(ctx, el, w, h); break;
        case "badge": drawBadge(ctx, el, item); break;
      }
      const b = elBBox(ctx, el, item);
      if (b) boxes.push({ i, ...b, kind: el.kind });
    });
    if (opts.eink) einkDither(ctx, w, h);
    /* 警戒线在 eink 之后绘制：它属于编辑器 UI 层，不应被四色量化。
     * 分辨率语义：guideCfg.vw×vh = 可视区像素尺寸（按当前画布朝向已换算），
     * 遮挡 = 画布与可视区之差，只在对应两边出现（与真实相框行为一致）。 */
    const g = opts.guideCfg;
    if (g && g.mode !== "off" && g.vw > 0 && g.vh > 0) {
      const mx = Math.max(0, (w - g.vw) / 2), my = Math.max(0, (h - g.vh) / 2);
      if (mx > 0 || my > 0) {
        const r2 = Math.max(0, Math.min(g.radius || 0, g.vw / 2, g.vh / 2));
        if (g.mode === "mask" || g.mode === "both") {
          ctx.save();
          ctx.fillStyle = "rgba(0,0,0,0.42)";
          ctx.beginPath();
          ctx.rect(0, 0, w, h);                       /* 外框：全画布 */
          roundRect(ctx, mx, my, w - mx, h - my, r2, true);   /* 内框：可视区 */
          ctx.fill("evenodd");
          ctx.restore();
        }
        ctx.save();
        ctx.strokeStyle = "rgba(220,180,0,.95)";
        ctx.lineWidth = g.mode === "mask" ? 1.5 : 2;
        if (g.mode === "dash") ctx.setLineDash([6, 5]);
        roundRect(ctx, mx, my, w - mx, h - my, r2);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = "rgba(220,180,0,.95)";
        ctx.font = "12px sans-serif";
        ctx.textAlign = "center";
        ctx.fillText(`可视区 ${Math.round(g.vw)}×${Math.round(g.vh)}`, w / 2, Math.max(14, my - 6));
        ctx.textAlign = "left";
        ctx.restore();
      }
    }
    return boxes;
  }

  return { render, resolveSlot, sealHexesFor, wrapLines, fitText,
           formatDateDisplay, weekdayCn, PALETTE };
})();
