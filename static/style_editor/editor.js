/* InkTime 样式编辑器 · 交互逻辑（无构建，原生 JS） */
"use strict";

/* ---------- 状态 ---------- */
let meta = null, stylesList = [];
let style = null, savedJsonText = "", item = {};
let photoImg = null, photoUrl = "";
let boxes = [], selected = -1;
let undoStack = [], redoStack = [];

const $ = id => document.getElementById(id);
const cv = $("cv");
const KIND_LABEL = {glass: "毛玻璃卡", veil: "渐变底托", text: "横排文字", vtext: "竖排文字",
                    seal: "钤印", bar: "饰杠", frame: "边框", badge: "角标徽章"};
const KIND_ICON = {glass: "🪟", veil: "🌫", text: "T", vtext: "ㄧ", seal: "🖼", bar: "▬", frame: "▢", badge: "⬤"};

function clone(o) { return JSON.parse(JSON.stringify(o)); }
function styleJsonText() { return JSON.stringify(style, null, 2); }
function isDirty() { return style && styleJsonText() !== savedJsonText; }
function canvasSize() {
  const o = style ? style.orientation : "portrait";
  return meta.canvas[o];
}
function pushUndo() {
  undoStack.push(clone(style));
  if (undoStack.length > 80) undoStack.shift();
  redoStack = [];
}

/* ---------- 初始化 ---------- */
async function init() {
  document.addEventListener("style-render", renderAll);
  const today = new Date();
  item = {side: "山间的清晨雾气还未散去",
          date: `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}-${String(today.getDate()).padStart(2, "0")}`,
          city: "厦门市"};
  meta = await (await fetch("/api/style_meta")).json();
  await refreshStyles();
  await refreshPhotos();

  $("itSide").value = item.side; $("itDate").value = item.date; $("itCity").value = item.city;
  ["itSide", "itCity"].forEach(id => $(id).addEventListener("input", e => {
    item[id === "itSide" ? "side" : "city"] = e.target.value; renderAll();
  }));
  $("itDate").addEventListener("change", e => { item.date = e.target.value; renderAll(); });
  document.querySelectorAll(".quicklen button").forEach(b => b.addEventListener("click", () => {
    const LENS = {empty: "", short: "短句测试",
                  mid: "十二个字的中文文案排版样本",
                  long: "这是一条三十个字左右的超长压测文案用来检验折行与省略号截断的边界行为再加几个字"};
    item.side = LENS[b.dataset.len]; $("itSide").value = item.side; renderAll();
  }));

  $("tgEink").addEventListener("change", renderAll);
  $("tgGuide").addEventListener("change", renderAll);
  bindGuideCfg();
  /* 属性面板几何输入 hover → 画布高亮对应边/参考线 */
  $("props").addEventListener("mouseover", e => {
    const edge = e.target && e.target.dataset ? e.target.dataset.edge : null;
    if (edge && edge !== hoverEdge) { hoverEdge = edge; renderAll(); }
  });
  $("props").addEventListener("mouseout", e => {
    if (e.target && e.target.dataset && e.target.dataset.edge) {
      hoverEdge = null; renderAll();
    }
  });
  $("btnFrameState").addEventListener("click", loadFrameState);
  $("btnUndo").addEventListener("click", () => doUndo());
  $("btnRedo").addEventListener("click", () => doRedo());
  $("btnSave").addEventListener("click", saveFlow);
  $("btnGroundTruth").addEventListener("click", showGroundTruth);
  $("btnNew").addEventListener("click", newStyleDialog);
  $("btnDup").addEventListener("click", async () => {
    if (!style) return;
    /* 先登记新 ID，再把当前画布状态（含未保存修改）整体写入——
     * "另存为新样式"复制的是所见状态，不是磁盘上的旧版本 */
    const doc = await api("/api/styles", "POST",
        {orientation: style.orientation, base: style.id});
    const carry = { ...clone(style), id: doc.id, name: style.name + " 副本" };
    await api(`/api/styles/${doc.id}`, "PUT", carry);
    await refreshStyles();
    await loadStyle(doc.id);
  });
  $("btnDel").addEventListener("click", async () => {
    if (!style) return;
    if (!confirm(`确定删除 ${style.id}「${style.name}」？此操作直接删文件，回滚靠 git。`)) return;
    await api(`/api/styles/${style.id}`, "DELETE");
    await refreshStyles();
    const first = stylesList.find(s => s.orientation === style.orientation) || stylesList[0];
    if (first) await loadStyle(first.id); else { style = null; renderAll(); }
  });
  $("oriFilter").addEventListener("change", rebuildStyleList);
  $("btnUpload").addEventListener("click", () => $("photoUp").click());
  $("photoUp").addEventListener("change", async e => {
    const f = e.target.files[0]; if (!f) return;
    const fd = new FormData(); fd.append("photo", f);
    const r = await fetch("/api/upload_photo", {method: "POST", body: fd});
    const j = await r.json();
    addPhotoOption("📷 " + f.name.slice(0, 18), j.url, true);
    await setPhoto(j.url);
    e.target.value = "";
  });
  $("photoSel").addEventListener("change", e => setPhoto(e.target.value));

  bindCanvasEvents();
  bindKeyboard();
  window.addEventListener("beforeunload", e => {
    if (isDirty()) { e.preventDefault(); e.returnValue = ""; }
  });
  document.fonts.ready.then(renderAll);

  const want = (location.hash || "").replace("#", "").toUpperCase();
  const first = stylesList.find(s => s.id === want) || stylesList.find(s => s.id === "P05") || stylesList[0];
  if (first) await loadStyle(first.id);
}

