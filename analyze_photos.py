#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from pathlib import Path
import base64
import json
import sqlite3
import os
import subprocess
import time
import requests
import io
import re
import datetime
from PIL import Image, ExifTags, ImageOps, ImageFile
ImageFile.LOAD_TRUNCATED_IMAGES = True
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass
import config as cfg
from common import normalize_type_to_list, parse_datetime_from_filename, resolve_chat_endpoint
import shutil
from photo_scan_safety import scandir_with_retries, ScanIncompleteError, count_missing_paths
from db_schema import MigrationError, require_current_schema, open_current_database

_yolo_model = None
_pose_model = None

# COCO-pose 面部关键点索引：0 鼻、1 左眼、2 右眼、3 左耳、4 右耳
_FACE_KPT_IDS = (0, 1, 2, 3, 4)
_EYE_IDS = (1, 2)
_EAR_IDS = (3, 4)


def _face_box_from_pose(kpt_xy, kpt_conf, person_box_px, img_w, img_h) -> list | None:
    """由 COCO-pose 面部关键点派生归一化脸框 [cx, cy, bw, bh]（与 bbox 同格式）。

    脸宽锚点优先级：双耳间距×1.15 > 双眼间距×2.4 > 人物框高×0.12（兜底）；
    眼线约在脸高 40% 处、鼻约 52%，据此从锚点反推框顶，再裁回图像内。
    无任何可见面部关键点（侧脸/遮挡/人过小）返回 None——宁缺毋滥，
    选型端对无 face 字段的数据退回主体级防护。
    """
    conf_lim = float(getattr(cfg, "YOLO_POSE_KPT_CONF", 0.3))
    pts = {i: (float(kpt_xy[i][0]), float(kpt_xy[i][1]))
           for i in _FACE_KPT_IDS
           if i < len(kpt_conf) and float(kpt_conf[i]) >= conf_lim}
    if not pts:
        return None
    cx = sum(p[0] for p in pts.values()) / len(pts)
    px1, py1, px2, py2 = person_box_px
    person_h = max(1.0, py2 - py1)
    if len(set(_EAR_IDS) & pts.keys()) == 2:
        face_w = 1.15 * abs(pts[3][0] - pts[4][0])
        anchor_y = (pts[3][1] + pts[4][1]) / 2
        anchor_t = 0.42
    elif len(set(_EYE_IDS) & pts.keys()) == 2:
        face_w = 2.4 * abs(pts[1][0] - pts[2][0])
        anchor_y = (pts[1][1] + pts[2][1]) / 2
        anchor_t = 0.40
    elif 0 in pts:
        face_w = 0.12 * person_h
        anchor_y, anchor_t = pts[0][1], 0.52
    else:
        face_w = 0.12 * person_h
        anchor_y, anchor_t = next(iter(pts.values()))[1], 0.45
    face_w = max(face_w, 0.05 * img_w)  # 关键点间距过小时保底，防零尺寸框
    face_h = 1.35 * face_w
    top = anchor_y - anchor_t * face_h
    x1, y1 = max(0.0, cx - face_w / 2), max(0.0, top)
    x2 = min(float(img_w), cx + face_w / 2)
    y2 = min(float(img_h), top + face_h)
    if x2 <= x1 or y2 <= y1:
        return None
    return [round((x1 + x2) / 2 / img_w, 4), round((y1 + y2) / 2 / img_h, 4),
            round((x2 - x1) / img_w, 4), round((y2 - y1) / img_h, 4)]


def _attach_faces(image_path: str, subjects: list) -> None:
    """给 person 条目附加 pose 关键点派生的脸框（face 字段，2026-10-24）。

    pose 模型只认人：仅当检测出 person 时才加载/推理，宠物照零开销。
    与检测框按 IoU≥0.3 配对；任何失败静默降级为无脸数据，选型端退回
    主体级防护（不阻断分析主流程）。
    """
    global _pose_model
    try:
        from ultralytics import YOLO
        if _pose_model is None:
            _pose_model = YOLO(str(getattr(cfg, "YOLO_POSE_MODEL", "yolov8n-pose.pt")))
        results = _pose_model(image_path, verbose=False)
    except Exception as e:
        print(f"[WARN] pose 脸部关键点失败（退回无脸数据）: {e}")
        return

    for r in results:
        img_h, img_w = r.orig_shape
        if r.keypoints is None or not len(r.boxes):
            continue
        pose_boxes = r.boxes.xyxy.tolist()
        kpts_xy = r.keypoints.xy.tolist()
        kpts_cf = (r.keypoints.conf.tolist()
                   if r.keypoints.conf is not None else None)
        for s in subjects:
            if s.get("label") != "person" or "face" in s:
                continue
            cx, cy, bw, bh = s["bbox"]
            sx1, sy1 = (cx - bw / 2) * img_w, (cy - bh / 2) * img_h
            sx2, sy2 = (cx + bw / 2) * img_w, (cy + bh / 2) * img_h
            best_j, best_iou = None, 0.3
            for j, (qx1, qy1, qx2, qy2) in enumerate(pose_boxes):
                ix1, iy1 = max(sx1, qx1), max(sy1, qy1)
                ix2, iy2 = min(sx2, qx2), min(sy2, qy2)
                inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
                union = ((sx2 - sx1) * (sy2 - sy1)
                         + (qx2 - qx1) * (qy2 - qy1) - inter)
                iou = inter / union if union > 0 else 0.0
                if iou > best_iou:
                    best_iou, best_j = iou, j
            if best_j is None:
                continue
            face = _face_box_from_pose(
                kpts_xy[best_j],
                kpts_cf[best_j] if kpts_cf else [1.0] * len(kpts_xy[best_j]),
                (sx1, sy1, sx2, sy2), img_w, img_h)
            if face:
                s["face"] = face


def detect_subjects(image_path: Path) -> list:
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
    if any(s.get("label") == "person" for s in subjects):
        _attach_faces(str(image_path), subjects)
    return subjects

# =======================
# NAS 掉盘守护（macOS /Volumes）
# =======================
NAS_MOUNT_URL = str(getattr(cfg, "NAS_MOUNT_URL", "") or "").strip()
NAS_MOUNT_POINT = Path(str(getattr(cfg, "NAS_MOUNT_POINT", "/Volumes/photo") or "/Volumes/photo")).expanduser()
NAS_RETRY_TIMES = int(getattr(cfg, "NAS_RETRY_TIMES", 3) or 3)
NAS_RETRY_SLEEP_SEC = float(getattr(cfg, "NAS_RETRY_SLEEP_SEC", 2.0) or 2.0)


