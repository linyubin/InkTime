## 整理 config.py 与 config-example.py 的分类方案

### 分类依据
按 **InkTime 数据流（配置项首次被读取并使用的阶段）** 归类，让用户改某一环节时相关配置聚在一起。消费者归属已通过全仓库 `getattr(cfg, "XXX")` / `cfg.XXX` 搜索确认。

### 分类结构（两个文件统一，7 大段）

每段用 `# ═══ XX ════════════════════════════════════════` 标题分隔，标题点明用途与对应脚本。

| 段号 | 标题 | 包含配置项 | 主消费者 |
|------|------|-----------|----------|
| ① | 照片库与数据库路径 | `IMAGE_DIR`, `DB_PATH`, `PATH_MAP`, `EXCLUDE_KEYWORDS` | analyze/render/server 通用 |
| ② | VLM 分析（首次打分/打标签） | `API_URL`, `MODEL_NAME`, `API_KEY`, `BATCH_LIMIT`, `TIMEOUT` | analyze_photos.py |
| ③ | 评分与加权（选片核心） | `MEMORY_THRESHOLD`, `DAILY_PHOTO_QUANTITY`, `SCORE_MEMORY_WEIGHT`, `SCORE_BEAUTY_WEIGHT`(标注未生效), `HIGH_SCORE_THRESHOLD`, `RESHOW_AFTER_DAYS`, `RECENCY_PENALTY`, `PATH_WEIGHTS`, `CATEGORY_WEIGHTS`, `PORTRAIT_BOOST` | render_daily_photo.py |
| ④ | 地理位置（旅行加成/城市解析） | `WORLD_CITIES_CSV`, `CITY_GRID_DEG`, `HOME_LAT`, `HOME_LON`, `HOME_RADIUS_KM`, `CITY_MAX_DISTANCE_KM` | analyze_photos.py |
| ⑤ | YOLO 智能裁切 | `YOLO_CONF_THRESHOLD`, `YOLO_CLASSES`, `YOLO_SUBJECT_WEIGHTS` | analyze + render |
| ⑥ | 渲染输出（墨水屏画面生成） | `BIN_OUTPUT_DIR`, `FONT_PATH`, `ENABLE_FRAME_ROTATION`, `LANDSCAPE_CANVAS_WIDTH`, `LANDSCAPE_CANVAS_HEIGHT` | render_daily_photo.py |
| ⑦ | 投递：ESP32 拉取 / BLE 推送 | 顶部"能力开关"块 + ESP32 子段(`DOWNLOAD_KEY`, `FLASK_HOST`, `FLASK_PORT`, `ENABLE_REVIEW_WEBUI`, `ESP32_SERVE_WAIT_MIN`) + BLE 子段(`EPD_DEVICE_MAC`, `EPD_BLE_CHUNK_SIZE`, `EPD_ROTATE`) | inktime_daily.sh / server.py / push_to_epd_ble.py |

### 两个文件的处理差异
- **config.py**：纯重新分组排列，**值不动**（包括刚加的 `ENABLE_FRAME_ROTATION=True` 等），只调顺序 + 补段标题 + 必要注释。
- **config-example.py**：按同样 7 段重组，**并补齐缺失项**（`PATH_WEIGHTS`, `SCORE_MEMORY_WEIGHT`, `SCORE_BEAUTY_WEIGHT`, `HIGH_SCORE_THRESHOLD`, `RESHOW_AFTER_DAYS`, `RECENCY_PENALTY`, `HOME_LAT/LON/RADIUS_KM` 示例值等），配示例值与中文注释，成为可直接复制使用的完整模板。

### 关键处理点
1. **投递段顶部放"能力开关"**：`ENABLE_BLE_PUSH` / `ENABLE_ESP32_SERVE` 放在 ⑦ 段最上方（这是用户最先要决策的总开关），下面再分 ESP32 / BLE 两个子段。
2. **`EPD_ROTATE` 加注释**：明确标注"仅作用于 BLE 推送（push_to_epd_ble.py），与 ESP32 横屏渲染方案无关"，避免混淆（回应上一轮讨论）。
3. **`SCORE_BEAUTY_WEIGHT` 保留 + 标注**：两个文件都保留该行，加注释"⚠️ 当前未生效，render 用 1 - SCORE_MEMORY_WEIGHT 隐式推导；保留以便未来独立调参"。
4. **删除冗余的"未来扩展预留"注释**：`# ENABLE_SERVO_CONTROL = False` 这行历史占位删除（舵机已迁移到 ESP32 固件侧 servo_calibrated，Python 侧不再需要）。

### 不做的事
- 不改任何配置项的**名字**（避免破坏 `getattr` 读取）。
- 不改 config.py 的任何**值**（用户当前运行环境保持不变）。
- 不动 render_daily_photo.py 等消费代码。

### 验证
整理完成后跑一次 `python3 -c "import config"` 确认两文件语法无误、字段齐全。