async function api(url, method, body) {
  const r = await fetch(url, {method, headers: {"Content-Type": "application/json"},
                              body: body ? JSON.stringify(body) : undefined});
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

/* ---------- 版式列表 ---------- */
async function refreshStyles() {
  stylesList = await (await fetch("/api/styles")).json();
  rebuildStyleList();
}
function rebuildStyleList() {
  const f = $("oriFilter").value;
  const ul = $("styleList"); ul.innerHTML = "";
  stylesList.filter(s => f === "all" || s.orientation === f).forEach(s => {
    const li = document.createElement("li");
    if (style && s.id === style.id) li.className = "current";
    li.innerHTML = `<span>${s.id}</span><span class="nm">${esc(s.name)}</span>
                    <span class="tag">${s.orientation === "portrait" ? "竖" : "横"}·${s.elements}素</span>`;
    li.addEventListener("click", async () => {
      if (isDirty() && !confirm("当前版式未保存，切换将丢弃修改。继续？")) return;
      await loadStyle(s.id);
    });
    ul.appendChild(li);
  });
}
async function loadStyle(sid) {
  style = await (await fetch(`/api/styles/${sid}`)).json();
  savedJsonText = styleJsonText();
  selected = -1; undoStack = []; redoStack = [];
  const [w, h] = canvasSize();
  cv.width = w; cv.height = h;
  /* 照片朝向适配只对压力照片组生效（_p/_l 成对）；真实照片/上传照片保持不动 */
  const suffix = style.orientation === "portrait" ? "_p" : "_l";
  if (photoUrl && photoUrl.includes("/stress/") && !photoUrl.endsWith(suffix + ".jpg")) {
    const want = photoUrl.replace(/_[pl]\.jpg$/, suffix + ".jpg");
    addPhotoOption(labelOf(want.split("/").pop().replace(/\.jpg$/, "")), want, true);
    await setPhoto(want);
  }
  history.replaceState(null, "", `#${sid}`);
  refreshAll();
  updateCroppedPhoto();          /* 避让区随版式变，取景要跟着重裁 */
}
function refreshAll() {
  renderAll(); rebuildStyleList(); refreshStyleProps(); refreshLayers(); refreshProps();
}

/* ---------- 照片 ---------- */
async function refreshPhotos() {
  const list = await (await fetch("/api/stress_photos")).json();
  const sel = $("photoSel");
  list.forEach(p => addPhotoOption(labelOf(p.name), p.url, false));
  const warm = [...sel.options].find(o => o.value.includes("warm_p"));
  if (warm) { sel.value = warm.value; await setPhoto(warm.value); }
}
function labelOf(name) {
  const ZH = {bright: "亮底", dark: "暗底", gray: "中灰", noise: "彩噪", warm: "暖渐变", cool: "冷渐变"};
  const m = name.match(/^(.*)_[pl]$/);
  return m ? `${ZH[m[1]] || m[1]}·${name.endsWith("_p") ? "竖" : "横"}` : name;
}
function addPhotoOption(label, url, selected) {
  const sel = $("photoSel");
  if ([...sel.options].some(o => o.value === url)) { sel.value = url; return; }
  const o = document.createElement("option");
  o.textContent = label; o.value = url;
  sel.appendChild(o);
  if (selected) sel.value = url;
}
/* ---------- 照片 ----------
 * 渲染用底图必须走真实管线的裁切（/api/style_photo → _cover_crop，
 * 含版式避让区 + 主体信号），否则预览取景/缩放与真机不一致。
 * 裁切请求期间先用原图兜底渲染。 */
let croppedPhoto = null, cropKey = "";
const cropCache = new Map();

function currentCropKey() {
  const cr = style ? (style.crop_reserve || {}) : {};
  return JSON.stringify([photoUrl, style ? style.orientation : "",
                         cr, item.subjects_json || ""]);
}

async function updateCroppedPhoto() {
  if (!photoUrl || !style || !meta) return;
  const key = currentCropKey();
  if (key === cropKey) return;
  cropKey = key;
  if (cropCache.has(key)) {
    croppedPhoto = cropCache.get(key);
    renderAll();
    return;
  }
  const [w, h] = canvasSize();
  try {
    const r = await fetch("/api/style_photo", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({photo: photoUrl, orientation: style.orientation,
                            crop_reserve: style.crop_reserve || {},
                            subjects_json: item.subjects_json || ""})});
    if (!r.ok) throw new Error(await r.text());
    const bmp = await createImageBitmap(await r.blob());
    const cvn = document.createElement("canvas");
    cvn.width = w; cvn.height = h;
    cvn.getContext("2d").drawImage(bmp, 0, 0);
    if (cropCache.size > 24) cropCache.delete(cropCache.keys().next().value);
    cropCache.set(key, cvn);
    if (currentCropKey() === key) {   /* 期间参数没再变才应用 */
      croppedPhoto = cvn;
      renderAll();
    }
  } catch (e) {
    console.warn("style_photo 裁切失败，退回原图居中:", e);
  }
}

async function setPhoto(url) {
  photoUrl = url;
  croppedPhoto = null; cropKey = "";
  photoImg = await new Promise(res => {
    const im = new Image();
    im.onload = () => res(im); im.onerror = () => res(null);
    im.src = url;
  });
  await updateCroppedPhoto();
  renderAll();
}

/* ---------- 警戒线设置（localStorage 记忆，v2 分辨率语义） ----------
 * 存竖屏基准的可视区尺寸（默认 3:2 = 480×720），横屏画布自动宽高互换
 * （720×480）。遮挡只出现在画布与可视区之差对应的两边，与真实相框一致。 */
const DEFAULT_GUIDE = {mode: "mask", vw: 480, vh: 720, radius: 0};
function guideCfg() {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem("seGuideCfg2") || "{}"); } catch (e) {}
  const cfg = {...DEFAULT_GUIDE, ...saved};
  cfg.vw = Math.round(+cfg.vw || 480);
  cfg.vh = Math.round(+cfg.vh || 720);
  if (style && style.orientation === "landscape") {
    return {...cfg, vw: cfg.vh, vh: cfg.vw};   /* 横屏：宽高互换 */
  }
  return cfg;
}
function bindGuideCfg() {
  $("tgGuide").checked = localStorage.getItem("seGuideOn") !== "0";
  $("tgGuide").addEventListener("change", e => {
    localStorage.setItem("seGuideOn", e.target.checked ? "1" : "0");
    renderAll();
  });
  $("btnGuideCfg").addEventListener("click", async () => {
    const cur = guideCfg();
    const ok = await modal(`
      <h2>⚙ 相框可视区设置</h2>
      <div class="frm" style="min-width:340px">
        <label>显示模式</label><select id="gcMode">
          <option value="mask">半透明遮罩（区外加暗）</option>
          <option value="dash">虚线框</option>
          <option value="both">遮罩 + 虚线</option>
        </select>
        <label>可视区宽</label><input type="number" id="gcVw" min="120" max="800" value="${cur.vw}">
        <label>可视区高</label><input type="number" id="gcVh" min="120" max="800" value="${cur.vh}">
        <label>圆角</label><input type="number" id="gcRadius" min="0" max="80" value="${cur.radius}">
        <div class="full row" style="grid-column:1/-1">
          <button type="button" id="gcP32">3:2 相框（480×720）</button>
          <button type="button" id="gcP43">4:3（480×640）</button>
          <button type="button" id="gcPFull">全屏无遮挡</button>
        </div>
      </div>
      <div class="tips">尺寸为竖屏基准（画布 480×800），横屏自动互换（720×480）。
        遮挡只出现在画布比可视区多出的两边，与真实相框一致；仅影响编辑器显示，
        不改变渲染输出；设置存浏览器本地。</div>`,
      [["取消", "cancel"], ["应用", "primary"]]);
    if (!ok) return;
    localStorage.setItem("seGuideCfg2", JSON.stringify({
      mode: ok.values.gcMode, vw: +ok.values.gcVw || 480,
      vh: +ok.values.gcVh || 720, radius: +ok.values.gcRadius || 0}));
    renderAll();
  });
  /* 弹层内的预设按钮（事件委托，弹层销毁重建亦生效） */
  $("modalBack").addEventListener("click", e => {
    const t = e.target;
    if (t.id === "gcP32") { $("gcVw").value = 480; $("gcVh").value = 720; }
    if (t.id === "gcP43") { $("gcVw").value = 480; $("gcVh").value = 640; }
    if (t.id === "gcPFull") { $("gcVw").value = 480; $("gcVh").value = 800; }
  });
}
function guideOpts() {
  const on = $("tgGuide").checked;
  const cfg = guideCfg();
  if (!on) return {mode: "off", inset: 0, radius: 0};
  return cfg;
}