def _is_mount_ok() -> bool:
    """判断 NAS 是否仍然挂载（尽量保守）。"""
    try:
        # /Volumes 下的网络卷一般是 mount point
        if NAS_MOUNT_POINT and NAS_MOUNT_POINT.exists():
            if os.path.ismount(str(NAS_MOUNT_POINT)):
                return True
        # 兜底：只要图片根目录可访问也算 OK
        return IMAGE_DIR.exists()
    except Exception:
        return False


def _try_remount_nas() -> bool:
    """尝试重挂载 NAS。

    只在配置了 NAS_MOUNT_URL 时执行；优先使用 macOS 的 AppleScript 挂载，
    这样可以复用钥匙串里保存的凭据。
    """
    if not NAS_MOUNT_URL:
        return False

    print(f"[WARN] 检测到 NAS 可能掉盘，尝试重挂载：{NAS_MOUNT_URL}")

    # 1) AppleScript（推荐）：mount volume "afp://..." / "smb://..."
    try:
        subprocess.run(
            ["osascript", "-e", f'mount volume "{NAS_MOUNT_URL}"'],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception:
        pass

    # 2) 兜底：如果你更喜欢命令行挂载，可以自行改成 mount_afp / mount_smbfs。

    # 等待卷出现
    for _ in range(10):
        if _is_mount_ok():
            print("[INFO] NAS 重挂载成功，继续处理。")
            return True
        time.sleep(0.5)

    print("[WARN] NAS 重挂载失败（卷仍不可用）。")
    return False


def _read_bytes_with_nas_retry(path: Path) -> bytes:
    """读文件：遇到 NAS 掉盘类错误时，尝试重挂载并重试。"""
    last_err: Exception | None = None

    # 只对照片库路径内的文件启用重挂载逻辑，避免误伤本地文件。
    try:
        in_photo_dir = str(path).startswith(str(IMAGE_DIR))
    except Exception:
        in_photo_dir = False

    for attempt in range(1, max(1, NAS_RETRY_TIMES) + 1):
        try:
            return path.read_bytes()
        except OSError as e:
            last_err = e

            # 不在照片库目录内，或者没有配置 NAS URL，直接抛
            if not in_photo_dir or not NAS_MOUNT_URL:
                raise

            # 常见网络卷断连错误：57 Socket is not connected；也可能表现为 5/6 等 I/O 类错误。
            # 这里不做过度精确匹配，先判断挂载状态；如果掉盘就尝试重挂载。
            if not _is_mount_ok():
                print(f"[WARN] 读文件失败（第 {attempt}/{NAS_RETRY_TIMES} 次）：{e}")
                ok = _try_remount_nas()
                if ok:
                    # 重挂载后立刻重试
                    continue

            # 如果挂载看起来还 OK，但仍然读失败，按重试策略稍等再试
            if attempt < NAS_RETRY_TIMES:
                print(f"[WARN] 读文件失败（第 {attempt}/{NAS_RETRY_TIMES} 次），稍后重试：{e}")
                time.sleep(max(0.1, NAS_RETRY_SLEEP_SEC))
                continue

            raise

    # 理论上不会到这
    if last_err:
        raise last_err
    raise OSError("读取文件失败")


# ================== 配置区域（来自 config.py） ==================

ROOT_DIR = Path(__file__).resolve().parent

# 要扫描的图片目录
IMAGE_DIR = Path(str(getattr(cfg, "IMAGE_DIR", "") or "")).expanduser()
if not IMAGE_DIR.is_absolute():
    IMAGE_DIR = (ROOT_DIR / IMAGE_DIR).resolve()

# SQLite 数据库路径
DB_PATH = Path(str(getattr(cfg, "DB_PATH", "photos.db") or "photos.db")).expanduser()
if not DB_PATH.is_absolute():
    DB_PATH = (ROOT_DIR / DB_PATH).resolve()

# LM Studio / Ollama / 智谱开放平台等 AI 接口（仍允许用环境变量覆盖）
# API_URL 写完整端点或只写 base 均可，common.resolve_chat_endpoint 自动补全：
#   智谱   ：https://open.bigmodel.cn/api/paas/v4
#   LM Studio：http://127.0.0.1:1234/v1
#   Ollama  ：http://10.168.1.136:11434
API_URL = str(
    getattr(cfg, "API_URL", None)
    or os.environ.get("LMSTUDIO_URL", "http://127.0.0.1:1234/v1/chat/completions")
)
# 解析后的可直连端点与协议（"openai" | "ollama"）
API_ENDPOINT, API_PROTOCOL = resolve_chat_endpoint(API_URL)

# 模型名称（仍允许用环境变量覆盖）
MODEL_NAME = str(
    getattr(cfg, "MODEL_NAME", None)
    or os.environ.get("LMSTUDIO_MODEL", "qwen3-vl-32b-instruct")
)

# API KEY（仍允许用环境变量覆盖）
API_KEY = str(getattr(cfg, "API_KEY", None) or os.environ.get("LMSTUDIO_API_KEY", ""))

# 每次处理多少张；None 为不限制
BATCH_LIMIT = getattr(cfg, "BATCH_LIMIT", None)

# 请求超时时间（秒）
TIMEOUT = float(getattr(cfg, "TIMEOUT", 600) or 600)

# 发送给 VLM 之前，先把图片长边缩放到该值（像素）。
# 0 表示不缩放。
# 本地推理可保持较高值；云端推理建议降低（减少 token/成本）。
VLM_MAX_LONG_EDGE = int(getattr(cfg, "VLM_MAX_LONG_EDGE", 2560) or 2560)

# 中文城市数据库位置
WORLD_CITIES_CSV = Path(str(getattr(cfg, "WORLD_CITIES_CSV", "data/world_cities_zh.csv") or "data/world_cities_zh.csv")).expanduser()
if not WORLD_CITIES_CSV.is_absolute():
    WORLD_CITIES_CSV = (ROOT_DIR / WORLD_CITIES_CSV).resolve()

CITY_GRID_DEG = float(getattr(cfg, "CITY_GRID_DEG", 1.0) or 1.0)
CITY_MAX_DISTANCE_KM = float(getattr(cfg, "CITY_MAX_DISTANCE_KM", 80.0) or 80.0)
HOME_LAT = float(getattr(cfg, "HOME_LAT", 22.543096) or 22.543096)
HOME_LON = float(getattr(cfg, "HOME_LON", 114.057865) or 114.057865)
HOME_RADIUS_KM = float(getattr(cfg, "HOME_RADIUS_KM", 60.0) or 60.0)
# 排除关键字（全路径匹配，不区分大小写；\ 与 / 分隔符自动等价，见 is_excluded）
EXCLUDE_KEYWORDS = getattr(cfg, "EXCLUDE_KEYWORDS", [])

# 跨平台路径映射（来自 config.PATH_MAP）：{数据库路径前缀, 本地路径前缀}
# 数据库历史记录多为 Windows 生成（UNC 路径），而 Linux/树莓派把照片库挂载在
# /mnt/nas/Photos。分析脚本在「查库跳过 / 清理残留 / 写库」三个环节统一把
# 本机扫描路径换算成数据库规范路径，保证跨平台一套库、已分析的不重跑。
PATH_MAP = getattr(cfg, "PATH_MAP", {}) or {}


def to_db_form(path) -> str:
    r"""本机扫描路径 → 数据库规范路径（PATH_MAP 反向映射 + 分隔符归一）。

    例：/mnt/nas/Photos/x.JPG → \\10.168.1.111\Photos\x.JPG；
    无映射命中时原样返回（Windows 本机分析、或未配置映射的场景）。
    """
    s = str(path)
    for db_prefix, local_prefix in PATH_MAP.items():
        db_prefix, local_prefix = str(db_prefix), str(local_prefix)
        if not local_prefix:
            continue
        root_local = local_prefix.rstrip("/\\")
        if s == local_prefix or (
            s.startswith(root_local)
            and len(s) > len(root_local)
            and s[len(root_local)] in "/\\"
        ):
            rest = s[len(root_local):]
            sep = "\\" if "\\" in db_prefix else "/"
            rest = rest.replace("\\", sep).replace("/", sep)
            return db_prefix.rstrip("/\\") + rest
    return s


def _like_escape(s: str) -> str:
    """转义 SQL LIKE 通配符，需配合 ESCAPE '\\' 子句使用。"""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

# ==================================================

# exiftool 是否可用：缺失时只降级 GPS/部分 EXIF，不中断流程
EXIFTOOL_AVAILABLE = False

def require_exiftool() -> None:
    global EXIFTOOL_AVAILABLE
    EXIFTOOL_AVAILABLE = shutil.which("exiftool") is not None
    if not EXIFTOOL_AVAILABLE:
        print(
            "[WARN] 未找到 exiftool，将跳过 exiftool 辅助的 GPS/EXIF 读取（不影响主流程）。\n"
            "       如需更完整的 GPS 信息，请安装：\n"
            "       macOS: brew install exiftool\n"
            "       Ubuntu/Debian: sudo apt-get install -y libimage-exiftool-perl\n"
            "       Windows: choco install exiftool"
        )

def encode_image_to_b64(path: Path) -> str:
    """读取图片并（可选）缩放长边后，重新编码为 JPEG，再转 base64。

    目的：
    1) 控制输入分辨率（尤其是 200MP 这类超大图），避免推理成本/延迟暴涨；
    2) 通过重新编码，尽量规避某些 JPEG 在 libvips 侧解码报错（如 marker 前多余字节）。
    """
    data = _read_bytes_with_nas_retry(path)

    # 尽量用 PIL 容错打开，然后统一转成干净的 JPEG bytes
    try:
        img = Image.open(io.BytesIO(data))
        # 处理 EXIF 旋转
        try:
            img = ImageOps.exif_transpose(img)  # type: ignore
        except Exception:
            pass

        # 统一色彩模式：JPEG 需要 RGB
        if img.mode in ("RGBA", "LA"):
            # 有透明通道时，用白底合成
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")

        # 可选缩放
        try:
            w, h = img.size
            long_edge = max(w, h)
            if VLM_MAX_LONG_EDGE and long_edge > VLM_MAX_LONG_EDGE:
                scale = float(VLM_MAX_LONG_EDGE) / float(long_edge)
                new_w = max(1, int(round(w * scale)))
                new_h = max(1, int(round(h * scale)))
                img = img.resize((new_w, new_h), resample=Image.LANCZOS)
        except Exception:
            pass

        out = io.BytesIO()
        # quality 92 在观感和体积之间比较平衡；optimize 可能更慢但通常能降体积
        img.save(out, format="JPEG", quality=92, optimize=True)
        clean_bytes = out.getvalue()
        return base64.b64encode(clean_bytes).decode("utf-8")

    except Exception as e:
        # 不再盲目返回原始损坏字节，而是抛出异常，让主循环捕获并跳过该损坏或不支持的图片
        raise RuntimeError(f"图片损坏或格式不支持（Pillow 无法解码）：{e}")


# 生成一句话文案
def _thinking_control() -> dict:
    """OpenAI 兼容请求中「思考链控制」的字段口径（按端点自动区分服务商）。

    智谱 glm-5.3-flash 是“始终思考”模型：不认 think 字段（思考链照跑并吃掉
    max_tokens 预算），也不支持 disabled，只支持 effort 档位，用官方
    thinking.enabled + effort=low 控制开销；LM Studio 等本地兼容层沿用
    think=false（未知字段会被忽略，无副作用）。
    """
    if "open.bigmodel.cn" in API_ENDPOINT:
        return {"thinking": {"type": "enabled", "effort": "low"}}
    return {"think": False}


def generate_side_caption(image_path: Path, img_b64: str | None = None) -> str | None:
    system_prompt = (
        "你是一位为「电子相框」撰写旁白短句的中文文案助手。\n"
        "你的目标不是描述画面，而是为画面补上一点“画外之意”。\n\n"

        "创作原则：\n"
        "1. 避免使用以下词语：世界、梦、时光、岁月、温柔、治愈、刚刚好、悄悄、慢慢 等（但不是绝对禁止）。\n"
        "2. 严禁使用如下句式：……里……着整个世界；……里……着整个夏天；……得像……（简单的比喻）; ……比……还……； ……得比……更……。\n"
        "3. 只基于图片中能确定的信息进行联想，不要虚构时间、人物关系、事件背景。\n"
        "4. 文案应自然、有趣，带一点幽默或者诗意，但请避免煽情、鸡汤。\n"
        "5. 不要复述画面内容本身，而是写“看完画面后，心里多出来的一句话”。\n"
        "6. 可以偏向以下风格之一：\n"
        "   - 日常中的微妙情绪\n"
        "   - 轻微自嘲或冷幽默\n"
        "   - 对时间、记忆、瞬间的含蓄感受\n"
        "   - 看似平淡但有余味的一句判断\n"
        "7. 避免小学生作文式的、套路式的模板化表达\n"

        "格式要求：\n"
        "1. 只输出一句中文短句，不要换行，不要引号，不要任何解释。\n"
        "2. 建议长度 8～24 个汉字，最多不超过 30 个汉字。\n"
        "3. 不要出现“这张照片”“这一刻”“那天”等指代照片本身的词。\n"
    )
    user_prompt = "请基于这张照片，生成一句符合规则的中文文案。"
    try:
        if img_b64 is None:
            img_b64 = encode_image_to_b64(image_path)
    except Exception:
        return None

    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"

    is_ollama_native = API_PROTOCOL == "ollama"

    if is_ollama_native:
        payload = {
            "model": MODEL_NAME,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt, "images": [img_b64]},
            ],
            "stream": False,
            "options": {"temperature": 0.7, "top_p": 0.9},
            "think": False,
        }
    else:
        payload = {
            "model": MODEL_NAME,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
                        },
                    ],
                },
            ],
            "temperature": 0.7,
            # 始终思考型模型需要预留思考 token（effort=low 约 100~300），64 会被思考链吃光
            "max_tokens": 1024,
            "top_p": 0.9,
            "stream": False,
            **_thinking_control(),
        }

    # 始终思考型模型偶发把 max_tokens 全花在思考链上（content 为空、finish=length），
    # 对空结果做有限重试，其余失败仍按原口径静默返回 None（文案是可选产物）
    caption = None
    for _attempt in range(3):
        try:
            resp = requests.post(API_ENDPOINT, headers=headers, json=payload, timeout=min(120, TIMEOUT))
        except Exception:
            return None

        if not resp.ok:
            return None

        try:
            data = resp.json()
            if is_ollama_native:
                content = data["message"]["content"]
            else:
                content = data["choices"][0]["message"]["content"]
        except Exception:
            return None

        if not isinstance(content, str):
            content = str(content)

        caption = content.strip().strip("“”\"'")
        if caption:
            break
    return caption or None


