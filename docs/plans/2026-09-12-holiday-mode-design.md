# 节假日模式设计（含纪念日 AI 选片）

日期：2026-09-12
状态：设计定稿，待实施
前置依赖：`7a26c5d`（12 款文字版式渲染管线 + 自动选版式）已合入，本设计基于该版式系统。

## 1. 背景与目标

InkTime 每日从照片库选"历史上的今天"照片渲染上屏。目标：在节假日 / 纪念日当天，

1. 识别"今天是什么日子"——通过独立配置文件维护节假日列表，支持公历与农历；
2. 给当日渲染的照片叠加专属模板与庆祝元素（五套模板，适配横竖屏）；
3. 可选接入远端视觉大模型：按节日氛围重排当日候选照片、生成照片描述与横幅祝福语；未配置 AI 时全部回退到现有管线与手写配置。

## 2. 非目标

- 不改动选片打分算法本身（AI 重排只作用于已选候选的顺序与文案）。
- 不改动 ESP32 固件（节日画面在渲染期烙进 BIN，sidecar 新增字段旧固件自动忽略）。
- 不做节日主题的照片库筛选（如"只显示历年春节的照片"）——留待后续按需求追加。
- 不做多设备差异化模板（所有设备同一天同一模板）。

## 3. 配置文件

### 3.1 `holidays.json`（数据文件，gitignore）

位于仓库根目录，与 `config.py` 同级。仓库内提交模板 `holidays-example.json`（内容即下例，JSON 不支持注释，字段含义靠本文档与字段名自解释）：

```json
{
  "holidays": [
    { "name": "春节",     "calendar": "lunar", "start": "1-1",       "end": "1-6", "greeting": "新春快乐 阖家团圆" },
    { "name": "除夕",     "calendar": "lunar", "start": "chuxi" },
    { "name": "中秋节",   "calendar": "lunar", "start": "8-15" },
    { "name": "国庆节",   "calendar": "solar", "start": "10-1",      "end": "10-7" },
    { "name": "爸爸70大寿", "calendar": "solar", "start": "2027-3-5",  "template": "elder_birthday",
      "greeting": "福如东海 寿比南山", "ai_hint": "爸爸，70岁生日，全家聚餐" }
  ]
}
```

字段说明：

| 字段 | 必填 | 说明 |
|---|---|---|
| `name` | 是 | 节日名，显示在模板上 |
| `calendar` | 是 | `solar` 公历 / `lunar` 农历 |
| `start` | 是 | `"月-日"` 每年循环；`"年-月-日"`（带年份）只命中一次；`"chuxi"` 除夕特殊规则 |
| `end` | 否 | 范围结束日，格式同 `start`（年可省略）。跨年范围自动回绕（如 `12-25`~`1-2`） |
| `template` | 否 | 模板 ID，缺省 `general_ribbon`，见 §5 |
| `greeting` | 否 | 横幅祝福语；AI 开启且成功时被 AI 生成的祝福语覆盖 |
| `ai_hint` | 否 | 拼入 AI 提示词的补充语境（人物、关系、活动）；缺省用 `name` |
| `leap` | 否 | 农历专用，`true` 表示只匹配闰该月；缺省只匹配平月 |

### 3.2 `config.py` 新增第 ⑧ 段（节假日模式）

```python
# ═══ ⑧ 节假日模式 ═════════════════════════════════════
# （holidays.py / holiday_render.py / holiday_ai.py —— render_daily_photo.py 调用）

HOLIDAY_MODE_ENABLED = True
HOLIDAYS_FILE = "./holidays.json"

# ── 可选：远端视觉大模型（节日选片 / 描述 / 祝福语）──
# 协议自动识别：URL 以 /api/chat 结尾按 Ollama native，
# 含 /v1/chat/completions 按 OpenAI 兼容（与 analyze_photos.py 同一套双协议）。
# 模型必须支持图像输入。留空 MODEL 视为未启用。
HOLIDAY_AI_ENABLED = False
HOLIDAY_AI_API_URL  = ""      # 例: http://127.0.0.1:11434/api/chat 或 https://.../v1/chat/completions
HOLIDAY_AI_API_KEY  = ""
HOLIDAY_AI_MODEL    = ""
HOLIDAY_AI_TIMEOUT_SEC   = 120
HOLIDAY_AI_CANDIDATES    = 10   # 送选照片数（选片管线照常打分，取前 N 张送 AI）
HOLIDAY_AI_IMAGE_MAX_EDGE = 512 # 送选缩略图长边
```