/* ---------- 渲染 ---------- */
function renderAll() {
  if (!meta || !style) return;
  boxes = Renderer.render(cv, style, item, croppedPhoto || photoImg, {
    canvasSize: canvasSize(),
    veilAlphaDefault: meta.veil_alpha,
    guideCfg: guideOpts(),
    eink: $("tgEink").checked,
  });
  drawSelection();
  updateSelInfo();
  /* 溢出提示（三）：随每次渲染更新的固定警告条（重建 props 会丢输入焦点，故独立于它） */
  const ovEl = $("ovWarn");
  if (ovEl) {
    const bx = boxes.find(x => x.i === selected);
    const ov = bx ? overflows(bx) : null;
    ovEl.hidden = !ov;
    if (ov) ovEl.textContent = `⚠ 超出可视区：${overflowText(ov)}px（相框遮挡后不可见）`;
  }
  $("btnUndo").disabled = !undoStack.length;
  $("btnRedo").disabled = !redoStack.length;
  $("btnSave").disabled = !isDirty();
  const badge = $("styleBadge");
  badge.textContent = `${style.id}「${style.name}」${style.orientation === "portrait" ? "竖屏" : "横屏"}`;
  badge.classList.toggle("dirty", isDirty());
}
/* 属性面板几何输入 hover 高亮的边（x0/x1/y0/y1/x/y/cx） */
let hoverEdge = null;

/* 可视区（按警戒线设置计算）与元素溢出检测 */
function visibleRect() {
  const g = guideCfg();
  const [W, H] = canvasSize();
  const mx = Math.max(0, (W - g.vw) / 2), my = Math.max(0, (H - g.vh) / 2);
  return {x0: mx, y0: my, x1: W - mx, y1: H - my};
}
function overflows(b) {
  const v = visibleRect();
  const o = {};
  if (b.x0 < v.x0 - 0.5) o.l = Math.round(v.x0 - b.x0);
  if (b.x1 > v.x1 + 0.5) o.r = Math.round(b.x1 - v.x1);
  if (b.y0 < v.y0 - 0.5) o.t = Math.round(v.y0 - b.y0);
  if (b.y1 > v.y1 + 0.5) o.b = Math.round(b.y1 - v.y1);
  return Object.keys(o).length ? o : null;
}
function overflowText(o) {
  const m = {l: "左", r: "右", t: "上", b: "下"};
  return Object.keys(o).map(k => m[k] + o[k]).join(" ");
}

function drawSelection() {
  const b = boxes.find(x => x.i === selected);
  const ctx = cv.getContext("2d");
  ctx.save();
  /* 溢出提示（一）：未选中但超出可视区的元素 → 红色虚线框 */
  const v = visibleRect();
  for (const bx of boxes) {
    if (bx.i === selected || !overflows(bx)) continue;
    ctx.strokeStyle = "rgba(200,0,0,.7)"; ctx.lineWidth = 1.5;
    ctx.setLineDash([4, 4]);
    ctx.strokeRect(bx.x0, bx.y0, bx.x1 - bx.x0, bx.y1 - bx.y0);
    ctx.setLineDash([]);
  }
  /* hover 边参考线：竖线（x0/x1/x/cx）或横线（y0/y1/y）+ 数值标签 */
  if (hoverEdge) {
    const el = style.elements[selected];
    const [W, H] = canvasSize();
    let vert = null, hori = null;
    const val = v => (v === undefined || v === null) ? "" : Math.round(v);
    if (el) {
      if (el.rect) {
        if (hoverEdge === "x") vert = [el.rect[0], `x=${val(el.rect[0])}`];
        if (hoverEdge === "y") hori = [el.rect[1], `y=${val(el.rect[1])}`];
        if (hoverEdge === "w") vert = [el.rect[2], `x1=${val(el.rect[2])}`];
        if (hoverEdge === "h") hori = [el.rect[3], `y1=${val(el.rect[3])}`];
      } else {
        if (hoverEdge === "x") vert = [el.x, `x=${val(el.x)}`];
        if (hoverEdge === "y") hori = [el.y, `y=${val(el.y)}`];
        if (hoverEdge === "cx") vert = [el.cx, `cx=${val(el.cx)}`];
      }
    }
    const line = (x1, y1, x2, y2) => {
      ctx.strokeStyle = "rgba(255,122,0,.9)"; ctx.lineWidth = 1.5;
      ctx.setLineDash([10, 6]);
      ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
      ctx.setLineDash([]);
    };
    const tag = (x, y, s) => {
      ctx.font = "12px Consolas, monospace";
      const tw = ctx.measureText(s).width;
      ctx.fillStyle = "rgba(0,0,0,.72)";
      ctx.fillRect(x - 2, y - 13, tw + 6, 17);
      ctx.fillStyle = "#ffb266";
      ctx.fillText(s, x + 1, y);
    };
    if (vert) { line(vert[0], 0, vert[0], H); tag(Math.min(vert[0] + 4, W - 70), 16, vert[1]); }
    if (hori) { line(0, hori[0], W, hori[0]); tag(8, Math.max(16, hori[0] - 6), hori[1]); }
  }
  if (!b) { ctx.restore(); return; }
  ctx.strokeStyle = "#dcb400"; ctx.lineWidth = 2; ctx.setLineDash([7, 5]);
  ctx.strokeRect(b.x0 - 2, b.y0 - 2, b.x1 - b.x0 + 4, b.y1 - b.y0 + 4);
  ctx.setLineDash([]);
  ctx.fillStyle = "#dcb400";
  ctx.fillRect(b.x1 - 4, b.y1 - 4, 10, 10);
  /* 选中元素的溢出红罩（几何/坐标提示已移至画布下方 #canvasHud，不再叠加照片） */
  const el = style.elements[selected];
  if (el) {
    const ov = overflows(b);
    if (ov) {
      ctx.fillStyle = "rgba(200,0,0,.30)";
      const iy0 = Math.max(b.y0, v.y0), iy1 = Math.min(b.y1, v.y1);
      const ix0 = Math.max(b.x0, v.x0), ix1 = Math.min(b.x1, v.x1);
      if (ov.l) ctx.fillRect(b.x0, iy0, v.x0 - b.x0, iy1 - iy0);
      if (ov.r) ctx.fillRect(v.x1, iy0, b.x1 - v.x1, iy1 - iy0);
      if (ov.t) ctx.fillRect(ix0, b.y0, ix1 - ix0, v.y0 - b.y0);
      if (ov.b) ctx.fillRect(ix0, v.y1, ix1 - ix0, b.y1 - v.y1);
    }
  }
  ctx.restore();
}
/* 画布下方 HUD：选中元素几何 + 溢出提示（renderAll 时刷新） */
function updateSelInfo() {
  const info = $("selInfo");
  if (!info) return;
  const el = style && style.elements[selected];
  if (!el) { info.textContent = "未选中元素"; return; }
  let geo;
  if (el.rect) {
    const w = Math.round(el.rect[2] - el.rect[0]), h = Math.round(el.rect[3] - el.rect[1]);
    geo = `x${Math.round(el.rect[0])} y${Math.round(el.rect[1])} · ${w}×${h}`;
  } else {
    geo = `x${Math.round(el.x)} y${Math.round(el.y)}`
        + (el.cx !== undefined && el.cx !== null ? ` · cx${Math.round(el.cx)}` : "");
  }
  const bx = boxes.find(x => x.i === selected);
  const ov = bx ? overflows(bx) : null;
  info.innerHTML = `📍 ${esc(el.name || el.kind)}（${el.kind}）<span class="mono">${geo}</span>`
    + (ov ? `<span class="ov"> · ⚠超 ${overflowText(ov)}px</span>` : "");
  const hud = document.getElementById("canvasHud");
  if (hud) hud.style.setProperty("--hudw", `${cv.getBoundingClientRect().width}px`);
}