def list_images(limit: int | None = None) -> list[Path]:
    exts = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
    files = []
    print("[INFO] 正在递归扫描图片目录，请稍候……")
    scanned = 0

    dirs_to_scan = [IMAGE_DIR]

    while dirs_to_scan:
        curr_dir = dirs_to_scan.pop()

        # 扫描不可达目录时立即中止，禁止把不完整快照用于数据库清理。
        def _before_attempt():
            if not _is_mount_ok() and NAS_MOUNT_URL:
                _try_remount_nas()

        entries = scandir_with_retries(
            curr_dir, attempts=NAS_RETRY_TIMES, sleep_seconds=NAS_RETRY_SLEEP_SEC,
            before_attempt=_before_attempt,
        )

        if not entries:
            continue

        for entry in entries:
            scanned += 1
            if scanned % 500 == 0:
                print(f"[SCAN] 已扫描文件数：{scanned} …")

            try:
                entry_path = Path(entry.path)
                path_str = str(entry_path)

                if _is_path_excluded(path_str):
                    continue

                if entry.is_dir(follow_symlinks=False):
                    dirs_to_scan.append(entry_path)
                elif entry.is_file(follow_symlinks=False):
                    if entry_path.suffix.lower() in exts:
                        if is_screenshot(entry_path):
                            continue
                        files.append(entry_path)
            except OSError as e:
                raise ScanIncompleteError(f"读取文件状态失败，禁止使用不完整快照：{entry.path}") from e

    print(f"[INFO] 扫描完成，共发现 {len(files)} 张图片（文件总数 {scanned}）。")
    if limit is not None:
        files = files[:limit]
    return files