## 4. 匹配规则（holidays.py）

- **判定基准日是渲染当天**（`TODAY`，即展示日），与照片历史拍摄日期无关。
- 新模块 `holidays.py`，纯匹配逻辑、不依赖 PIL，职责：
  - `load_holidays()`：读 `HOLIDAYS_FILE`，解析校验（缺文件/解析失败返回空表并打印警告）；
  - `get_today_holiday(date: dt.date) -> Optional[dict]`：命中返回
    `{"name", "greeting", "template", "ai_hint"}`，否则 `None`。
- 公历条目：比对 `(month, day)`；带年份的一次性日期比对完整 `(y, m, d)`。
- 农历条目：用 `lunardate`（纯 Python，约 18KB，支持闰月，1900–2100）把当天转农历再比对。
- **除夕**：`lunardate` 把"明天"转农历，若明天是正月初一则今天是除夕（免查农历月天数表）。
- 多条命中取配置顺序第一条，不叠加。
- `main()` 每日调用一次，命中时打印 `[HOLIDAY] 命中春节 (2027-2-6, template=general_ribbon)`。

## 5. 五套视觉模板（holiday_render.py）

### 5.1 与版式系统的关系（2026-09-12 提交 `7a26c5d` 的影响）

渲染入口现状：`TEXT_LAYOUT_STRATEGY=True`（默认）时 `render_image` / `render_image_landscape`
走 `_render_with_layout()`：自动选 12 款版式之一 → 按 `CROP_RESERVES` 四向避让裁切 →
`render_layouts.render_layout()` 绘制文字层。

**节日叠加挂钩点**：`_render_with_layout()` 内、`render_layout()` 之后（版式文字层之上、
gamma/抖动之前）调用 `holiday_render.apply_holiday_layer(canvas, layout_id, orientation, holiday)`。

**放置冲突与提示表**：12 款版式中 3 款占用非底部区域，所有顶部放置的节日元素
（横幅 / 药丸 / 角标）按版式感知调整锚点，提示表语义（`placement` 传入各模板函数）：

```python
# 值语义：default=横幅通栏/角标右上；banner_right=元素右移让出左上占位；
#         banner_left=元素整体限制在左侧占位区右边。
HOLIDAY_PLACEMENT = {
    "L02": "banner_right",   # 左侧通高信息栏(x40-216) → 元素右移至 x≥240 起
    "L06": "banner_right",   # 顶签悬挂占顶部左侧(x40-370) → 元素右移至 x≥390 起
    "P05": "banner_left",    # 右侧竖排题签+印(x382-454) → 元素右缘截短至 x≤370；角标翻左上
}
# 其余版式顶部空闲，按 default 放置
```

- **CROP_RESERVES 不改**：横幅是不透明设计元素，盖在照片上属设计行为（与 L05 报纸条同逻辑，避让为 0）；角标很小无需避让。
- **抖动安全**：沿用 `render_layouts.py` 的既有铁律与常量——只用调色板纯色
  （`SEAL_RED=(200,0,0)`、`ACCENT_YELLOW=(220,180,0)`、`WHITE`），元素全不透明、
  无深色描边；字体复用 `render_layouts._get_font()`。
- **装饰手法**：矢量绘制（圆角矩形/椭圆/多边形）+ 必要时预渲染 PNG 贴片
  （沿用 `_seal()` 的 RGBA-paste 先例，alpha 只作 mask、落canvas为RGB纯色）。

### 5.2 模板规格

| 模板 ID | 场景 | 视觉元素 |
|---|---|---|
| `general_ribbon`（默认） | 节假日通用 | 顶部通宽红色横幅（竖屏高 72px / 横屏 56px，底部圆角 16），左侧白色节日名（加粗），右侧白色祝福语；横幅下沿 4px 黄色饰线 |
| `child_birthday` | 儿童生日 | 顶部黄色横幅 + 红色文字；横幅下左右各一组气球（红/黄椭圆 + 白色细线绳，3 只错落） |
| `elder_birthday` | 长辈生日 | 顶部红色横幅（同 general）；横幅右端一枚红色圆角方形「寿」印（白文字，仿 `_seal()` 手法） |
| `adult_birthday` | 成人生日 | 顶部红色横幅（同 general）；横幅右端小三层蛋糕剪影（红色糕体圆角矩形堆叠 + 黄色蜡烛火苗，总高约 40px） |
| `anniversary` | 结婚纪念日 | 顶部居中窄红圆角药丸横幅（白字「结婚纪念日 · 祝福语」），药丸两侧各一枚红心、内叠小黄心（多边形/贝塞尔近似） |

