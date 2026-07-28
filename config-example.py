# =========================================================
#  InkTime 配置模板
#  复制本文件为 config.py，按需修改各段取值即可。
#  分段按数据流组织：每段标题标注了【主消费者脚本】，
#  改某个环节时只需关注对应那一段。
# =========================================================


# ═══ ① 照片库与数据库路径 ═════════════════════════════
# （analyze_photos.py / render_daily_photo.py / server.py 通用）

# 照片库路径（你自己的相册目录）
#   Windows:  r"\\10.168.1.111\Photos"
#   Linux:    "/mnt/nas/Photos"
IMAGE_DIR = "./test"

# 数据库路径（建议保持默认）
DB_PATH = "./photos.db"

# 排除包含以下关键词或特征的路径或文件名（全路径匹配，不区分大小写）
EXCLUDE_KEYWORDS = ["screenshot", "截屏", "屏幕截图", "cache"]

# 跨平台路径映射
# 如在 Windows 上生成数据库后拷贝到 Linux/树莓派运行，在此配置可将 Windows 路径前缀映射到 Linux 路径
# 例: {"\\\\10.168.1.111\\Photos": "/home/pi/Photos"}
PATH_MAP = {}


# ═══ ② VLM 分析 ═══════════════════════════════════════
# （analyze_photos.py —— 首次打分、打标签、识别城市）
# 本段仅在「分析照片库」阶段使用，日常每日渲染不读这里。

# VLM 模型接口（如 LM Studio / Ollama）
API_URL = "http://127.0.0.1:11434/api"
MODEL_NAME = "qwen3.5:9b"
API_KEY = "ollama"

# 每次最多处理多少张的图片（None 表示不限制）
BATCH_LIMIT = None

# 请求超时时间（秒）
TIMEOUT = 600


# ═══ ③ 评分与加权（选片核心）═══════════════════════════
# （render_daily_photo.py —— 每日选片决策）

# 每日选片“精彩度”阈值，低于此值的照片不进入候选
MEMORY_THRESHOLD = 70.0

# 每日挑选的照片数量
DAILY_PHOTO_QUANTITY = 5

# memory 与 beauty 在最终得分中的权重（合计建议为 1.0）
SCORE_MEMORY_WEIGHT: float = 0.85
# ⚠️ 当前未生效：render 实际用 (1 - SCORE_MEMORY_WEIGHT) 隐式推导 beauty 权重。
#    保留此字段以便未来独立调参，目前改它无效果。
SCORE_BEAUTY_WEIGHT: float = 0.15

# final_score 超过此阈值的照片，从 Top-N 随机选 1 张；否则取得分最高的 1 张
HIGH_SCORE_THRESHOLD: float = 87.0

# 防重复展示：展示过的照片在多少天内再次进入候选时会被降权
RESHOW_AFTER_DAYS: int = 180
# 近期展示过的照片的 final_score 乘数（0~1，越小惩罚越重）
RECENCY_PENALTY: float = 0.9

# 路径权重：照片路径前缀匹配（最长前缀优先，不匹配则 1.0）
# 支持 Windows 原始路径 或 Linux 映射后路径（两种格式均可，代码自动适配）
# 例：
#   降权  r"\\10.168.1.111\Photos\MtPhotos_upload\clj\vivo X80": 0.7
#   屏蔽  r"\\10.168.1.111\Photos\Others": 0.1
#   加权  r"\\10.168.1.111\Photos\Timeline\Timeline@布丁": 1.4
PATH_WEIGHTS: dict = {
    # 相对路径写法（自动拼上 IMAGE_DIR）
    # "/Timeline/Timeline@布丁/布丁-幼儿园": 0.01,
    # "/Timeline/Timeline@布丁":             1.3,
    # "/MtPhotos_upload":                    1.1,
}

# 分类权重：匹配 type 字段中的分类关键词（多个分类取平均值，不匹配则 1.0）
CATEGORY_WEIGHTS: dict = {
    "家庭": 1.5,
    "旅行": 1.5,
    "美食": 1.1,
    "截图": 0.1,
    "文档": 0.1,
}

# 肖像加成：在每日选片之后对已选照片列表进行二次重排序
# 凡照片 type 字段包含以下分类之一，展示优先级（_display_score）将被提升
# 多个分类命中时取最大值（不平均，避免多标签稀释加成效果）
# 不在列表中的分类默认乘数为 1.0（不改变顺序）
# 注意：此配置仅影响展示顺序，不影响 choose_photos_for_today() 的选片逻辑
PORTRAIT_BOOST: dict = {
    "人物": 1.5,
    "宠物": 1.5,
    "猫咪": 1.4,
    "孩子": 1.4,
    "家庭": 1.3,
    "合影": 1.2,
}


# ═══ ④ 地理位置 ═══════════════════════════════════════
# （analyze_photos.py —— 旅行加成、城市名解析）
# 本段在「分析照片库」阶段写入数据库；每日渲染只读数据库结果。

# 离线中文城市名索引，使用 geonames 数据制作
WORLD_CITIES_CSV = "./data/world_cities_zh.csv"

# 网格大小（纬度/经度度数）；越大越快但精度略差。1.0 对大多数场景够用。
CITY_GRID_DEG = 1.0