# 排除 Screenshot 图片
def is_screenshot(path: Path) -> bool:
    s = str(path)
    return "screenshot" in s.lower()


def _is_path_excluded(path_str: str) -> bool:
    r"""全路径排除关键字匹配（不区分大小写）。

    分隔符归一：\ 与 / 等价，让一套 EXCLUDE_KEYWORDS 同时适配
    Windows（\\10.168.1.111\Photos\陈丽娟的照片）与 Linux（/mnt/nas/Photos/陈丽娟的照片）。
    """
    norm = path_str.replace("\\", "/").lower()
    return any(kw.replace("\\", "/").lower() in norm for kw in EXCLUDE_KEYWORDS)


def filter_unscored(conn: sqlite3.Connection, paths: list[Path]) -> list[Path]:
    if not paths:
        return []

    cur = conn.cursor()
    already: set[str] = set()
    # SQLite 的 IN (...) 参数数量有上限（约 32766），分批查询
    CHUNK = 2000
    # 每个文件同时核对本机路径与 PATH_MAP 映射后的数据库规范路径，
    # 兼容「Windows 建库 + Linux 续跑」与「Linux 原生建库」两种口径
    candidates: list[tuple[Path, set[str]]] = []
    all_keys: list[str] = []
    seen: set[str] = set()
    for p in paths:
        keys = {str(p), to_db_form(p)}
        candidates.append((p, keys))
        for k in keys:
            if k not in seen:
                seen.add(k)
                all_keys.append(k)

    for i in range(0, len(all_keys), CHUNK):
        batch = all_keys[i : i + CHUNK]
        placeholders = ",".join("?" for _ in batch)
        rows = cur.execute(
            f"SELECT path FROM photo_scores WHERE path IN ({placeholders})",
            batch,
        ).fetchall()
        already.update(row[0] for row in rows)
    return [p for p, keys in candidates if not (keys & already)]


def _convert_gps_to_deg(value):
    try:
        d, m, s = value
        return float(d[0]) / float(d[1]) + float(m[0]) / float(m[1]) / 60.0 + float(s[0]) / float(s[1]) / 3600.0
    except Exception:
        return None


