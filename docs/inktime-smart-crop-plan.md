# InkTime 智能裁切方案 — 实施计划

> 讨论日期：2026-06-06
> 状态：待实施
> 执行环境：PC 端修改，树莓派验证

---

## 一、问题描述

当前 `render_daily_photo.py` 的裁切逻辑（第 698~709 行）采用**居中裁切**：

```python
scale = max(img_area_w / img_w, img_area_h / img_h)
# ...
left = max(0, (draw_w - img_area_w) // 2)   # 始终从正中间裁
top  = max(0, (draw_h - img_area_h) // 2)
```

当照片横竖屏比例与屏幕不匹配时，居中裁切会丢失主体（人脸、宠物等偏离画面中心的内容）。

---

## 二、目标

让裁切窗口**智能偏向画面主体**，确保人物、宠物等重要内容不被裁掉。

---

## 三、方案选择

### 最终方案：YOLO 检测 + 居中兜底（分阶段实施）

| 阶段 | 内容 | 优先级 |
|------|------|--------|
| **阶段一** | 加入 YOLO-nano 物体检测，存储精确 bbox | ✅ 先做 |
| **阶段二** | 多主体冲突时引入 VLM 语义仲裁 | 🔜 按需 |

**阶段一不改动 VLM prompt**，保持分析流程简洁。

---

## 四、阶段一详细计划

### 4.1 技术选型

| 组件 | 选择 | 理由 |
|------|------|------|
| 检测模型 | **YOLOv8-nano**（ultralytics） | 轻量，RPi 可跑，支持 person/cat/dog |
| 推理速度 | ~100-200ms/张（RPi） | 批量凌晨渲染，不在实时路径 |
| Python 包 | `ultralytics` | pip 安装，API 简洁 |

### 4.2 数据库改动

`photos.db` 的 `photo_scores` 表新增一列：

```sql
ALTER TABLE photo_scores ADD COLUMN subjects_json TEXT;
```

**字段格式（JSON）：**

```json
[
  {"label": "person", "bbox": [0.35, 0.12, 0.15, 0.20], "conf": 0.92},
  {"label": "cat",    "bbox": [0.55, 0.40, 0.25, 0.30], "conf": 0.87}
]
```

- `bbox` 格式：`[x_center, y_center, width, height]`，均为 0~1 相对坐标
- `label`：YOLO 原生 class（person / cat / dog 等）
- `conf`：置信度，低于阈值（如 0.3）的不入库

### 4.3 analyze_photos.py 改动

**新增函数 `detect_subjects(image_path)`：**

```python
from ultralytics import YOLO

# 模型只加载一次（全局）
_yolo_model = None

def get_yolo_model():
    global _yolo_model
    if _yolo_model is None:
        _yolo_model = YOLO("yolov8n.pt")  # nano 版本
    return _yolo_model

# 只检测这几类（与相框场景相关）
YOLO_CLASSES = {"person", "cat", "dog", "bird", "horse"}

def detect_subjects(image_path: Path) -> list[dict]:
    model = get_yolo_model()
    results = model(str(image_path), verbose=False)
    
    subjects = []
    for r in results:
        for box in r.boxes:
            cls_name = r.names[int(box.cls)]
            if cls_name not in YOLO_CLASSES:
                continue
            conf = float(box.conf)
            if conf < 0.3:
                continue
            # 转换为归一化 [xc, yc, w, h]
            x1, y1, x2, y2 = box.xyxy[0].tolist()
            img_w, img_h = r.orig_shape  # (h, w)
            subjects.append({
                "label": cls_name,
                "bbox": [
                    round((x1 + x2) / 2 / img_w, 4),
                    round((y1 + y2) / 2 / img_h, 4),
                    round((x2 - x1) / img_w, 4),
                    round((y2 - y1) / img_h, 4),
                ],
                "conf": round(conf, 3),
            })
    return subjects
```

**在 `main()` 的分析循环中调用：**

```python
# 现有流程之后，入库之前
subjects = detect_subjects(path)
subjects_json = json.dumps(subjects, ensure_ascii=False) if subjects else None

# INSERT 语句加入 subjects_json 字段
```

### 4.4 render_daily_photo.py 改动

**替换当前居中裁切逻辑（第 698~709 行）：**