/* ---------- 画布交互 ---------- */
let drag = null;
function canvasXY(e) {
  const r = cv.getBoundingClientRect();
  return {x: (e.clientX - r.left) * (cv.width / r.width),
          y: (e.clientY - r.top) * (cv.height / r.height)};
}
function bindCanvasEvents() {
  cv.addEventListener("pointerdown", e => {
    if (!style) return;
    const {x, y} = canvasXY(e);
    const b = [...boxes].reverse().find(bb => x >= bb.x0 - 3 && x <= bb.x1 + 3 && y >= bb.y0 - 3 && y <= bb.y1 + 3);
    selected = b ? b.i : -1;
    if (b) {
      pushUndo();
      const el = style.elements[b.i];
      const resizable = ["glass", "veil", "bar", "seal", "badge"].includes(el.kind);
      const nearBR = resizable && Math.abs(x - b.x1) < 10 && Math.abs(y - b.y1) < 10;
      drag = {x0: x, y0: y, el, mode: nearBR ? "resize" : "move",
              orig: clone(el)};
      try { cv.setPointerCapture(e.pointerId); } catch (err) { /* 合成事件无活跃指针 */ }
    }
    renderAll(); refreshLayers(); refreshProps();
  });
  cv.addEventListener("pointermove", e => {
    if (!drag) return;
    const {x, y} = canvasXY(e);
    const dx = Math.round(x - drag.x0), dy = Math.round(y - drag.y0);
    const el = drag.el, o = drag.orig;
    if (drag.mode === "move") {
      if (el.rect) el.rect = [o.rect[0] + dx, o.rect[1] + dy, o.rect[2] + dx, o.rect[3] + dy];
      else {
        el.x = o.x + dx; el.y = o.y + dy;
        if (o.cx !== undefined && o.cx !== null) el.cx = o.cx + dx;
      }
    } else if (el.rect) {
      el.rect = [o.rect[0], o.rect[1], Math.max(o.rect[0] + 12, o.rect[2] + dx),
                 Math.max(o.rect[1] + 12, o.rect[3] + dy)];
    } else if (el.kind === "seal") {
      el.w = Math.max(12, o.w + dx); el.h = Math.max(12, o.h + dy);
    } else if (el.kind === "badge") {
      el.r = Math.max(8, o.r + Math.round((dx + dy) / 2));
    }
    renderAll(); refreshProps();
  });
  cv.addEventListener("pointerup", () => { drag = null; refreshLayers(); });
  cv.addEventListener("dblclick", () => {
    const first = document.querySelector("#props input, #props textarea, #props select");
    if (first) first.focus();
  });

  /* 元素库拖放 */
  cv.addEventListener("dragover", e => e.preventDefault());
  cv.addEventListener("drop", e => {
    e.preventDefault();
    const kind = e.dataTransfer.getData("text/kind");
    if (!kind) return;
    const {x, y} = canvasXY(e);
    addElement(kind, x, y);
  });
}

function bindKeyboard() {
  document.addEventListener("keydown", e => {
    if (!style || ["INPUT", "TEXTAREA", "SELECT"].includes(e.target.tagName)) return;
    const el = style.elements[selected];
    const k = e.key.toLowerCase();
    if ((e.ctrlKey || e.metaKey) && k === "z") { e.preventDefault(); e.shiftKey ? doRedo() : doUndo(); return; }
    if ((e.ctrlKey || e.metaKey) && k === "y") { e.preventDefault(); doRedo(); return; }
    if ((e.ctrlKey || e.metaKey) && k === "d" && el) { e.preventDefault(); duplicateSelected(); return; }
    if (!el) return;
    if (e.key === "Delete" || e.key === "Backspace") {
      e.preventDefault(); pushUndo();
      style.elements.splice(selected, 1); selected = -1;
      refreshAll(); return;
    }
    const step = e.shiftKey ? 10 : 1;
    let handled = true;
    if (e.key === "ArrowLeft") nudge(el, -step, 0);
    else if (e.key === "ArrowRight") nudge(el, step, 0);
    else if (e.key === "ArrowUp") nudge(el, 0, -step);
    else if (e.key === "ArrowDown") nudge(el, 0, step);
    else handled = false;
    if (handled) { e.preventDefault(); renderAll(); refreshProps(); }
  });
}
function nudge(el, dx, dy) {
  if (el.rect) el.rect = [el.rect[0] + dx, el.rect[1] + dy, el.rect[2] + dx, el.rect[3] + dy];
  else { el.x += dx; el.y += dy; if (el.cx !== undefined && el.cx !== null) el.cx += dx; }
}
function duplicateSelected() {
  pushUndo();
  const copy = clone(style.elements[selected]);
  copy.name = uniqueName(copy.name);
  if (copy.rect) copy.rect = copy.rect.map((v, i) => i < 2 ? v + 16 : v + 16);
  else { copy.x += 16; copy.y += 16; if (copy.cx !== undefined && copy.cx !== null) copy.cx += 16; }
  style.elements.splice(selected + 1, 0, copy);
  selected += 1;
  refreshAll();
}