def read_gps_with_exiftool(path: Path):
    if not EXIFTOOL_AVAILABLE:
        return None
    try:
        result = subprocess.run(
            ["exiftool", "-n", "-json", str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
    except FileNotFoundError:
        # 没装 exiftool，则直接跳过
        return None
    except subprocess.CalledProcessError:
        return None

    try:
        data = json.loads(result.stdout)[0]
    except Exception:
        return None

    lat = data.get("GPSLatitude")
    lon = data.get("GPSLongitude")
    alt = data.get("GPSAltitude")
    # 同时过滤 None 和空字符串，防止 float('') 抛出 ValueError
    if not lat or not lon:
        return None
    try:
        return {
            "lat": float(lat),
            "lon": float(lon),
            "alt": float(alt) if alt not in (None, "") else None,
        }
    except (ValueError, TypeError):
        return None


def read_exif(path: Path) -> dict:
    info: dict = {}
    try:
        img = Image.open(path)
        try:
            width, height = img.size
            info["width"] = int(width)
            info["height"] = int(height)
            if width > height:
                info["orientation"] = "landscape"
            elif height > width:
                info["orientation"] = "portrait"
            else:
                info["orientation"] = "square"
        except Exception:
            pass
        exif_raw = img._getexif() or {}
    except Exception:
        # 如果打不开图片，尝试在 path.name 中至少提取一下时间
        parsed_dt = parse_datetime_from_filename(path.name)
        if parsed_dt:
            info["datetime"] = parsed_dt
        return info

    exif = {}
    for tag_id, value in exif_raw.items():
        tag = ExifTags.TAGS.get(tag_id, tag_id)
        exif[tag] = value

    # 基本字段
    info["datetime"] = exif.get("DateTimeOriginal") or exif.get("DateTime")
    if not info.get("datetime"):
        parsed_dt = parse_datetime_from_filename(path.name)
        if parsed_dt:
            info["datetime"] = parsed_dt

    info["make"] = exif.get("Make")
    info["model"] = exif.get("Model")
    info["iso"] = exif.get("ISOSpeedRatings") or exif.get("PhotographicSensitivity")
    info["exposure_time"] = exif.get("ExposureTime")
    info["f_number"] = exif.get("FNumber")
    info["focal_length"] = exif.get("FocalLength")

    gps_info = exif.get("GPSInfo")
    lat = lon = None
    if isinstance(gps_info, dict):
        # GPSInfo 的 key 可能是数字，需要映射
        gps_tags = {}
        for k, v in gps_info.items():
            name = ExifTags.GPSTAGS.get(k, k)
            gps_tags[name] = v

        lat_ref = gps_tags.get("GPSLatitudeRef")
        lat_raw = gps_tags.get("GPSLatitude")
        lon_ref = gps_tags.get("GPSLongitudeRef")
        lon_raw = gps_tags.get("GPSLongitude")

        if lat_raw and lat_ref:
            lat = _convert_gps_to_deg(lat_raw)
            if lat is not None and lat_ref in ["S", "s"]:
                lat = -lat
        if lon_raw and lon_ref:
            lon = _convert_gps_to_deg(lon_raw)
            if lon is not None and lon_ref in ["W", "w"]:
                lon = -lon

    info["gps_lat"] = lat
    info["gps_lon"] = lon

    if info.get("gps_lat") is None or info.get("gps_lon") is None:
        gps = read_gps_with_exiftool(path)
        if gps is not None:
            info["gps_lat"] = gps["lat"]
            info["gps_lon"] = gps["lon"]
            if gps.get("alt") is not None:
                info["gps_alt"] = gps["alt"]

    return info


def in_home(lat: float | None, lon: float | None) -> bool:
    """判断是否在“本地/常驻地”范围内。"""
    if lat is None or lon is None:
        return False
    try:
        d = haversine_km(float(lat), float(lon), float(HOME_LAT), float(HOME_LON))
        return d <= float(HOME_RADIUS_KM)
    except Exception:
        return False


def format_eta(seconds: float) -> str:
    if seconds <= 0:
        return "00:00:00"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


import csv
import math
from typing import Dict, List, Tuple, Optional

CityRecord = Tuple[float, float, str, str]  # (lat, lon, name_zh, name_en)

_CITY_CACHE_CITIES: List[CityRecord] | None = None
_CITY_CACHE_GRID: Dict[Tuple[int, int], List[int]] | None = None

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return r * c

def grid_key(lat: float, lon: float) -> Tuple[int, int]:
    gx = int(math.floor(lat / CITY_GRID_DEG))
    gy = int(math.floor(lon / CITY_GRID_DEG))
    return gx, gy

def load_world_cities(csv_path: Path) -> Tuple[List[CityRecord], Dict[Tuple[int, int], List[int]]]:
    if not csv_path.exists():
        raise SystemExit(f"[FATAL] 找不到城市索引文件: {csv_path}")

    cities: List[CityRecord] = []
    grid_index: Dict[Tuple[int, int], List[int]] = {}

    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                lat = float((row.get("lat") or "").strip())
                lon = float((row.get("lon") or "").strip())
            except Exception:
                continue
            name_en = (row.get("name_en") or "").strip()
            name_zh = (row.get("name_zh") or "").strip()
            cities.append((lat, lon, name_zh, name_en))

    for idx, (lat, lon, name_zh, name_en) in enumerate(cities):
        key = grid_key(lat, lon)
        grid_index.setdefault(key, []).append(idx)

    print(f"[INFO] 已加载中文城市库: {csv_path}")
    return cities, grid_index

def find_nearest_city(
    lat: float,
    lon: float,
    cities: List[CityRecord],
    grid_index: Dict[Tuple[int, int], List[int]],
    max_km: float = 80.0,
) -> str:
    if not cities:
        return ""

    gx, gy = grid_key(lat, lon)

    def collect_candidates(radius: int) -> List[int]:
        cand: List[int] = []
        for dx in range(-radius, radius + 1):
            for dy in range(-radius, radius + 1):
                bucket = grid_index.get((gx + dx, gy + dy))
                if bucket:
                    cand.extend(bucket)
        return cand

    candidates = collect_candidates(radius=1)
    if not candidates:
        candidates = collect_candidates(radius=2)
    if not candidates:
        return ""

    best_idx: Optional[int] = None
    best_dist = float("inf")

    for idx in candidates:
        city_lat, city_lon, name_zh, name_en = cities[idx]
        d = haversine_km(lat, lon, city_lat, city_lon)
        if d < best_dist:
            best_dist = d
            best_idx = idx

    if best_idx is None or best_dist > max_km:
        return ""

    _, _, name_zh, name_en = cities[best_idx]
    return name_zh or name_en or ""

def get_city_resolver():
    global _CITY_CACHE_CITIES, _CITY_CACHE_GRID
    if _CITY_CACHE_CITIES is None or _CITY_CACHE_GRID is None:
        _CITY_CACHE_CITIES, _CITY_CACHE_GRID = load_world_cities(WORLD_CITIES_CSV)

    def resolve(lat: float | None, lon: float | None) -> str:
        if lat is None or lon is None:
            return ""
        return find_nearest_city(lat, lon, _CITY_CACHE_CITIES, _CITY_CACHE_GRID, max_km=CITY_MAX_DISTANCE_KM)

    return resolve


def call_vlm(image_path: Path, img_b64: str | None = None) -> dict:
    try:
        # 支持外部传入已编码的 b64（主循环里与侧栏文案共用一次编码，省一遍读盘+缩放+重编码）
        if img_b64 is None:
            img_b64 = encode_image_to_b64(image_path)
    except Exception as e:
        raise RuntimeError(f"读取图片失败：{e}")

    exif_info = read_exif(image_path)
    exif_json = json.dumps(exif_info, ensure_ascii=False, default=str)

    system_prompt = (
        "你是一个“个人相册照片评估助手”，擅长理解真实照片的内容，并从回忆价值和美观角度打分。\n"
        "你会收到一张照片（以 base64 形式提供），你的任务是：\n"
        "1）用中文详细描述照片内容（80~200 字），\n"
        "2）判断照片的大致类型：人物/孩子/猫咪/家庭/旅行/风景/美食/宠物/日常/文档/杂物/其他，一张照片可以有不止一个类型。\n"
        "3）给出 0~100 的“值得回忆度” memory_score（精确到一位小数），\n"
        "4）给出 0~100 的“美观程度” beauty_score（精确到一位小数），\n"
        "5）用简短中文 reason 解释原因（不超过 40 字）。\n\n"

        "【值得回忆度（memory_score）评分方法】\n"
        "请先按照值得回忆的程度，先确定照片的'得分区间'，再进行精调：\n"
        "如何判定值得回忆度（memory_score）的得分区间：\n"
        "- 垃圾/随手拍/无意义记录：40.0 分以下（常见为 0~25；若还能勉强辨认但无故事，也不要超过 39.9）。\n"
        "- 稍微有点可回忆价值：以 65.0 分为中心（大多落在 58.1~70.3）。\n"
        "- 不错的回忆价值：以 75 分为中心（大多落在 68.7~82.4）。\n"
        "- 特别精彩、强烈值得珍藏：以 85 分为中心（大多落在 79.1~95.9；\n"
        "如何继续精调memory_score得分（若同时符合几条加分项，加分可叠加）：\n"
        "- 人物与关系：画面中含有面积较大的人脸，有人物互动，或属于合影 → 大幅提高评分；\n"
        "- 事件性：生日/聚会/仪式/舞台/明显事件 → 少许提高评分；\n"
        "- 稀缺性与不可复现：明显“这一刻很难再来一次” → 大幅提高评分；\n"
        "- 情绪强度：笑、哭、惊喜、拥抱、互动、氛围强 → 少许提高评分；\n"
        "- 信息密度：画面能讲清楚发生了什么 → 微微提高评分；\n"
        "- 优美风景：画面中含有壮丽的自然风光，或精美、有秩序感的构图 → 少许提高评分；\n"
        "- 旅行意义：异地、地标、旅途情景 → 少许提高评分。\n\n"
        "- 画质：画面不清晰、模糊、有残影、虚焦 → 微微降低评分。\n\n"

        "【重点照片的处理】\n"
        "如果画面中含有：孩子/猫咪/宠物题材，这些主题更容易产生高回忆价值，请直接以75分为中心，并大幅提高评分”。\n"

        "【明显低价值图片的处理】\n"
        "对以下低价值图片，必须将 memory_score 压低到 0~25（最多不超过 39）。\n"
        "- 裸露、低俗、色情或违反公序良俗的图片。\n\n"
        "- 账单、收据、广告、随手拍的杂物、测试图片、屏幕截图等。\n\n"
        
        "【美观分（beauty_score）评分方法】\n"
        "美观分只评价视觉：构图、光线、清晰度、色彩、主体突出。\n"
        "不要被“孩子/猫/旅行”主题绑架美观分：主题不等于好看。\n"

        "请严格只输出 JSON，格式如下：\n"
        "{\n"
        "  \"caption\": \"……\",\n"
        "  \"type\": \"人物/家庭/旅行/…… 可以带多个type\",\n"
        "  \"memory_score\": 0.0-100.0 的数字, 精确到 1 位小数\n"
        "  \"beauty_score\": 0.0-100.0 的数字, 精确到 1 位小数\n"
        "  \"reason\": \"不超过 60 字的中文理由\"\n"
        "}\n"
        "不要输出任何多余文字，不要加注释。"
    )

    user_text = (
        "下面是照片的内容，请结合图像本身完成上述任务。\n"
    )

    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"

    # 按解析出的协议区分 Ollama native (/api/chat) 与 OpenAI 兼容格式
    is_ollama_native = API_PROTOCOL == "ollama"

    if is_ollama_native:
        # Ollama native：图片放 images，content 为纯文本字符串
        payload = {
            "model": MODEL_NAME,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text, "images": [img_b64]},
            ],
            "stream": False,
            "think": False,
            "options": {"temperature": 0.2},
        }
    else:
        # OpenAI 兼容：content 为对象数组，图片用 image_url
        payload = {
            "model": MODEL_NAME,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_text},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{img_b64}"
                            },
                        },
                    ],
                },
            ],
            "temperature": 0.2,
            "stream": False,
            **_thinking_control(),
        }

    resp = requests.post(API_ENDPOINT, headers=headers, json=payload, timeout=TIMEOUT)
    if not resp.ok:
        print("HTTP:", resp.status_code)
        print(resp.text)
        raise RuntimeError(f"LM Studio 请求失败: HTTP {resp.status_code}")

    data = resp.json()
    try:
        if is_ollama_native:
            content = data["message"]["content"].strip()
        else:
            content = data["choices"][0]["message"]["content"].strip()
    except Exception:
        print("[DEBUG] 返回内容：", data)
        raise RuntimeError("解析失败：无法从响应中读取 content")

    # content 应该是 JSON 字符串
    try:
        # 使用增强逻辑提取并解析 JSON
        extracted = extract_json_from_text(content)
        obj = json.loads(extracted)
    except Exception:
        print("[DEBUG] 非 JSON 输出：", content)
        raise RuntimeError("解析失败：模型未按 JSON 输出")

    return obj, exif_info




