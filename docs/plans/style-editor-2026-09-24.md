# 样式编辑器设计与实施计划（2026-09-24）

> grill-with-docs 会话产出的共识。决策依据见 ADR 0002（版式数据化）、
> ADR 0003（编辑器形态与预览保真）；术语见 CONTEXT.md「照片排版」区。
>
> **实施状态（2026-09-24 当日完成）**：阶段①–④全部落地并验收——
> 迁移 864/864 逐像素一致（`scripts/validate_style_migration.py`）；
> 预览保真自检 12 款 mean diff ≤5.4/255（`static/style_editor/selftest.html`）；
> 交互自动化 20/20（`static/style_editor/interact_test.html`，无头 chromium）。
> 编辑器入口：webui.py 重启后访问 `/style-editor`。

## 需求对照

| 原始需求 | 落地方式 |
|---|---|
| HTML 编辑器，设计横/竖照片排版样式 | `/style-editor` 单页，480×800 / 800×480 画布，一朝向一样式 |
| 可调元素：文本框（颜色/透明度/渐变/形态/尺寸/位置）、印章等预设 | 元素级编辑：glass/veil/text/vtext/seal/meta_line/bar/frame/badge 九类元素，全参数属性面板 + 画布拖拽 |
| 加载已有样式、编辑后保存 | 12 款在册版式全部数据化为 JSON（ADR 0002），编辑器直接读写 |
| 拖拽现有样式组合为新样式 | 元素库预设拖入画布 + 以现有版式为底稿开新样式（编号 P13/L08 续） |

## 架构

```
layouts/                     # 版式唯一事实来源：一样式一 JSON 文件
  P01.json ... L07.json
style_runtime.py             # 通用解释渲染器：按 JSON 调绘制原语画到画布
render_layouts.py            # 退役逐款函数，保留 _glass/_scrim/_seal 等原语供 runtime 复用
layout_strategy.py           # 选型打分/硬否决留代码，按版式 ID 索引不变；
                             # CROP_RESERVES 迁入各样式 JSON
webui.py                     # + /style-editor 路由与样式 CRUD/真机对照 API
static/style_editor/         # 无构建单页：index.html + editor.js + renderer.js
scripts/validate_style_migration.py   # 新旧渲染逐像素对比验收
static/style_editor/stress/  # 压力测试照片（亮底/暗底/复杂构图，横竖齐备）
```

## 样式 schema（草案，实现时定稿）

```jsonc
{
  "schema_version": 1,
  "id": "P05",
  "name": "竖排题签·钤印",
  "orientation": "portrait",          // portrait | landscape
  "crop_reserve": { /* 自 layout_strategy.CROP_RESERVES 原样迁入 */ },
  "elements": [                        // 数组顺序即层级（底→顶）
    { "kind": "glass",  "name": "右侧签面", "rect": [382,56,454,352],
      "radius": 8, "alpha": 184, "blur": 10 },
    { "kind": "vtext",  "slot": "side", "font": "smiley", "size": 24,
      "col_h": 280, "pitch": 31, "col_gap": 31, "rect": [397,64,454,344] },
    { "kind": "seal",   "slot": "city", "glyph_px": 28, "rect": [382,356,454,400] },
    { "kind": "meta_line", "slot": "date", "with_weekday": true,
      "size": 17, "fill": [96,90,82] }
  ]
}
```

- 坐标：画布绝对像素（rect = x0,y0,x1,y1），与渲染器现状一致。
- 槽位枚举：`side | date | city | holiday_name | greeting | static`（static 用
  `static_text`，装饰性文字）。文案不入样式（Q6）。
- 文本元素参数：`font`（smiley | lxgw，Q14）、`size`、`scheme`（dark | white
  带投影）、`stroke`、`align/cx`、`overflow`（wrap+n 行 | truncate）；竖排参数
  `col_h/pitch/col_gap`（右→左列序）。
- 元素 kind 与参数对照（迁移来源）：glass←`_glass`、veil←`_scrim`、
  text/vtext←`_draw_dark/_draw_white`+P05 竖排、seal←`_seal`、
  meta_line←`_meta_segments`、bar←各款内联饰杠；frame/badge 为新预设。

## 编辑器功能清单

- 三栏布局：元素库（预设 + 从在册版式抽取的元素）/ 画布 / 属性面板。
- 画布：真实像素尺寸、INSET 40px 相框警戒线（可开关）、朝向切换（横竖两画布
  对照但不强制成对）。
- 元素操作：库→画布拖入、移动/缩放、层级拖拽调序、复制/删除、undo/redo。
- 槽位工具条：占位文案可编辑（默认真实长度样例）、一键拉长压测（复现 18
  半角宽否决场景）。
- 预览：默认 RGB 高保真；墨水屏模式（JS 复刻 gamma+四色量化）；真机对照
  （后端真管线出图叠加比对，滑杆混合）。
- 保存：写回 `layouts/<ID>.json`，保存前 diff 确认；新建样式 P13/L08 续编号
  + name；回滚交 git。

## 实施阶段与验收

1. **数据化**：schema 定稿、`style_runtime.py`（复用 render_layouts 原语）、
   12 款转写为 `layouts/*.json`。
   验收：`validate_style_migration.py` 对每款 × 每张压力照片，新旧渲染
   **逐像素一致**（同一 Pillow 原语，理应零差异）。
2. **切换**：`render_layout()` 改读 `layouts/` 注册表，退役逐款函数；
   `layout_strategy` 注册表同步，CROP_RESERVES 迁入 JSON。
   注意：`holiday_render._dodge_x` 的锚位避让按版式 ID 硬编码，ID 不变则
   不受影响，切换时回归验证节日层。
3. **编辑器**：webui 路由 + API（样式 CRUD、真机对照渲染、压力照片服务、
   任意照片换入）、单页三栏 UI、canvas2d 镜像渲染器。
   验收：编辑器预览与 Python 出图真机对照，目视无可辨差异（栅格化级差异
   可容忍，ADR 0003）。
4. **打磨**：墨水屏模式、diff 保存确认、undo/redo、竖排/边框/角标预设。

## 范围外（v2 候选）

- 节日模板（holiday_render 9 套）数据化与编辑（Q8：架构预留复用）。
- 编辑打分权重/否决规则（Q13：策略留 Python）。