/* ---------- 元素库 ---------- */
const PRESETS = {
  glass:  {label: "毛玻璃卡", rect: [40, 60, 440, 250], radius: 14, alpha: 184, blur: 12, shadow: false, highlight: true},
  veil:   {label: "渐变底托", rect: [0, 430, 480, 800], a0: 0, a1: null, ramp_px: 170, rgb: [246, 241, 231]},
  text:   {label: "横排文字", slot: "side", x: 40, y: 600, size: 24, fill: [0, 0, 0], stroke: 0.3, bold: false, align: "l",
           wrap: {max_w: 400, max_lines: 2, line_step: 34}},
  vtext:  {label: "竖排文字", slot: "side", x: 420, y: 64, size: 24, fill: [0, 0, 0], col_h: 280, pitch: 31, col_gap: 31, max_cols: 2},
  seal:   {label: "钤印", slot: "city", x: 370, y: 360, w: 72, h: 44, vertical: false, glyph_px: 28, radius: 6},
  meta:   {label: "日期行", kind: "text", slot: "meta_wd_city", x: 28, y: 738, size: 18, fill: [96, 90, 82], stroke: 0, bold: false, align: "l"},
  bar:    {label: "饰杠", rect: [40, 120, 130, 124], fill: [200, 0, 0]},
  frame:  {label: "边框", margin: 14, stroke_w: 2, fill: [255, 255, 255], double: false},
  badge:  {label: "角标徽章", slot: "static", static_text: "印", x: 64, y: 64, r: 30, fill: [200, 0, 0], text_fill: [255, 255, 255], size: 22},
};
function buildPresets() {
  const box = $("presets"); box.innerHTML = "";
  Object.entries(PRESETS).forEach(([key, p]) => {
    const kind = p.kind || key;
    const div = document.createElement("div");
    div.className = "preset"; div.draggable = true;
    div.innerHTML = `<span class="ic">${KIND_ICON[kind]}</span>${p.label}`;
    div.addEventListener("dragstart", e => e.dataTransfer.setData("text/kind", key));
    div.addEventListener("click", () => addElement(key));
    box.appendChild(div);
  });
}
function uniqueName(base) {
  let n = 2;
  const names = new Set((style.elements || []).map(e => e.name));
  while (names.has(`${base} ${n}`)) n++;
  return `${base} ${n}`;
}
function addElement(key, x, y) {
  if (!style) return;
  pushUndo();
  const p = clone(PRESETS[key]);
  const kind = p.kind || key;
  delete p.label; delete p.kind;
  const el = {kind, name: uniqueName(KIND_LABEL[kind]), ...p};
  if (x !== undefined && y !== undefined) {
    if (el.rect) {
      const w = el.rect[2] - el.rect[0], h = el.rect[3] - el.rect[1];
      el.rect = [Math.round(x - w / 2), Math.round(y - h / 2), Math.round(x + w / 2), Math.round(y + h / 2)];
    } else { el.x = Math.round(x); el.y = Math.round(y); }
  }
  style.elements.push(el);
  selected = style.elements.length - 1;
  refreshAll();
}

/* ---------- 层级列表 ---------- */
function refreshLayers() {
  const ol = $("layers"); ol.innerHTML = "";
  if (!style) return;
  [...style.elements].reverse().forEach(el => {
    const i = style.elements.indexOf(el);
    const li = document.createElement("li");
    if (i === selected) li.classList.add("sel");
    li.draggable = true;
    const ovMark = (boxes.find(x => x.i === i) && overflows(boxes.find(x => x.i === i)))
      ? '<span style="color:#ff6b6b" title="超出可视区">⚠</span>' : "";
    li.innerHTML = `<span>${KIND_ICON[el.kind] || "?"}</span>${ovMark}<span>${esc(el.name || "")}</span>
                    <span class="kd">${el.kind}</span><span class="del" title="删除">✕</span>`;
    li.addEventListener("click", e => {
      if (e.target.classList.contains("del")) {
        pushUndo(); style.elements.splice(i, 1);
        if (selected === i) selected = -1;
        refreshAll(); return;
      }
      selected = i; renderAll(); refreshLayers(); refreshProps();
    });
    li.addEventListener("dragstart", e => e.dataTransfer.setData("text/layer", String(i)));
    li.addEventListener("dragover", e => e.preventDefault());
    li.addEventListener("drop", e => {
      e.preventDefault();
      const from = parseInt(e.dataTransfer.getData("text/layer"), 10);
      if (isNaN(from) || from === i) return;
      pushUndo();
      const [m] = style.elements.splice(from, 1);
      style.elements.splice(i, 0, m);
      selected = i;
      refreshAll();
    });
    ol.appendChild(li);
  });
}

/* ---------- 属性面板 ---------- */
function refreshStyleProps() {
  const box = $("styleProps"); box.innerHTML = "";
  if (!style) return;
  const name = document.createElement("input");
  name.type = "text"; name.value = style.name;
  name.addEventListener("input", () => { style.name = name.value; renderAll(); });
  addRow(box, "名称", name);
  const ro = document.createElement("input");
  ro.type = "text"; ro.value = `${style.id} · ${style.orientation === "portrait" ? "竖屏 480×800" : "横屏 800×480"}`;
  ro.disabled = true;
  addRow(box, "ID/朝向", ro);
  const cr = style.crop_reserve || (style.crop_reserve = {});
  ["top", "bottom", "left", "right"].forEach(k => {
    const inp = document.createElement("input");
    inp.type = "number"; inp.value = cr[k] ?? 0;
    inp.addEventListener("change", () => {
      const v = Math.max(0, parseInt(inp.value, 10) || 0);
      if (v) cr[k] = v; else delete cr[k];
      renderAll();
      updateCroppedPhoto();      /* 避让区变了，取景重裁 */
    });
    addRow(box, "避让 " + k, inp);
  });
}