- 每套模板实现为 `holiday_render.py` 内一个绘制函数，签名统一
  `(canvas, orientation, placement, holiday)`，竖横屏几何各自适配，
  并遵守 `FRAME_MASK_INSET_PX` 换算（复用 `_py()/_lx()` 同款口径）。
- 祝福语过长自动截断加省略号（复用 `_fit_text` 思路）。

### 5.3 作用范围

#### 5.3.0 改版（2026-09-13，提案页 docs/holiday-template-showcase-2026-09-13 选定）

模板从 5 款细分为 9 款（旧 ID 保留别名映射）：`general_ribbon`(T1-A)、
`child_bunting`(T2-B)/`child_wave`(T2-E)、`elder_ribbon`(T3-A)/`elder_calligraphy`(T3-B)、
`adult_cake`(T4-A)/`adult_editorial`(T4-D)、`anniversary_rings`(T5-B)/`anniversary_hearts`(T5-E)。
三项强化：

1. **对齐**：文字全部改 PIL 锚点（lm/rm/ma/mm）光学居中；
2. **主体遮挡**：实色顶带模板的占位高度经 `holiday_top_reserve()` 并入裁切避让
   （主体裁切时自动避开横幅）；书法大字/双戒等悬浮元素渲染期读
   `canvas.info["subject_box"]`（由 `_subject_box_on_canvas()` 把主体框映射到画布坐标），
   在左/中/右锚位选与主体 + 版式占位区（`LAYOUT_ZONES`）重叠最小的槽位（`_dodge_x`）；
3. **相框遮罩**：横屏贴边元素统一按可视区（`_vis_x`）锚定，竖屏按可视顶（`_top`）。

装饰烙进**当日渲染批次的所有照片**（photo_0 ~ photo_{N-1}）。设备默认显示 photo_0，
手动 `/fetch?idx=N` 切换到任何一张看到的都是带装饰的版本——满足"切换的那张也要有"
且无需新增按需渲染机制。

## 6. 可选 AI 接入（holiday_ai.py）

仅当"当天命中节假日"且 `HOLIDAY_AI_ENABLED` 且 `HOLIDAY_AI_MODEL` 非空时进入 AI 流程：

```
选片管线照常跑（阈值/加权/连拍去重/路径权重不变）
  AI 模式下 choose_photos_for_today(count=HOLIDAY_AI_CANDIDATES)，其余照旧
        ↓
holiday_ai.select_and_describe(candidates, holiday)——一次 VLM 调用：
  输入：N 张缩略图（长边缩至 HOLIDAY_AI_IMAGE_MAX_EDGE，转 JPEG base64，
        双协议与 analyze_photos.py 相同）+ 节日上下文（name/template/ai_hint/日期）
  输出 JSON：
  { "ranking": [7,3,0,…],                      ← 按贴合节日氛围重排（下标=送选顺序）
    "descriptions": {"7": "和爸爸在天台吹风",…}, ← 每张 ≤20 字（side caption 口径）
    "greeting": "爸爸生日快乐" }                 ← ≤12 字横幅祝福语
        ↓
成功 → photos 按 ranking 重排（photo_0 = AI 首选 = 设备默认显示张），
      **并跳过后续 `apply_portrait_rerank`（PORTRAIT_BOOST）——AI 顺序为准**；
      candidates[i]["side"] 替换为对应 AI 描述（版式渲染器读 item["side"]，零改动穿透）；
      holiday["greeting"] 替换为 AI 祝福语
失败（超时/网络/HTTP 错误/JSON 损坏/下标越界）→ 打印警告，整体回退现有管线
（含照常执行 apply_portrait_rerank）
```

- **防御性解析**：剥离 markdown 代码围栏；`ranking` 下标钳制去重；缺失字段逐项回退
  （只有 ranking → 保留原文案与配置祝福语；只有描述 → 不重排）。
- **未配 AI**：选片、描述（分析期 `side_caption`）、祝福语（`holidays.json` 手写）全部照旧。
- AI 结果在渲染循环前一次性完成，`main()` 内打印 `[HOLIDAY-AI] 重排后首选: <path>`。

## 7. 模块划分与数据流

