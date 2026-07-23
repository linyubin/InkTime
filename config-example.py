# 照片库路径（你自己的相册目录）
IMAGE_DIR = "./test"

# 数据库路径（建议保持默认）
DB_PATH = "./photos.db"

# VLM 模型接口（如 LM Studio）
API_URL = "http://127.0.0.1:11434/api"
MODEL_NAME = "qwen3.5:9b"
API_KEY = "ollama"

# 每次最多处理多少张的图片
BATCH_LIMIT = None

# 请求超时时间（秒）
TIMEOUT = 600

# 为防止照片隐私泄露，建议为 ESP32 下载路径加一个随机前缀作为密钥
# 前缀修改后，请同步修改 esp32/ink-display-7C-photo/ink-display-7C-photo.ino 固件中的 DAILY_PHOTO_PATH_PREFIX 字段）
DOWNLOAD_KEY = "yourdownloadkey"

# Flask 静态服务
FLASK_HOST = "0.0.0.0"
FLASK_PORT = 8765
# 是否开启照片库 WebUI（前期检验提示词选片效果时使用，跑通后建议关闭）
ENABLE_REVIEW_WEBUI = True

# ── 投递/能力开关（可并存，互不干扰；为未来功能如舵机控制留扩展）──
# 由 scripts/inktime_daily.sh 读取，决定 render 之后跑哪些投递步骤
ENABLE_BLE_PUSH    = True   # True: render 后 BLE 推送到 nRF5 设备（push_to_epd_ble.py）
ENABLE_ESP32_SERVE = True   # True: render 后临时启动 server.py 供 ESP32 主动拉取
# ESP32 拉取等待：render 后 server 最多在线多久（分钟），超时未拉取则关闭
ESP32_SERVE_WAIT_MIN = 15
# 未来扩展预留：
# ENABLE_SERVO_CONTROL = False

# 离线中文城市名索引，使用 geonames 数据制作
WORLD_CITIES_CSV = "./data/world_cities_zh.csv"

# 网格大小（纬度/经度度数）；越大越快但精度略差。1.0 对大多数场景够用。
CITY_GRID_DEG = 1.0

# 你的“常驻常驻”坐标（用于判断是否为旅行期间照片，从而对评分进行小幅加成）
# 照片 GPS 距离常驻地超过 HOME_RADIUS_KM，则视为“异地”
# 默认值给了深圳市中心附近（不改也能保持原行为的大致效果）
HOME_LAT = 22.543096
HOME_LON = 114.057865
HOME_RADIUS_KM = 60.0

# 最大接受距离（公里），超出则认为“不在任何城市附近”
CITY_MAX_DISTANCE_KM = 100.0

# 墨水屏渲染 BIN 文件输出目录（server.py 在 /static/inktime/<DOWNLOAD_KEY>/ 下提供下载；传输日志见 logs/transfer.log）
BIN_OUTPUT_DIR = "./output"

# 自定义字体路径（为空则退回默认字体）
FONT_PATH = ""

# 每日选片“精彩度”阈值
MEMORY_THRESHOLD = 70.0

# 每日挑选的照片数量
DAILY_PHOTO_QUANTITY = 5

# 排除包含以下关键词或特征的路径或文件名（全路径匹配，不区分大小写）
EXCLUDE_KEYWORDS = ["screenshot", "截屏", "屏幕截图", "cache"]

# ── 加权选片配置 ───────────────────────────────────────────
# 分类权重：匹配 type 字段中的分类关键词（多个分类取平均值，不匹配则 1.0）
CATEGORY_WEIGHTS: dict = {
    "家庭": 1.5,
    "旅行": 1.5,
    "美食": 1.1,
    "截图": 0.1,
    "文档": 0.1,
}

# 肖像加成：在每日选片之后对已选照片列表进行二次重排序
# 凡照片 type 字段包含以下分类之一，展示优先级将被提升
# 多个分类命中时取最大值（避免多标签稀释加成），不在列表中的分类默认乘数为 1.0
PORTRAIT_BOOST: dict = {
    "人物": 1.5,
    "宠物": 1.5,
    "猫咪": 1.4,
    "孩子": 1.4,
    "家庭": 1.3,
    "合影": 1.2,
}

# ── EPD-nRF5 蓝牙推送配置 ────────────────────────────────
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

# ── 跨平台与路径配置 ─────────────────────────────────────
# 跨平台路径映射（如在 Windows 上生成数据库后拷贝到 Linux/树莓派运行，在此配置可将 Windows 路径前缀瞬间映射到 Linux 路径）
# 例: {"\\\\10.168.1.111\\Photos": "/home/pi/Photos"} 
PATH_MAP = {}

# 墨水屏图像旋转方向
# 硬件原生为横屏 (800×480)，竖屏放置时需要旋转图像数据。
# 可选值：
#   "CW90"   - 顺时针旋转 90°（屏幕逆时针 90° 竖放时使用）
#   "CCW90"  - 逆时针旋转 90°（屏幕顺时针 90° 竖放时使用）
#   "ROT180" - 旋转 180°（上下颠倒时使用）
#   "NONE"   - 不旋转（横屏使用，或调试用）
# 如果显示内容整体是镜像或颠倒的，请切换 CW90 / CCW90 再试。
EPD_ROTATE = "CW90"

# ── 智能裁切 YOLO 配置 ─────────────────────────────────────────
YOLO_CONF_THRESHOLD = 0.3
YOLO_CLASSES = ["person", "cat", "dog", "bird", "horse"]
YOLO_SUBJECT_WEIGHTS = {
    "person": 5.0,
    "child": 5.0,
    "cat": 4.0,
    "dog": 4.0,
}