function refreshProps() {
  const box = $("props"); box.innerHTML = "";
  const el = style && style.elements[selected];
  if (!el) { box.innerHTML = '<p class="empty">在画布或层级列表选中元素</p>'; return; }
  const frm = document.createElement("div"); frm.className = "frm";
  box.appendChild(frm);
  head(frm, `${KIND_ICON[el.kind]} ${el.kind}`);

  const nm = document.createElement("input"); nm.type = "text"; nm.value = el.name || "";
  nm.addEventListener("input", () => { el.name = nm.value; refreshLayers(); });
  addRow(frm, "名称", nm);

  if (el.rect) {
    rectField(frm, el);
  } else if (el.kind !== "frame") {
    num(frm, "x", () => el.x, v => el.x = v, 1, null, null, "x");
    num(frm, "y", () => el.y, v => el.y = v, 1, null, null, "y");
  }
  if (el.kind === "seal") { num(frm, "w", () => el.w, v => el.w = v); num(frm, "h", () => el.h, v => el.h = v); }
  if (el.kind === "badge") num(frm, "半径 r", () => el.r, v => el.r = v);

  switch (el.kind) {
    case "glass":
      num(frm, "圆角", () => el.radius ?? 14, v => el.radius = v);
      num(frm, "白不透明", () => el.alpha ?? 184, v => el.alpha = v, 1, 0, 255);
      num(frm, "模糊", () => el.blur ?? 12, v => el.blur = v);
      chk(frm, "外投影", () => el.shadow, v => el.shadow = v);
      chk(frm, "白高光边", () => el.highlight !== false, v => el.highlight = v);
      break;
    case "veil": {
      num(frm, "起始 alpha", () => el.a0 ?? 0, v => el.a0 = v, 1, 0, 255);
      const follow = document.createElement("input"); follow.type = "checkbox";
      follow.checked = el.a1 === null || el.a1 === undefined;
      follow.addEventListener("change", () => {
        el.a1 = follow.checked ? null : (meta.veil_alpha || 216);
        if (!follow.checked) a1Box.style.display = ""; else a1Box.style.display = "none";
        renderAll();
      });
      const a1Box = document.createElement("span");
      const a1in = document.createElement("input"); a1in.type = "number";
      a1in.value = el.a1 ?? meta.veil_alpha ?? 216;
      a1in.addEventListener("change", () => { el.a1 = Math.max(0, Math.min(255, +a1in.value || 0)); renderAll(); });
      a1in.style.width = "70px";
      a1Box.append("峰值 ", a1in);
      a1Box.style.display = follow.checked ? "none" : "";
      const wrap = document.createElement("span");
      wrap.append(follow, " 跟随全局 ", a1Box);
      addRow(frm, "峰值 alpha", wrap);
      num(frm, "渐变区高", () => el.ramp_px ?? 0, v => el.ramp_px = v);
      rgb(frm, "颜色", () => el.rgb || [246, 241, 231], v => el.rgb = v);
      break;
    }
    case "text":
      textFields(frm, el);
      break;
    case "vtext":
      slotField(frm, el);
      num(frm, "字号", () => el.size ?? 24, v => el.size = v);
      rgb(frm, "颜色", () => el.fill || [0, 0, 0], v => el.fill = v);
      num(frm, "列高", () => el.col_h, v => el.col_h = v);
      num(frm, "字距", () => el.pitch, v => el.pitch = v);
      num(frm, "列距", () => el.col_gap, v => el.col_gap = v);
      num(frm, "最大列数", () => el.max_cols ?? 2, v => el.max_cols = v);
      fontField(frm, el);
      break;
    case "seal":
      slotField(frm, el);
      num(frm, "字形px", () => el.glyph_px ?? 26, v => el.glyph_px = v);
      num(frm, "圆角", () => el.radius ?? 6, v => el.radius = v);
      chk(frm, "竖排", () => !!el.vertical, v => el.vertical = v);
      break;
    case "bar":
      rgb(frm, "颜色", () => el.fill || [200, 0, 0], v => el.fill = v);
      break;
    case "frame":
      num(frm, "线宽", () => el.stroke_w ?? 2, v => el.stroke_w = v);
      chk(frm, "双线", () => !!el.double, v => el.double = v);
      if (el.double) num(frm, "双线间距", () => el.double_gap ?? 4, v => el.double_gap = v);
      rgb(frm, "颜色", () => el.fill || [255, 255, 255], v => el.fill = v);
      if (!el.rect) num(frm, "贴边距", () => el.margin ?? 12, v => el.margin = v);
      break;
    case "badge":
      slotField(frm, el);
      if ((el.slot || "static") === "static")
        txt(frm, "文字", () => el.static_text || "", v => el.static_text = v);
      rgb(frm, "底色", () => el.fill || [200, 0, 0], v => el.fill = v);
      rgb(frm, "文字色", () => el.text_fill || [255, 255, 255], v => el.text_fill = v);
      num(frm, "字号", () => el.size ?? 16, v => el.size = v);
      fontField(frm, el);
      break;
  }
  const del = document.createElement("button");
  del.textContent = "🗑 删除元素"; del.className = "danger";
  del.addEventListener("click", () => {
    pushUndo(); style.elements.splice(selected, 1); selected = -1; refreshAll();
  });
  const row = document.createElement("div"); row.className = "full";
  row.appendChild(del); frm.appendChild(row);
}

function textFields(frm, el) {
  slotField(frm, el);
  if ((el.slot || "static") === "static")
    txt(frm, "文字", () => el.static_text || "", v => el.static_text = v);
  num(frm, "字号", () => el.size ?? 18, v => el.size = v);
  rgb(frm, "颜色", () => el.fill || [0, 0, 0], v => el.fill = v);
  num(frm, "描边宽", () => el.stroke ?? 0.3, v => el.stroke = v, 0.1);
  chk(frm, "加粗", () => !!el.bold, v => el.bold = v);
  const al = document.createElement("select");
  [["l", "左对齐"], ["c", "居中"], ["r", "右对齐"]].forEach(([v, t]) => {
    const o = document.createElement("option"); o.value = v; o.textContent = t;
    if ((el.align || "l") === v) o.selected = true; al.appendChild(o);
  });
  al.addEventListener("change", () => { el.align = al.value; renderAll(); });
  addRow(frm, "对齐", al);
  if (el.align && el.align !== "l") num(frm, "参考线 cx", () => el.cx ?? 0, v => el.cx = v, 1, null, null, "cx");
  fontField(frm, el);
  head(frm, "宽度策略");
  const mode = el.fit !== undefined && el.fit !== null ? "fit"
             : el.wrap ? "wrap" : "auto";
  const md = document.createElement("select");
  [["auto", "自适应（不限制）"], ["fit", "单行截断"], ["wrap", "折行"]].forEach(([v, t]) => {
    const o = document.createElement("option"); o.value = v; o.textContent = t;
    if (mode === v) o.selected = true; md.appendChild(o);
  });
  md.addEventListener("change", () => {
    if (md.value === "auto") { delete el.fit; delete el.wrap; }
    else if (md.value === "fit") { delete el.wrap; el.fit = el.fit ?? 400; }
    else { delete el.fit; el.wrap = el.wrap || {max_w: 400, max_lines: 2, line_step: 34}; }
    renderAll(); refreshProps();
  });
  addRow(frm, "模式", md);
  if (mode === "fit") num(frm, "最大宽", () => el.fit, v => el.fit = v);
  if (mode === "wrap") {
    num(frm, "行宽", () => el.wrap.max_w, v => el.wrap.max_w = v);
    num(frm, "最大行数", () => el.wrap.max_lines, v => el.wrap.max_lines = v);
    num(frm, "行距", () => el.wrap.line_step, v => el.wrap.line_step = v);
  }
  if (el.peer_pad) {
    head(frm, "让位规则 peer_pad");
    txt(frm, "对齐元素", () => el.peer_pad.peer, v => el.peer_pad.peer = v);
    num(frm, "dx_cx", () => el.peer_pad.dx_cx ?? 0, v => el.peer_pad.dx_cx = v);
    num(frm, "dx_fit", () => el.peer_pad.dx_fit ?? 0, v => el.peer_pad.dx_fit = v);
  }
}

const SLOTS = [["side", "旁白 side"], ["city", "城市"], ["date_full", "日期 2026.9.24"],
               ["date_year", "年 2026"], ["date_rest", "月日 9.24"], ["weekday", "星期"],
               ["date_wd", "日期·星期"], ["meta_wd_city", "日期·星期·城市"],
               ["meta_city", "日期·城市"], ["wd_city", "星期·城市"], ["static", "固定文字"]];