```
holidays.json ──> holidays.py        匹配：get_today_holiday(TODAY)（纯逻辑，无 PIL）
                        │
render_daily_photo.py main()：
  holiday = get_today_holiday(TODAY)
  count = HOLIDAY_AI_CANDIDATES if (holiday and AI 启用) else DAILY_PHOTO_QUANTITY
  photos, info = choose_photos_for_today(items, TODAY, count=count)
  if holiday and AI 启用：holiday_ai.select_and_describe() → 重排 + 文案/祝福语覆写（失败回退）
  逐张渲染 render_image(chosen)（内部走版式管线）
      └─ _render_with_layout()：选版式 → 避让裁切 → render_layout() 文字层
         └─ holiday_render.apply_holiday_layer(canvas, layout, orient, holiday)  ← 叠加节日层
      └─ gamma → 抖动 → BIN / preview / sidecar（不变）
```

新增文件：`holidays.py`、`holiday_render.py`、`holiday_ai.py`、`holidays-example.json`、
`scripts/test_holidays.py`、`scripts/validate_holiday_templates.py`。
修改文件：`render_daily_photo.py`（main() 接入 + `_render_with_layout` 挂钩 + sidecar）、
`config-example.py`（第 ⑧ 段）、`requirements.txt`（+`lunardate==0.3.0`）、`.gitignore`（+`holidays.json`）。

## 8. 失败降级（每日主流程零风险）

| 故障 | 行为 |
|---|---|
| `holidays.json` 缺失/解析失败 | 警告一次，按无节日处理，其余不变 |
| `lunardate` 未安装 | 公历条目照常匹配，农历条目跳过并警告 |
| 农历转换越界（1900–2100 外） | 跳过该条目并警告 |
| AI 超时/网络失败/JSON 损坏 | 警告，回退现有选片顺序 + 手写文案与祝福语 |

## 9. sidecar 变更

`photo_N.json` 增加可选字段（旧固件忽略未知字段）：

```json
{ "orientation": "portrait", "w": 480, "h": 800,
  "layout": "P01", "holiday": "春节" }
```

## 10. 测试与验证

1. **匹配单测** `scripts/test_holidays.py`（venv python 直跑，无网络依赖）：
   已知真值断言——2026-10-01 国庆、2026-09-25 中秋、2027-02-05 除夕、2027-02-06 春节、
   带年份一次性日期（命中当年、次年不命中）、范围含端点、跨年回绕、闰月规则、
   非节日日不命中、多命中取第一条、坏 JSON 降级。
2. **AI 解析单测**（同文件或 `scripts/test_holiday_ai.py`）：围栏剥离、下标越界钳制、
   部分字段缺失的逐项回退；HTTP 用本地桩或 mock，不依赖真实服务。
3. **模板回归** `scripts/validate_holiday_templates.py`（仿 `validate_layouts.py`）：
   5 模板 × 2 朝向 × 关键版式（含 L02/L06/P05 三个让位特例）渲染样张，
   产出 `docs/holiday-template-validation/` 报告页 + 抖动后样图，人工核对。
4. **端到端**：手工把 `HOLIDAYS_FILE` 指向含"今天"的测试配置跑 `render_daily_photo.py`，
   核对 `[HOLIDAY]`/`[LAYOUT]`/`[GAMMA]` 日志链与 output 产物。

## 11. 决策记录

| 决策 | 结论 | 理由 |
|---|---|---|
| 装饰作用范围 | 当日批次全部照片 | 设备 `/fetch?idx=N` 可切换显示任意一张，批次内全部烙装饰才能保证切换后仍带节日元素（用户确认的"切换那张也渲染"） |
| 农历实现 | `lunardate` 库 | 纯 Python 零依赖、支持闰月；手写换算表易错；树莓派已验证可安装 |
| 除夕表达 | `"chuxi"` 特殊规则 | 农历年末日每年公历浮动，固定月日不可表达；"明天是正月初一"判定免查表 |
| 版式系统关系 | 节日层叠加在版式文字层之上，3 个特例版式让位 | 不改 `7a26c5d` 的选型与裁切逻辑；让位表仅 3 条特例，成本最低 |
| 横幅不避让裁切 | 不改 CROP_RESERVES | 横幅是不透明设计元素（同 L05 先例），盖住部分照片属设计行为 |
| AI 范围 | 只在节假日生效，重排+描述+祝福语 | 用户明确需求；平日管线不动，风险隔离 |
| 祝福语来源 | AI 成功→AI；否则→配置手写 | 用户确认（决策 3） |
| 模板套数 | 5 套（通用/儿童生日/长辈生日/成人生日/结婚纪念日） | 用户指定（决策 4） |
