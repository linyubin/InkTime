# GEMINI.md - InkTime 项目指南

## 项目概览
InkTime 是一个集成 AI 视觉能力的墨水屏电子相框项目。它通过分析用户的本地照片库，利用大语言模型（VLM）识别照片内容、进行回忆度评分并撰写旁白，最终将“历史上的今天”中最具回忆价值的照片推送到 ESP32 控制的墨水屏上展示。

### 核心流程
1. **照片分析 (Python)**：扫描 `IMAGE_DIR`，提取 EXIF/GPS，调用 VLM (如 Qwen-VL) 进行评分和文案生成，结果存入 SQLite。
2. **图片渲染 (Python)**：根据日期筛选高分照片，渲染成墨水屏专用的 `.bin` 文件。
3. **展示端 (ESP32)**：ESP32 定时从服务器拉取渲染好的文件并刷新屏幕，随后进入深度休眠。

## 技术栈
- **后端**: Python 3.10+, Flask, SQLite, Pillow, Requests
- **硬件/固件**: ESP32-S3, Arduino, GxEPD2 库
- **AI**: 支持 OpenAI 兼容接口的视觉模型 (LM Studio, Ollama, 云端 API)
- **其他工具**: `exiftool` (用于获取 GPS 信息)

## 构建与运行

### 1. 环境准备
```bash
# 创建并激活虚拟环境
python -m venv venv
./venv/Scripts/activate  # Windows
source venv/bin/activate # Linux/macOS

# 安装依赖
pip install -r requirements.txt
```

### 2. 配置说明
1. 复制模板：`cp config-example.py config.py`
2. 编辑 `config.py`：
   - `IMAGE_DIR`: 照片库路径
   - `API_URL` & `MODEL_NAME`: VLM 接口配置
   - `DOWNLOAD_KEY`: 给 ESP32 下载路径加的简单口令

### 3. 运行步骤
- **第一步：分析照片**
  ```bash
  python analyze_photos.py
  ```
- **第二步：手动触发选片渲染（可选，通常由定时任务执行）**
  ```bash
  python render_daily_photo.py
  ```
- **第三步：启动服务器**
  ```bash
  python server.py
  ```
  访问 `http://127.0.0.1:8765/review` 可进入 WebUI 预览分析结果。

### 4. ESP32 固件烧录
- 路径: `esp32/ink-display-7C-photo/ink-display-7C-photo.ino`
- 工具: Arduino IDE
- 开发板选择: ESP32-S3 Dev Module (开启 PSRAM)
- 依赖库: `GxEPD2`

## 开发约定
- **配置驱动**: 所有的路径、API 密钥、阈值参数都应放在 `config.py` 中，严禁硬编码。
- **数据库**: 使用 `photos.db` (SQLite) 存储所有照片的元数据。
- **提示词**: 分析照片的 System Prompt 在 `analyze_photos.py` 的 `generate_side_caption` 和 `analyze_image_content` 函数中定义。
- **硬件适配**: 若更换不同尺寸的墨水屏，需修改固件中的 `GxEPD2` 构造函数以及 `render_daily_photo.py` 中的渲染尺寸。

## 关键目录说明
- `esp32/pcb/`: 包含硬件原理图、BOM 和 PCB 制板文件 (Gerber)。
- `data/`: 存放离线地理位置索引 `world_cities_zh.csv`。
- `scripts/`: 包含自动化运行脚本，如 `inktime_daily.sh`。
- `output/`: 存放生成的墨水屏 `.bin` 渲染文件。