# 你的“常驻地”坐标（用于判断是否为旅行期间照片，从而对评分进行小幅加成）
# 照片 GPS 距离常驻地超过 HOME_RADIUS_KM，则视为“异地”
HOME_LAT = 22.543096
HOME_LON = 114.057865
HOME_RADIUS_KM = 60.0

# 最大接受距离（公里），超出则认为“不在任何城市附近”
CITY_MAX_DISTANCE_KM = 100.0


# ═══ ⑤ YOLO 智能裁切 ═══════════════════════════════════
# （analyze_photos.py 写入主体框；render_daily_photo.py 渲染时据框居中裁切）

YOLO_CONF_THRESHOLD = 0.3
YOLO_CLASSES = ["person", "cat", "dog", "bird", "horse"]
YOLO_SUBJECT_WEIGHTS = {
    "person": 5.0,
    "child": 5.0,
    "cat": 4.0,
    "dog": 4.0,
}


# ═══ ⑥ 渲染输出（墨水屏画面生成）═══════════════════════
# （render_daily_photo.py —— 选片后渲染 480×800 / 800×480 画面）

# 墨水屏渲染 BIN 文件输出目录
# server.py 在 /static/inktime/<DOWNLOAD_KEY>/ 下提供下载；传输日志见 logs/transfer.log
BIN_OUTPUT_DIR = "./output"

# 自定义字体路径（为空则退回默认字体）
FONT_PATH = ""

# ── 相框横屏自动旋转（需舵机硬件支持）─────────────────────
# 启用后：横排照片走 800×480 横屏渲染管线，ESP32 拉取 photo_N.json sidecar
#         拿朝向后转舵机 90° 横屏显示。
# 关闭时：所有照片走原竖屏 480×800 竖屏管线，sidecar 永远报 portrait，
#         对无舵机的老设备零感知（向后兼容）。
# 详见 docs/plans/2026-07-25-frame-rotation-design.md
ENABLE_FRAME_ROTATION = False
LANDSCAPE_CANVAS_WIDTH  = 800
LANDSCAPE_CANVAS_HEIGHT = 480


# ═══ ⑦ 投递：ESP32 拉取 / BLE 推送 ════════════════════
# （inktime_daily.sh 读取能力开关；server.py / push_to_epd_ble.py 读取各自参数）

# ── 投递能力开关（由 scripts/inktime_daily.sh 读取，决定 render 之后跑哪些投递步骤）──
# 两者可并存，互不干扰。
ENABLE_BLE_PUSH    = True   # True: render 后 BLE 推送到 nRF5 设备（push_to_epd_ble.py）
ENABLE_ESP32_SERVE = True   # True: render 后临时启动 server.py 供 ESP32 主动拉取

# ───── ESP32 拉取（server.py）── 仅 ENABLE_ESP32_SERVE=True 时生效 ─────
# 为防止照片隐私泄露，建议为 ESP32 下载路径加一个随机前缀作为密钥
# 前缀修改后，请同步修改 esp32/ink-display-7C-photo/ink-display-7C-photo.ino 固件中的 DAILY_PHOTO_PATH_PREFIX 字段
DOWNLOAD_KEY = "yourdownloadkey"

# Flask 静态服务
FLASK_HOST = "0.0.0.0"
FLASK_PORT = 8765

# 是否开启照片库 WebUI（前期检验提示词选片效果时使用，跑通后建议关闭）
ENABLE_REVIEW_WEBUI = True

# ESP32 拉取等待：render 后 server 最多在线多久（分钟），超时未拉取则关闭
ESP32_SERVE_WAIT_MIN = 15

# ───── BLE 推送（push_to_epd_ble.py）── 仅 ENABLE_BLE_PUSH=True 时生效 ─────
# 目标墨水屏设备的蓝牙 MAC 地址
# 填写后 push_to_epd_ble.py 将跳过扫描步骤，直接连接（推荐，更快更稳定）
# 在 NAS 上可通过以下命令获取：
#   sudo bluetoothctl
#   > power on
#   > scan on
#   > (等待出现 NRF_EPD_XXXX，记录其 MAC 地址)
EPD_DEVICE_MAC = ""  # 例："AA:BB:CC:DD:EE:FF"

# BLE 传输块大小（字节），单次 GATT write 的 data 部分（不含命令头）
# 默认 238（MTU 244 - 6 字节开销），连接不稳或数据错乱时可调小至 128 或 64
EPD_BLE_CHUNK_SIZE = 238

# 墨水屏图像旋转方向（仅作用于 BLE 推送路径，push_to_epd_ble.py 读取）
# ⚠️ 与上方 ESP32 横屏渲染方案（ENABLE_FRAME_ROTATION）无关：
#    ESP32 画面朝向由固件内 rotate180/landscape_invert + 舵机标定角度独立决定。
# 可选值：
#   "CW90"   - 顺时针旋转 90°（屏幕逆时针 90° 竖放时使用）
#   "CCW90"  - 逆时针旋转 90°（屏幕顺时针 90° 竖放时使用）
#   "ROT180" - 旋转 180°（上下颠倒时使用）
#   "NONE"   - 不旋转（横屏使用，或调试用）
# 如果显示内容整体是镜像或颠倒的，请切换 CW90 / CCW90 再试。
EPD_ROTATE = "CW90"
