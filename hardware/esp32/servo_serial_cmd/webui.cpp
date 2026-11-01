/*
 * webui.cpp — 网页 HTML + JSON
 *
 * HTML 用 R"(...)" raw literal 存进 PROGMEM，省 RAM。
 * 前端是单页：模式切换、参数输入、启动/停止按钮、零位校正、500ms 轮询刷新位置。
 */
#include "webui.h"
#include "config.h"
#include <ArduinoJson.h>

// ============ 前端 HTML ============
static const char INDEX_HTML[] PROGMEM = R"HTML(
<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ESP32 舵机控制</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
         background: #f0f2f5; margin: 0; padding: 16px; color: #222; }
  h1 { font-size: 1.2rem; margin: 0 0 12px; }
  .card { background: #fff; border-radius: 10px; padding: 16px; margin-bottom: 12px;
          box-shadow: 0 1px 4px rgba(0,0,0,.08); }
  .row { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
  .big { font-size: 2.4rem; font-weight: 700; color: #2563eb; }
  .muted { color: #666; font-size: .85rem; }
  .badge { display: inline-block; padding: 2px 10px; border-radius: 12px;
           font-size: .8rem; background: #e5e7eb; color: #333; }
  .badge.on { background: #16a34a; color: #fff; }
  label { font-size: .85rem; color: #555; display: block; margin-bottom: 4px; }
  input[type=number], input[type=range] { width: 100%; padding: 6px; font-size: 1rem; }
  .field { flex: 1; min-width: 120px; }
  button { padding: 8px 16px; border: none; border-radius: 6px; font-size: .9rem;
           cursor: pointer; background: #2563eb; color: #fff; }
  button.stop { background: #dc2626; }
  button.ghost { background: #e5e7eb; color: #333; }
  button:disabled { opacity: .5; cursor: not-allowed; }
  .seg { display: inline-flex; border: 1px solid #cbd5e1; border-radius: 6px; overflow: hidden; }
  .seg button { border-radius: 0; background: #fff; color: #333; }
  .seg button.active { background: #2563eb; color: #fff; }
  #log { font-family: ui-monospace, Consolas, monospace; font-size: .78rem;
         background: #0f172a; color: #cbd5e1; padding: 10px; border-radius: 6px;
         height: 140px; overflow-y: auto; white-space: pre-wrap; }
  .hidden { display: none; }
</style>
</head>
<body>
  <h1>ESP32 舵机控制台</h1>

  <!-- 状态卡片 -->
  <div class="card">
    <div class="row">
      <div>
        <div class="muted">逻辑 / 实际输出</div>
        <div><span class="big" id="pos">--</span><span class="muted">° / </span><span class="big" id="outPos" style="font-size:1.6rem;color:#666">--</span><span class="muted">°</span></div>
      </div>
      <div class="field">
        <div class="muted">模式 <span class="badge" id="modeBadge">--</span></div>
        <div class="muted" style="margin-top:8px">范围: <b id="rangeV">--</b>° · 偏移: <b id="offsetV">--</b>°</div>
        <div class="muted" style="margin-top:8px" id="ipV">IP: --</div>
      </div>
    </div>
  </div>

  <!-- 模式选择 -->
  <div class="card">
    <label>工作模式</label>
    <div class="seg" id="modeSeg">
      <button data-mode="idle" class="active">停止</button>
      <button data-mode="position">位置控制</button>
      <button data-mode="velocity">速度控制</button>
    </div>
  </div>

  <!-- 位置模式参数 -->
  <div class="card" id="posPanel">
    <label>位置控制</label>
    <div class="row">
      <div class="field">
        <label>目标角度 (°) <span id="targetV" class="muted"></span></label>
        <input type="range" id="target" min="0" max="360" step="1" value="0">
      </div>
      <div class="field" style="max-width:120px">
        <label>速度 (°/s)</label>
        <input type="number" id="speed" min="1" max="360" step="1" value="60">
      </div>
      <div>
        <button id="posStart">启动</button>
        <button id="posStop" class="stop">停止</button>
      </div>
    </div>
  </div>

  <!-- 速度模式参数 -->
  <div class="card" id="velPanel">
    <label>速度控制（正=正转，负=反转，0=停）</label>
    <div class="row">
      <div class="field">
        <label>角速度 (°/s)</label>
        <input type="number" id="velocity" min="-360" max="360" step="1" value="60">
      </div>
      <div>
        <button id="velStart">启动</button>
        <button id="velStop" class="stop">停止</button>
      </div>
    </div>
    <div class="muted" id="velHint" style="margin-top:6px"></div>
  </div>

  <!-- 通用设置 -->
  <div class="card">
    <label>通用设置</label>
    <div class="row">
      <div class="field" style="max-width:140px">
        <label>可转范围 (°)</label>
        <input type="number" id="range" min="1" max="360" step="1" value="360">
      </div>
    </div>
    <div class="row" style="margin-top:10px">
      <div class="field" style="max-width:140px">
        <label>零位校正 offset (°，&ge;0)</label>
        <input type="number" id="offset" min="0" max="180" step="0.5" value="0">
      </div>
      <div>
        <button id="offApply" class="ghost">应用预览</button>
        <button id="offSave">保存</button>
      </div>
    </div>
    <div class="muted" style="margin-top:6px">offset = 浮标0°对应的舵机物理角度。offset=0 时浮标与舵机坐标重合；offset=11 时浮标0°→舵机11°，浮标全程平移。</div>
  </div>

  <!-- WiFi 设置（切换网络用，保存后自动重启）-->
  <div class="card">
    <label>WiFi 设置 <span class="muted" id="curSsid"></span></label>
    <div class="row">
      <div class="field">
        <label>WiFi 名称 (SSID)</label>
        <input type="text" id="wfSsid" maxlength="32" placeholder="新 WiFi 名">
      </div>
      <div class="field">
        <label>密码</label>
        <input type="text" id="wfPass" maxlength="64" placeholder="密码">
      </div>
      <div>
        <button id="wfSave">保存并重启</button>
      </div>
    </div>
    <div class="muted" style="margin-top:6px">保存后 ESP32 会自动重启连接新 WiFi，本页需用新 IP 重新打开。</div>
  </div>

  <!-- 深度休眠（功耗测试用）-->
  <div class="card">
    <label>深度休眠 <span class="muted">（功耗测试用：休眠后可拔 USB 测锂电池真实功耗）</span></label>
    <div class="row">
      <div class="field" style="max-width:140px">
        <label>休眠时长 (秒)</label>
        <input type="number" id="sleepSec" min="1" max="86400" step="1" value="30">
      </div>
      <div>
        <button id="sleepBtn" class="stop">进入休眠</button>
      </div>
    </div>
    <div class="muted" style="margin-top:6px">
      点此按钮 = 串口输入 <code>deepsleep N</code>。休眠后 WiFi 断开，网页失联；到点自动唤醒重启。<br>
      <b>测功耗流程：</b>1. 点此按钮 → 2. 立刻拔 USB（改锂电池供电）→ 3. 万用表读数 = 真实休眠功耗。
    </div>
  </div>

  <!-- 日志 -->
  <div class="card">
    <label>操作日志</label>
    <div id="log"></div>
  </div>

<script>
const $ = id => document.getElementById(id);
let curMode = 'idle';

function log(msg) {
  const t = new Date().toLocaleTimeString();
  $('log').innerHTML = `[${t}] ${msg}\n` + $('log').innerHTML;
}

// 发送控制指令
async function sendCtrl(payload) {
  try {
    const r = await fetch('/api/control', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    const txt = await r.text();
    if (!r.ok) { log('错误: ' + txt); return false; }
    log('发送: ' + JSON.stringify(payload) + (txt ? ' → ' + txt : ''));
    return true;
  } catch(e) { log('网络错误: ' + e.message); return false; }
}

// 切换模式按钮组
function setModeUI(m) {
  curMode = m;
  document.querySelectorAll('#modeSeg button').forEach(b => {
    b.classList.toggle('active', b.dataset.mode === m);
  });
}

document.querySelectorAll('#modeSeg button').forEach(b => {
  b.onclick = async () => {
    const m = b.dataset.mode;
    setModeUI(m);
    const modeNum = m === 'position' ? 1 : m === 'velocity' ? 2 : 0;
    await sendCtrl({mode: modeNum});
  };
});

// 位置模式
$('target').oninput = e => $('targetV').textContent = e.target.value + '°';
$('posStart').onclick = async () => {
  await sendCtrl({
    mode: 1, action: 'start',
    target: parseFloat($('target').value),
    speed:  parseFloat($('speed').value)
  });
  setModeUI('position');
};
$('posStop').onclick = async () => { await sendCtrl({mode: 0}); setModeUI('idle'); };

// 速度模式
$('velStart').onclick = async () => {
  await sendCtrl({
    mode: 2, action: 'start',
    velocity: parseFloat($('velocity').value)
  });
  setModeUI('velocity');
};
$('velStop').onclick = async () => { await sendCtrl({mode: 0, velocity: 0}); setModeUI('idle'); };

// 通用
// 用户正在编辑某字段时，轮询不再覆盖它（避免输入到一半被服务器旧值冲掉）
let editingRange = false, editingOffset = false;
$('range').onfocus  = () => editingRange  = true;
$('offset').onfocus = () => editingOffset = true;

$('range').onchange = async () => {
  await sendCtrl({range: parseFloat($('range').value)});
  $('target').max = $('range').value;
  editingRange = false;
};
$('offApply').onclick = async () => {
  const v = parseFloat($('offset').value);
  if (isNaN(v)) { log('offset 值无效'); return; }
  await sendCtrl({offset: v});
  log('offset 预览已应用: ' + v + '°（未存Flash）');
  editingOffset = false;
};
$('offSave').onclick  = async () => {
  const v = parseFloat($('offset').value);
  if (isNaN(v)) { log('offset 值无效'); return; }
  await sendCtrl({offset: v, save: true});
  log('offset 已保存到 Flash: ' + v + '°');
  editingOffset = false;
};

// WiFi 设置：单独走 /api/wifi，保存后自动重启
$('wfSave').onclick = async () => {
  const ssid = $('wfSsid').value.trim();
  const pass = $('wfPass').value;
  if (!ssid) { log('SSID 不能为空'); return; }
  try {
    const r = await fetch('/api/wifi', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ssid, pass})
    });
    if (!r.ok) { log('保存失败: ' + await r.text()); return; }
    log('WiFi 已保存，ESP32 重启中... 等几秒后用新 IP 重新打开');
    setTimeout(() => location.reload(), 5000);
  } catch(e) { log('网络错误: ' + e.message); }
};

// 深度休眠：走 /api/sleep，ESP32 会断 WiFi，网页失联
$('sleepBtn').onclick = async () => {
  const secs = parseInt($('sleepSec').value);
  if (!secs || secs < 1) { log('休眠时长无效'); return; }
  if (!confirm('确定进入深度休眠 ' + secs + ' 秒？\n休眠后网页会失联，到点自动唤醒重启。')) return;
  log('发送休眠请求: ' + secs + ' 秒');
  try {
    const r = await fetch('/api/sleep', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({seconds: secs})
    });
    const txt = await r.text();
    log('ESP32 回复: ' + txt);
    log('现在可以拔 USB 测锂电池功耗了。' + secs + ' 秒后网页会自动恢复（按F5）。');
    // N 秒后自动刷新页面（ESP32 唤醒重启后网页恢复）
    setTimeout(() => location.reload(), secs * 1000 + 8000);
  } catch(e) {
    // 休眠后 WiFi 断开会触发 catch，这是正常的
    log('网页已与 ESP32 断开（休眠中）。等待唤醒...');
    setTimeout(() => location.reload(), secs * 1000 + 8000);
  }
};

// 轮询状态
async function poll() {
  try {
    const r = await fetch('/api/state');
    const s = await r.json();
    $('pos').textContent = s.position.toFixed(1);
    $('outPos').textContent = s.output.toFixed(1);
    $('rangeV').textContent = s.range.toFixed(0);
    $('offsetV').textContent = s.offset.toFixed(1);
    $('ipV').textContent = 'IP: ' + (s.ip || '--');
    $('curSsid').textContent = s.ssid ? ('· 当前: ' + s.ssid) : '· 未配置';
    const m = s.mode === 1 ? '位置' : s.mode === 2 ? '速度' : '停止';
    const badge = $('modeBadge');
    badge.textContent = m + (s.running ? ' (运行)' : '');
    badge.classList.toggle('on', s.running);
    if (!editingRange)  $('range').value  = s.range;
    if (!editingOffset) $('offset').value = s.offset;
    $('velHint').textContent = s.range >= 360
      ? '当前为连续旋转模式（360°），会持续旋转。'
      : '当前范围 < 360°，将在 0 与 ' + s.range.toFixed(0) + '° 之间往复。';
  } catch(e) {}
}
setInterval(poll, 500);
poll();
log('页面已加载');
</script>
</body>
</html>
)HTML";

const char* getIndexHtml() {
  return INDEX_HTML;
}

// ============ 配网页面（AP 模式专用，极简）============
static const char SETUP_HTML[] PROGMEM = R"HTML(
<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ESP32 配网</title>
<style>
  body { font-family: system-ui, sans-serif; background:#f0f2f5; margin:0; padding:24px; color:#222; }
  .box { max-width:360px; margin:40px auto; background:#fff; padding:24px; border-radius:12px;
         box-shadow:0 2px 12px rgba(0,0,0,.1); }
  h1 { font-size:1.3rem; margin:0 0 8px; }
  .muted { color:#666; font-size:.85rem; margin-bottom:16px; }
  label { font-size:.85rem; color:#555; display:block; margin:10px 0 4px; }
  input { width:100%; padding:10px; font-size:1rem; border:1px solid #cbd5e1;
          border-radius:6px; box-sizing:border-box; }
  button { width:100%; margin-top:16px; padding:12px; border:none; border-radius:6px;
           background:#2563eb; color:#fff; font-size:1rem; cursor:pointer; }
  #msg { margin-top:14px; font-size:.9rem; text-align:center; }
</style>
</head>
<body>
  <div class="box">
    <h1>ESP32 舵机 · 配网</h1>
    <div class="muted">填入你家 WiFi 信息，保存后设备会自动重启并连接。</div>
    <label>WiFi 名称 (SSID)</label>
    <input id="ssid" maxlength="32" placeholder="如 mi_0203">
    <label>密码</label>
    <input id="pass" maxlength="64" placeholder="WiFi 密码">
    <button id="save">保存并连接</button>
    <div id="msg"></div>
  </div>
<script>
document.getElementById('save').onclick = async () => {
  const ssid = document.getElementById('ssid').value.trim();
  const pass = document.getElementById('pass').value;
  const msg = document.getElementById('msg');
  if (!ssid) { msg.style.color='#dc2626'; msg.textContent='SSID 不能为空'; return; }
  msg.style.color='#666'; msg.textContent='正在保存...';
  try {
    const r = await fetch('/api/wifi', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ssid, pass})
    });
    if (!r.ok) { msg.style.color='#dc2626'; msg.textContent='保存失败: ' + await r.text(); return; }
    msg.style.color='#16a34a';
    msg.textContent='已保存！设备重启中，约10秒后会自动连接 ' + ssid +
                    '。请切回 ' + ssid + ' 网络后查看路由器分配的新 IP。';
  } catch(e) { msg.style.color='#dc2626'; msg.textContent='错误: ' + e.message; }
};
</script>
</body>
</html>
)HTML";

const char* getSetupHtml() {
  return SETUP_HTML;
}

// ============ JSON 序列化 ============
String buildStateJson(float curAngle, bool running, const String& ipStr, float outAngle) {
  JsonDocument doc;
  doc["mode"]      = (int)g_cfg.mode;
  doc["position"]  = curAngle;     // 逻辑角度
  doc["output"]    = outAngle;     // 实际输出角度（逻辑+offset）
  doc["range"]     = g_cfg.range;
  doc["offset"]    = g_cfg.offset;
  doc["target"]    = g_cfg.target;
  doc["speed"]     = g_cfg.speed;
  doc["velocity"]  = g_cfg.velocity;
  doc["running"]   = running;
  doc["ip"]        = ipStr;
  doc["ssid"]      = g_cfg.ssid;
  String out;
  serializeJson(doc, out);
  return out;
}

// ============ 控制指令解析 ============
// 期望的 JSON 字段（全部可选，给哪个改哪个）：
//   { mode: 0|1|2, action: "start"|"stop", target, speed, velocity, range, offset, save: bool }
bool parseControl(const String& body, String& errMsg) {
  JsonDocument doc;
  auto err = deserializeJson(doc, body);
  if (err) {
    errMsg = "JSON 解析失败: ";
    errMsg += err.c_str();
    return false;
  }

  bool changed = false;
  bool wantSave = doc["save"] | false;

  // 模式（0/1/2 整数）
  if (doc.containsKey("mode")) {
    int m = doc["mode"];
    if (m < 0 || m > 2) { errMsg = "mode 取值非法"; return false; }
    g_cfg.mode = (Mode)m;
    changed = true;
  }

  // 各参数（带限幅）
  if (doc.containsKey("target")) {
    float t = doc["target"];
    if (t < 0) t = 0;
    if (t > g_cfg.range) t = g_cfg.range;
    g_cfg.target = t;
    changed = true;
  }
  if (doc.containsKey("speed")) {
    float s = doc["speed"];
    if (s < 1) s = 1;
    if (s > 360) s = 360;
    g_cfg.speed = s;
    changed = true;
  }
  if (doc.containsKey("velocity")) {
    float v = doc["velocity"];
    if (v < -360) v = -360;
    if (v > 360) v = 360;
    g_cfg.velocity = v;
    changed = true;
  }
  if (doc.containsKey("range")) {
    float r = doc["range"];
    if (r < 1) r = 1;
    if (r > 360) r = 360;
    g_cfg.range = r;
    if (g_cfg.target > g_cfg.range) g_cfg.target = g_cfg.range;
    changed = true;
  }
  if (doc.containsKey("offset")) {
    float o = doc["offset"];
    // offset 只能为正(或0)：表示"浮标0°对应舵机几度"，即舵机物理零点与外部零点的距离
    // 不设上限，因为舵机物理量程可能 >180；offset+range 是否超量程由用户负责
    if (o < 0) o = 0;
    g_cfg.offset = o;
    changed = true;
  }

  // 持久化：有变更就存（或显式要求 save）
  if (changed || wantSave) {
    configSave();
  }
  return true;
}