function slotField(frm, el) {
  const s = document.createElement("select");
  SLOTS.forEach(([v, t]) => {
    const o = document.createElement("option"); o.value = v; o.textContent = t;
    if ((el.slot || "static") === v) o.selected = true; s.appendChild(o);
  });
  s.addEventListener("change", () => { el.slot = s.value; renderAll(); refreshProps(); });
  addRow(frm, "槽位", s);
}
function fontField(frm, el) {
  const s = document.createElement("select");
  [["", "跟随全局(得意黑)"], ["smiley", "得意黑"], ["lxgw", "霞鹜文楷"]].forEach(([v, t]) => {
    const o = document.createElement("option"); o.value = v; o.textContent = t;
    if ((el.font || "") === v) o.selected = true; s.appendChild(o);
  });
  s.addEventListener("change", () => { el.font = s.value || null; renderAll(); });
  addRow(frm, "字体", s);
}
function rectField(frm, el) {
  /* 直观表示：x/y/宽/高（内部仍存 rect=[x0,y0,x1,y1]） */
  const mk = (label, get, set, edge) => {
    const inp = document.createElement("input"); inp.type = "number"; inp.value = get();
    inp.dataset.edge = edge;
    inp.addEventListener("change", () => {
      let v = parseFloat(inp.value); if (isNaN(v)) return;
      set(v); inp.value = Math.round(get()); renderAll();
    });
    addRow(frm, label, inp);
  };
  mk("x", () => el.rect[0], v => {
    const d = v - el.rect[0];
    el.rect = [v, el.rect[1], el.rect[2] + d, el.rect[3]];   /* x 平移，宽不变 */
  }, "x");
  mk("y", () => el.rect[1], v => {
    const d = v - el.rect[1];
    el.rect = [el.rect[0], v, el.rect[2], el.rect[3] + d];
  }, "y");
  mk("宽", () => el.rect[2] - el.rect[0], v => {
    el.rect = [el.rect[0], el.rect[1], el.rect[0] + Math.max(2, v), el.rect[3]];
  }, "w");
  mk("高", () => el.rect[3] - el.rect[1], v => {
    el.rect = [el.rect[0], el.rect[1], el.rect[2], el.rect[1] + Math.max(2, v)];
  }, "h");
}

/* 表单小工具 */
function addRow(frm, label, ctrl) {
  const l = document.createElement("label"); l.textContent = label;
  frm.appendChild(l); frm.appendChild(ctrl); return ctrl;
}
function head(frm, text) {
  const h = document.createElement("div"); h.className = "klabel full"; h.textContent = text;
  frm.appendChild(h);
}
function num(frm, label, get, set, step = 1, min = null, max = null, edge = null) {
  const inp = document.createElement("input"); inp.type = "number";
  inp.value = get(); inp.step = step;
  if (edge) inp.dataset.edge = edge;
  if (min !== null) inp.min = min; if (max !== null) inp.max = max;
  inp.addEventListener("change", () => {
    let v = parseFloat(inp.value); if (isNaN(v)) return;
    if (min !== null) v = Math.max(min, v); if (max !== null) v = Math.min(max, v);
    set(v); inp.value = v; renderAll();
  });
  return addRow(frm, label, inp);
}
function txt(frm, label, get, set) {
  const inp = document.createElement("input"); inp.type = "text"; inp.value = get();
  inp.addEventListener("input", () => { set(inp.value); renderAll(); });
  return addRow(frm, label, inp);
}
function chk(frm, label, get, set) {
  const inp = document.createElement("input"); inp.type = "checkbox"; inp.checked = get();
  inp.addEventListener("change", () => { set(inp.checked); renderAll(); });
  return addRow(frm, label, inp);
}
function rgb(frm, label, get, set) {
  const wrap = document.createElement("span"); wrap.style.display = "inline-flex";
  wrap.style.gap = "6px"; wrap.style.alignItems = "center";
  const c = document.createElement("input"); c.type = "color";
  const v = get(); c.value = rgbToHex(v);
  c.addEventListener("input", () => { set(hexToRgb(c.value)); renderAll(); });
  const t = document.createElement("input"); t.type = "text"; t.value = v.join(",");
  t.style.width = "86px";
  t.addEventListener("change", () => {
    const arr = t.value.split(",").map(x => parseInt(x.trim(), 10));
    if (arr.length === 3 && arr.every(x => x >= 0 && x <= 255)) { set(arr); c.value = rgbToHex(arr); renderAll(); }
  });
  wrap.append(c, t);
  return addRow(frm, label, wrap);
}
function rgbToHex(a) { return "#" + a.map(v => v.toString(16).padStart(2, "0")).join(""); }
function hexToRgb(h) { return [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16)); }
function esc(s) { const d = document.createElement("span"); d.textContent = s ?? ""; return d.innerHTML; }

/* ---------- 载入相框状态（当日渲染的照片与版式） ---------- */
async function loadFrameState() {
  try {
    await _loadFrameState();
  } catch (e) {
    fetch("/api/selftest_report", {method: "POST", body: JSON.stringify(
      {stage: "fs-error", msg: String(e), stack: String(e.stack || "").slice(0, 400)})}).catch(() => {});
  }
}
async function _loadFrameState() {
  if (isDirty() && !confirm("当前版式未保存，载入相框状态将切换版式并丢弃修改。继续？")) return;
  let fs;
  try {
    const r = await fetch("/api/frame_state");
    if (!r.ok) throw new Error(await r.text());
    fs = await r.json();
  } catch (e) { alert("获取相框状态失败: " + e.message); return; }
  if (!fs.layout || !style_registry_has(fs.layout)) {
    await modal(`<h2>🖼 相框状态</h2><p>相框当前版式 ${fs.layout || "未知"} 不在版式库中，无法载入。</p>`,
                [["知道了", "primary"]]);
    return;
  }
  if (fs.photo_url) {
    addPhotoOption("🖼 相框当前照片", fs.photo_url, true);
    await setPhoto(fs.photo_url);
  }
  await loadStyle(fs.layout);
  if (fs.item) {
    item.side = fs.item.side ?? item.side;
    item.date = fs.item.date || item.date;
    item.city = fs.item.city ?? item.city;
    item.subjects_json = fs.item.subjects_json || "";
    $("itSide").value = item.side; $("itDate").value = item.date; $("itCity").value = item.city;
  }
  cropKey = "";                 /* 主体信号变了，强制重裁 */
  await updateCroppedPhoto();
  renderAll();
  const dt = fs.generated ? new Date(fs.generated).toLocaleString("zh-CN") : "未知时间";
  await modal(`<h2>🖼 已载入相框状态</h2>
    <div class="frm" style="min-width:360px">
      <label>版式</label><span>${fs.layout} · ${esc(fs.layout_name || "")}</span>
      <label>渲染时间</label><span>${dt}</span>
      <label>照片</label><span class="mono" style="word-break:break-all">${esc(fs.photo_path || "")}</span>
      <label>文案</label><span>${esc(fs.item ? fs.item.side : "")}</span>
      ${fs.holiday ? `<label>节日层</label><span>${esc(fs.holiday)}（编辑器 v1 不渲染节日层，真机对照亦不含）</span>` : ""}
      <label>数据来源</label><span>${fs.exact ? "sidecar（精确，含实际渲染文案）" : "photos.db 兜底（旧 sidecar 无 item，文案为库内分析值）"}</span>
    </div>`, [["好", "primary"]]);
}
function style_registry_has(id) { return stylesList.some(s => s.id === id); }