def extract_json_from_text(text: str) -> str:
    """从可能包含 Markdown 代码块或解释性文字的字符串中提取 JSON 子串。"""
    import re
    text = text.strip()
    
    # 移除 <think> 标签内容（如果存在）
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()
    
    # 查找 Markdown 代码块
    if '```json' in text:
        try:
            content = text.split('```json')[1].split('```')[0].strip()
            return content
        except Exception:
            pass
    elif '```' in text:
        try:
            content = text.split('```')[1].split('```')[0].strip()
            return content
        except Exception:
            pass

    # 兜底：查找第一个 '{' 和最后一个 '}'
    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end != -1 and end > start:
        return text[start:end+1]
    return text

def main():
    filelist_path = ROOT_DIR / "filelist.txt"

    # 任何扫描、filelist 写入或照片评分前先确认数据库已显式迁移。
    # 只读预检不会创建数据库；实际写连接还会再次校验以避免竞态。
    try:
        require_current_schema(DB_PATH)
    except MigrationError as exc:
        raise SystemExit(f"InkTime analyzer database startup blocked: {exc}") from None

    print(f"[INFO] AI 端点：{API_ENDPOINT}（协议 {API_PROTOCOL}，模型 {MODEL_NAME}）")
    print("[INFO] 正在扫描图片目录……")
    imgs = list_images()
    filelist_path.write_text("\n".join(str(p) for p in imgs), encoding="utf-8")
    print(f"[INFO] 已更新文件列表 filelist.txt，共 {len(imgs)} 个文件。")
    if not imgs:
        raise SystemExit(f"目录下没有图片文件: {IMAGE_DIR}")

    imgs = [p for p in imgs if not is_screenshot(p)]
    if not imgs:
        raise SystemExit("[INFO] 所有图片都被 Screenshot 过滤规则排除了，没有可处理的图片。")

    conn = open_current_database(DB_PATH)
    city_resolver = get_city_resolver()

    # =======================
    # 同步删除：NAS/磁盘上已不存在的文件，也从数据库里删除
    # 只处理当前 IMAGE_DIR 前缀下的记录，避免误删其它历史路径。
    # =======================
    # 清理/统计口径：本机 IMAGE_DIR 前缀 + PATH_MAP 反向映射出的数据库规范前缀。
    # Windows 时代生成的库存的是 UNC 路径，Linux 上必须按映射前缀才能对上。
    cleanup_prefixes = list(dict.fromkeys([str(IMAGE_DIR), to_db_form(IMAGE_DIR)]))

    try:
        # 用临时表避免 IN (...) 过长导致的 SQLite 参数上限问题
        conn.execute("DROP TABLE IF EXISTS _temp_existing_paths")
        conn.execute("CREATE TEMP TABLE _temp_existing_paths (path TEXT PRIMARY KEY)")

        # 批量插入当前扫描到的文件列表（本机路径与数据库规范路径都登记，
        # 两种口径的历史记录都能被 NOT EXISTS 命中，避免被误删）
        CHUNK = 2000
        total_files = len(imgs)
        inserted = 0
        for i in range(0, total_files, CHUNK):
            chunk = imgs[i : i + CHUNK]
            conn.executemany(
                "INSERT OR IGNORE INTO _temp_existing_paths(path) VALUES (?)",
                [(k,) for p in chunk for k in {str(p), to_db_form(p)}],
            )
            inserted += len(chunk)
            if inserted % 10000 == 0:
                print(f"[CLEAN] 已写入存在文件清单：{inserted}/{total_files} …")

        def _count_under_prefixes(cur: sqlite3.Cursor) -> int:
            return sum(
                cur.execute(
                    "SELECT COUNT(*) FROM photo_scores WHERE path LIKE ? ESCAPE '\\'",
                    (_like_escape(prefix) + "%",),
                ).fetchone()[0]
                for prefix in cleanup_prefixes
            )

        # 默认仅报告差异：即使是完整扫描，也不能将“缺失”直接等同于真实删除。
        # 历史版本在此执行 DELETE，NAS 瞬断或路径重映射可能导致分析记录丢失。
        missing = count_missing_paths(conn, cleanup_prefixes, _like_escape)
        conn.commit()
        if missing:
            print(f"[CLEAN][DRY-RUN] 发现 {missing} 条未在当前扫描中出现的评分记录；已保留。")
            print("[CLEAN][DRY-RUN] 自动硬删除已禁用；请单独审核缺失清单并在备份后清理。")
        else:
            print("[CLEAN] 数据库与扫描文件列表无缺失差异。")

    except Exception as e:
        # 清理统计失败也不能触发删除；回滚未提交的临时操作。
        conn.rollback()
        print(f"[WARN] 扫描差异统计失败（数据库记录保持不变）：{e}")

    cur_test = conn.cursor()
    # 只统计当前 IMAGE_DIR 下的已分析照片，避免数据库里其它路径/历史残留影响进度计算
    counted = 0
    for prefix in cleanup_prefixes:
        counted += cur_test.execute(
            "SELECT COUNT(*) FROM photo_scores WHERE path LIKE ? ESCAPE '\\'",
            (_like_escape(prefix) + "%",),
        ).fetchone()[0]
    print(f"[INFO] 数据库中已有 {counted} 张已分析照片（仅统计当前目录）。")

    target_paths = filter_unscored(conn, imgs)
    if not target_paths:
        print("[INFO] 所有图片都已经在 photo_scores 中有记录。")
        conn.close()
        return

    if BATCH_LIMIT is not None:
        target_paths = target_paths[:BATCH_LIMIT]

    # 进度条口径：以“本次启动时的快照”为准。
    # total = 已分析(当前目录) + 本次待处理（filter_unscored 产生的目标集合）
    already_done = counted
    total = already_done + len(target_paths)
    print(f"[INFO] 本次准备处理 {len(target_paths)} 张图片（快照总数 {total}，已分析 {already_done}）。")

    cur = conn.cursor()
    start_time = time.time()
    since_commit = 0
    COMMIT_EVERY = 50  # 批量提交：避免 3 万张逐行 fsync

    for idx, path in enumerate(target_paths, start=1):
        t_photo_start = time.perf_counter()
        sep = "=" * 60
        print("\n" + sep)
        print(f"[{idx}/{len(target_paths)}] 处理: {path}")
        # 同一张图只编码一次，VLM 评分与侧栏文案两个请求复用
        try:
            img_b64 = encode_image_to_b64(path)
        except Exception as e:
            print(f"[WARN] 图片读取/编码失败，跳过: {e}")
            continue
        try:
            result, exif_info = call_vlm(path, img_b64=img_b64)
        except Exception as e:
            print(f"[WARN] 调用模型失败: {e}")
            continue
        t_after_vlm = time.perf_counter()
        vlm_cost = t_after_vlm - t_photo_start

        caption = str(result.get("caption", "")).strip()
        ptype = normalize_type_to_list(result.get("type", ""))
        try:
            memory_score = float(result.get("memory_score", 0.0))
        except Exception:
            memory_score = 0.0
        try:
            beauty_score = float(result.get("beauty_score", 0.0))
        except Exception:
            beauty_score = 0.0
        reason = str(result.get("reason", "")).strip()

        side_caption = generate_side_caption(path, img_b64=img_b64)
        t_after_side = time.perf_counter()
        side_cost = t_after_side - t_after_vlm

        width = exif_info.get("width")
        height = exif_info.get("height")
        orientation = exif_info.get("orientation")

        exif_datetime = exif_info.get("datetime")
        exif_make = exif_info.get("make")
        exif_model = exif_info.get("model")

        def _to_int(v):
            try:
                if v is None:
                    return None
                return int(v)
            except Exception:
                return None

        def _to_float(v):
            try:
                if v is None:
                    return None
                return float(v)
            except Exception:
                return None

        exif_iso = _to_int(exif_info.get("iso"))
        exif_exposure_time = _to_float(exif_info.get("exposure_time"))
        exif_f_number = _to_float(exif_info.get("f_number"))
        exif_focal_length = _to_float(exif_info.get("focal_length"))
        exif_gps_lat = _to_float(exif_info.get("gps_lat"))
        exif_gps_lon = _to_float(exif_info.get("gps_lon"))
        exif_gps_alt = _to_float(exif_info.get("gps_alt"))

        if exif_gps_lat is not None and exif_gps_lon is not None:
            exif_city = city_resolver(exif_gps_lat, exif_gps_lon)
        else:
            exif_city = ""

        # 如果有 GPS 信息且不在本地范围内，略微提高回忆分（最多 +5，且不超过 100 分）
        lat = exif_info.get("gps_lat")
        lon = exif_info.get("gps_lon")
        if lat is not None and lon is not None and not in_home(lat, lon):
            memory_score = min(memory_score + 5.0, 100.0)

        exif_json = json.dumps(exif_info, ensure_ascii=False, default=str)

        print(f"  类型    ：{ptype}")
        print(f"  回忆分  ：{memory_score:.1f}")
        print(f"  美观分  ：{beauty_score:.1f}")
        if side_caption:
            print(f"  一句话文案：{side_caption}")
        else:
            print("  一句话文案：(无)")
        print(f"  画面描述：{caption}")
        print(f"  理由    ：{reason}")

        # 调用 YOLO 检测主体
        subjects = detect_subjects(path)
        subjects_json_str = json.dumps(subjects, ensure_ascii=False) if subjects else ""

        # 主键统一存数据库规范路径（PATH_MAP 反向映射），跨平台口径一致
        db_path = to_db_form(path)
        cur.execute(
            """
            INSERT OR REPLACE INTO photo_scores
            (path, caption, type, memory_score, beauty_score, reason,
             width, height, orientation, used_at,
             exif_json, raw_json,
             exif_datetime, exif_make, exif_model,
             exif_iso, exif_exposure_time, exif_f_number, exif_focal_length,
             exif_gps_lat, exif_gps_lon, exif_gps_alt, side_caption, exif_city, subjects_json)
            VALUES (?, ?, ?, ?, ?, ?,
                    ?, ?, ?, COALESCE((SELECT used_at FROM photo_scores WHERE path = ?), NULL),
                    ?, ?,
                    ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?)
            """,
            (
                db_path,
                caption,
                ptype,
                memory_score,
                beauty_score,
                reason,
                width,
                height,
                orientation,
                db_path,
                exif_json,
                json.dumps(result, ensure_ascii=False),
                exif_datetime,
                exif_make,
                exif_model,
                exif_iso,
                exif_exposure_time,
                exif_f_number,
                exif_focal_length,
                exif_gps_lat,
                exif_gps_lon,
                exif_gps_alt,
                side_caption,
                exif_city,
                subjects_json_str,
            ),
        )
        since_commit += 1
        if since_commit >= COMMIT_EVERY:
            conn.commit()
            since_commit = 0
        t_photo_end = time.perf_counter()
        total_cost = t_photo_end - t_photo_start
        # pretty timing summary

        # 进度条与预估时间（以本次启动时的快照为准，不受运行中新增照片影响）
        processed_now = already_done + idx

        denom = total if total > 0 else 1
        progress = processed_now / denom
        # 夹紧，确保不会超过 100%
        if progress < 0:
            progress = 0.0
        if progress > 1:
            progress = 1.0

        bar_width = 30
        filled = int(bar_width * progress)
        bar = "#" * filled + "-" * (bar_width - filled)

        elapsed = time.time() - start_time
        avg_per = elapsed / idx if idx > 0 else 0
        remaining = max(total - processed_now, 0)
        eta = format_eta(remaining * avg_per) if avg_per > 0 else "00:00:00"

        print(f"[进度] {bar} {progress*100:5.1f}%  {processed_now}/{total}  本张耗时 {total_cost:4.1f}s  预计剩余 {eta} ")

    if since_commit:
        conn.commit()
    conn.close()
    print("\n[完成] 本批次处理完成。")


if __name__ == "__main__":
    require_exiftool()
    main()