```python
def compute_crop_window(draw_w, draw_h, img_area_w, img_area_h, subjects_json):
    """
    根据 subjects 计算裁切窗口，无 subjects 时回退到居中。
    """
    if not subjects_json:
        # 回退：居中裁切
        left = max(0, (draw_w - img_area_w) // 2)
        top  = max(0, (draw_h - img_area_h) // 2)
        return left, top

    subjects = json.loads(subjects_json)
    if not subjects:
        left = max(0, (draw_w - img_area_w) // 2)
        top  = max(0, (draw_h - img_area_h) // 2)
        return left, top

    # 合并所有主体的 bounding box
    margin = 0.05  # 5% 扩展余量
    xs, ys = [], []
    for s in subjects:
        xc, yc, w, h = s["bbox"]
        xs.extend([xc - w / 2 - margin, xc + w / 2 + margin])
        ys.extend([yc - h / 2 - margin, yc + h / 2 + margin])

    # 联合框中心（归一化坐标 → 像素）
    cx = (min(xs) + max(xs)) / 2 * draw_w
    cy = (min(ys) + max(ys)) / 2 * draw_h

    # 裁切窗口以联合框中心为锚点，限制在图片范围内
    left = int(max(0, min(cx - img_area_w / 2, draw_w - img_area_w)))
    top  = int(max(0, min(cy - img_area_h / 2, draw_h - img_area_h)))

    return left, top
```

**在渲染流程中调用：**

```python
# 从数据库读取 subjects_json（与 path 一起查）
subjects_json = ...  # 从 photo_scores 表读取

left, top = compute_crop_window(draw_w, draw_h, img_area_w, img_area_h, subjects_json)
right  = left + img_area_w
bottom = top + img_area_h
img_cropped = img_resized.crop((left, top, right, bottom))
```

### 4.5 依赖安装

```bash
cd /home/andy/inktime
/home/andy/inktime/venv/bin/pip install ultralytics
```

模型文件 `yolov8n.pt` 首次运行时自动下载（~6MB），缓存在 `~/.cache/ultralytics/`。

---

## 五、阶段二（按需）

**触发条件：** 遇到 YOLO 检测到多个主体（如 3+ 张脸），裁切框装不下所有主体时。

**方案：** VLM 仲裁
- 在 VLM prompt 中增加指令："画面中检测到 N 个主体，请指出哪个是画面焦点"
- 用焦点主体的 bbox 作为裁切锚点，而非所有主体的联合框
- 需要修改 `call_vlm()` 的 prompt 和返回格式

**暂不实施，等阶段一上线后观察实际需求。**

---

## 六、兼容性处理

| 场景 | 处理方式 |
|------|----------|
| 已分析的老照片（无 subjects 数据） | `subjects_json` 为 NULL，回退到居中裁切 |
| YOLO 未检测到任何主体 | `subjects_json` 为空数组，回退到居中裁切 |
| YOLO 推理失败（模型加载错误等） | try/except 捕获，回退到居中裁切，不影响主流程 |
| 极端比例照片（如全景图） | 合并框超出裁切区域时，回退到居中裁切 |

---

## 七、验证步骤

1. **单元验证：** 对 10 张已分析照片运行 `detect_subjects()`，检查 bbox 是否合理
2. **渲染对比：** 对同一张照片分别用居中裁切和智能裁切渲染，目视对比
3. **边界测试：** 测试无人物/宠物的风景照、多人大合影、横竖屏极端比例
4. **性能测试：** 对全库照片批量跑 YOLO，统计总耗时
5. **回归测试：** 确认原有选片、评分、文案流程不受影响

---

## 八、文件改动清单

| 文件 | 改动类型 | 说明 |
|------|----------|------|
| `analyze_photos.py` | 新增函数 + 修改入库逻辑 | 加入 `detect_subjects()`，入库时写入 `subjects_json` |
| `photos.db` | DDL | `ALTER TABLE photo_scores ADD COLUMN subjects_json TEXT` |
| `render_daily_photo.py` | 替换裁切逻辑 | 用 `compute_crop_window()` 替换居中裁切 |
| `requirements.txt` | 新增依赖 | 加入 `ultralytics` |
| `config.py` | 可选 | 加入 `YOLO_CONF_THRESHOLD`、`YOLO_CLASSES` 等配置项 |

---

## 九、回滚方案

改动均为增量式，回滚简单：

- 删除 `subjects_json` 列：`ALTER TABLE photo_scores DROP COLUMN subjects_json`
- 恢复 `render_daily_photo.py` 的居中裁切逻辑（保留原代码注释即可）
- `ultralytics` 可卸载不影响其他功能