/* ---------- undo ---------- */
function doUndo() {
  if (!undoStack.length) return;
  redoStack.push(clone(style));
  style = undoStack.pop();
  if (selected >= style.elements.length) selected = -1;
  refreshAll();
}
function doRedo() {
  if (!redoStack.length) return;
  undoStack.push(clone(style));
  style = redoStack.pop();
  refreshAll();
}

/* ---------- 保存（diff 确认） ---------- */
function diffLines(a, b) {
  const A = a.split("\n"), B = b.split("\n"), n = A.length, m = B.length;
  const dp = Array.from({length: n + 1}, () => new Uint32Array(m + 1));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--)
    dp[i][j] = A[i] === B[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  const out = []; let i = 0, j = 0;
  while (i < n && j < m) {
    if (A[i] === B[j]) { out.push(["ctx", A[i]]); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { out.push(["del", A[i++]]); }
    else { out.push(["add", B[j++]]); }
  }
  while (i < n) out.push(["del", A[i++]]);
  while (j < m) out.push(["add", B[j++]]);
  return out;
}
async function saveFlow() {
  if (!style || !isDirty()) return;
  const cur = styleJsonText();
  const html = diffLines(savedJsonText, cur).map(([t, l]) =>
      `<span class="diff-${t === "ctx" ? "ctx" : t}">${esc(l) || " "}</span>`).join("");
  const ok = await modal(`<h2>保存 ${style.id} · 变更确认</h2>
      <div class="diffbox">${html}</div>
      <div class="tips">写入 layouts/${style.id}.json（回滚靠 git）</div>`,
      [["取消", "cancel"], ["💾 确认保存", "primary"]]);
  if (!ok || ok.button !== "💾 确认保存") return;
  await api(`/api/styles/${style.id}`, "PUT", style);
  savedJsonText = styleJsonText();
  await refreshStyles();
  renderAll();
}

/* ---------- 真机对照 ---------- */
async function showGroundTruth() {
  if (!style) return;
  const box = $("modalBox");
  $("modalBack").hidden = false;
  box.innerHTML = "<h2>🔬 真机对照</h2><p>后端渲染中…</p>";
  const render = async dither => {
    const r = await fetch("/api/style_preview", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({style, item, photo: photoUrl, dither})});
    if (!r.ok) { box.innerHTML = `<h2>🔬 真机对照</h2><p style="color:#ff9d9d">渲染失败: ${await r.text()}</p>`; return null; }
    return URL.createObjectURL(await r.blob());
  };
  let truthUrl = await render(false);
  if (!truthUrl) return;
  /* 预览侧：去掉警戒线的干净快照 */
  const [w, h] = canvasSize();
  const off = document.createElement("canvas"); off.width = w; off.height = h;
  Renderer.render(off, style, item, croppedPhoto || photoImg, {canvasSize: canvasSize(),
      veilAlphaDefault: meta.veil_alpha, guide: false, eink: $("tgEink").checked});
  const prevUrl = off.toDataURL("image/png");
  box.innerHTML = `
    <h2>🔬 真机对照 · ${style.id}</h2>
    <div id="gtWrap"><img src="${prevUrl}"><img id="gtTop" src="${truthUrl}"></div>
    <input type="range" id="gtSlider" min="0" max="100" value="100" title="叠加混合">
    <div class="tips">上层=Python 真实管线输出，下层=浏览器预览。滑杆对齐检查文字/底托位置；两图完全重合即高保真达标。
      栅格化级细微差异可容忍（ADR 0003）。</div>
    <label class="chk"><input type="checkbox" id="gtDither"> 叠加终段量化（墨水屏出图）</label>
    <div class="actions"><button class="primary" id="gtClose">关闭</button></div>`;
  $("gtSlider").addEventListener("input", e => $("gtTop").style.opacity = e.target.value / 100);
  $("gtDither").addEventListener("change", async e => {
    URL.revokeObjectURL(truthUrl);
    truthUrl = await render(e.target.checked);
    if (truthUrl) $("gtTop").src = truthUrl;
  });
  $("gtClose").onclick = closeModal;
}
function closeModal() {
  $("modalBack").hidden = true;
  $("modalBox").innerHTML = "";
  delete $("modalBox")._resolve;
}
function modal(html, buttons) {
  return new Promise(res => {
    const box = $("modalBox");
    $("modalBack").hidden = false;
    box.innerHTML = html + '<div class="actions"></div>';
    const act = box.querySelector(".actions");
    buttons.forEach(([t, cls]) => {
      const b = document.createElement("button");
      b.textContent = t; if (cls === "primary") b.className = "primary";
      b.onclick = () => {
        /* 关窗前快照表单值（关窗即销毁 DOM） */
        const values = {};
        box.querySelectorAll("input,select,textarea").forEach(el => {
          if (el.id) values[el.id] = el.type === "checkbox" ? el.checked : el.value;
        });
        const v = t;
        closeModal();
        res(cls === "cancel" ? null : {button: v, values});
      };
      act.appendChild(b);
    });
    $("modalBack").onclick = e => { if (e.target.id === "modalBack") { closeModal(); res(null); } };
  });
}

/* ---------- 新建版式 ---------- */
async function newStyleDialog() {
  const baseOpts = ['<option value="">空白</option>']
      .concat(stylesList.filter(s => !style || s.id !== style.id)
        .map(s => `<option value="${s.id}">基于 ${s.id}「${esc(s.name)}」</option>`)).join("");
  const html = `
    <h2>新建版式</h2>
    <div class="frm" style="min-width:340px">
      <label>朝向</label><select id="nsOri">
        <option value="portrait">竖屏 480×800（P 系）</option>
        <option value="landscape">横屏 800×480（L 系）</option></select>
      <label>名称</label><input type="text" id="nsName" placeholder="如：左下题签">
      <label>底稿</label><select id="nsBase">${baseOpts}</select>
    </div>`;
  const ok = await modal(html, [["取消", "cancel"], ["创建", "primary"]]);
  if (!ok) return;
  const doc = await api("/api/styles", "POST", {
    orientation: ok.values.nsOri || "portrait",
    name: ok.values.nsName || "", base: ok.values.nsBase || ""});
  await refreshStyles();
  await loadStyle(doc.id);
}

/* ---------- 启动 ---------- */
buildPresets();
init().catch(err => {
  document.body.insertAdjacentHTML("afterbegin",
    `<div style="background:#c80000;color:#fff;padding:8px 14px">初始化失败: ${err}</div>`);
});
