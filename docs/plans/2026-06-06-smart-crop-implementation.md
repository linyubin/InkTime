# Smart Crop Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement composition-aware golden ratio smart crop using YOLOv8-nano detection and SQLite storage.

**Architecture:** 
1. PC-side `analyze_photos.py` runs YOLOv8-nano to extract subject bounding boxes and saves them to `photo_scores` table as `subjects_json`.
2. Rendering pipeline (`render_daily_photo.py` and `server.py`) reads `subjects_json` and computes a dynamic crop window using a golden-ratio mapping algorithm.
3. No PyTorch dependencies needed on the rendering hardware.

**Tech Stack:** Python, SQLite, ultralytics (YOLO), Pillow.

---

### Task 1: Update Configuration

**Files:**
- Modify: `config.py`

**Step 1: Write configuration additions**
Modify `config.py` to add the following constants at the end of the file:
```python
# ── 智能裁切 YOLO 配置 ─────────────────────────────────────────
YOLO_CONF_THRESHOLD = 0.3
YOLO_CLASSES = ["person", "cat", "dog", "bird", "horse"]
YOLO_SUBJECT_WEIGHTS = {
    "person": 5.0,
    "child": 5.0,
    "cat": 4.0,
    "dog": 4.0,
}
```

**Step 2: Commit**
```bash
git add config.py
git commit -m "feat: add YOLO crop configuration"
```

### Task 2: Database and Subject Detection (analyze_photos.py)

**Files:**
- Modify: `analyze_photos.py`

**Step 1: Add YOLO logic to `analyze_photos.py`**
In `analyze_photos.py`, add `_yolo_model` global and `detect_subjects` function near `encode_image_to_b64`.

```python
_yolo_model = None

def detect_subjects(image_path: Path) -> list[dict]:
    global _yolo_model
    try:
        from ultralytics import YOLO
    except ImportError:
        return []
        
    if _yolo_model is None:
        _yolo_model = YOLO("yolov8n.pt")
        
    try:
        results = _yolo_model(str(image_path), verbose=False)
    except Exception as e:
        print(f"[WARN] YOLO 检测失败: {e}")
        return []
        
    subjects = []
    classes_to_detect = set(getattr(cfg, "YOLO_CLASSES", ["person", "cat", "dog"]))
    conf_thresh = getattr(cfg, "YOLO_CONF_THRESHOLD", 0.3)
    
    for r in results:
        for box in r.boxes:
            cls_name = r.names[int(box.cls)]
            if cls_name not in classes_to_detect:
                continue
            conf = float(box.conf)
            if conf < conf_thresh:
                continue
            
            x1, y1, x2, y2 = box.xyxy[0].tolist()
            img_h, img_w = r.orig_shape
            
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

**Step 2: Modify DB Insertion**
Update `ensure_table` to add `subjects_json TEXT`. Use `ALTER TABLE photo_scores ADD COLUMN subjects_json TEXT` wrapped in try/except.
In the `main()` loop, after `call_vlm`, call `detect_subjects(path)` and serialize it to json.
Update the `INSERT OR REPLACE INTO photo_scores` query to include `subjects_json` column.

**Step 3: Commit**
```bash
git add analyze_photos.py
git commit -m "feat: integrate YOLOv8 subject detection into analysis"
```

### Task 3: Golden Ratio Crop Calculation (`render_daily_photo.py`)

**Files:**
- Modify: `render_daily_photo.py`

**Step 1: Add compute_crop_window function**
Implement `compute_crop_window` in `render_daily_photo.py`.

```python
def compute_crop_window(draw_w: int, draw_h: int, img_area_w: int, img_area_h: int, subjects_json: str) -> tuple[int, int]:
    # Default center crop
    default_left = max(0, (draw_w - img_area_w) // 2)
    default_top  = max(0, (draw_h - img_area_h) // 2)

    if not subjects_json:
        return default_left, default_top

    try:
        import json
        subjects = json.loads(subjects_json)
        if not subjects:
            return default_left, default_top
            
        weights_map = getattr(cfg, "YOLO_SUBJECT_WEIGHTS", {"person": 5.0, "cat": 4.0, "dog": 4.0})
        
        # Select highest weighted subject (simplest approach for multi-subject conflict for now)
        best_subject = None
        best_score = -1
        for s in subjects:
            score = s["conf"] * weights_map.get(s["label"], 1.0)
            if score > best_score:
                best_score = score
                best_subject = s
                
        if not best_subject:
            return default_left, default_top

        cx_rel, cy_rel, _, _ = best_subject["bbox"]
        cx_pixel = cx_rel * draw_w
        cy_pixel = cy_rel * draw_h
        
        # Golden ratio calculation
        target_rel_x = max(0.382, min(0.618, cx_rel))
        target_rel_y = max(0.382, min(0.618, cy_rel))
        
        left = int(cx_pixel - target_rel_x * img_area_w)
        top = int(cy_pixel - target_rel_y * img_area_h)
        
        left = max(0, min(left, draw_w - img_area_w))
        top = max(0, min(top, draw_h - img_area_h))
        
        return left, top
    except Exception as e:
        print(f"[WARN] compute_crop_window error: {e}")
        return default_left, default_top
```

**Step 2: Integrate into `render_image`**
Modify `render_daily_photo.py` `render_image` function to accept `subjects_json` from `item` and call `compute_crop_window` to replace the center crop logic.
Modify `load_sim_rows` and `choose_photos_for_today` related dicts to extract `subjects_json` from DB (make sure to fetch `subjects_json` in the `SELECT` query in `load_sim_rows` and `load_sim_rows_for_dates` if necessary). Wait, `load_sim_rows` has a SELECT statement, we need to add `subjects_json` there.

**Step 3: Commit**
```bash
git add render_daily_photo.py
git commit -m "feat: implement composition-aware golden ratio cropping"
```

### Task 4: Update Server Metadata (`server.py`)

**Files:**
- Modify: `server.py`

**Step 1: Modify get_photo_meta_by_path**
Update the SELECT query in `get_photo_meta_by_path` to include `subjects_json`. Update the return dict to include `"subjects_json": subjects_json or ""`.

**Step 2: Modify load_rows, load_sim_rows_for_dates**
Ensure that any SQL queries fetching row data for rendering in `server.py` also fetch `subjects_json` and pass it along in the metadata so `/sim_render` can use it.

**Step 3: Commit**
```bash
git add server.py
git commit -m "feat: expose subjects_json in server photo metadata"
```
