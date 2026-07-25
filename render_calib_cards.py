#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
标定卡生成脚本（一次性部署产物，不属于每日 render_daily_photo.py 流程）。

用途：
- 为相框旋转功能（ENABLE_FRAME_ROTATION）生成两张"朝向无歧义"的标定卡。
- 标定卡在 ESP32 调试页通过 GET /static/inktime/<key>/calib_p.bin 与 calib_l.bin 下发，
  供安装者现场标定舵机的竖屏/横屏绝对角度、转速与画面方向反转开关。
- 每张卡的设计保证用户一眼即可判断画面是否正放、颠倒、镜像：
    * 中央一个大向上箭头（↑）—— 颠倒时箭头朝下，立刻能看出来。
    * 顶部 "TOP / 上" —— 标识物理上边。
    * 左右两侧 "L / 左" "R / 右" —— 镜像时左右互换，立刻能看出来。
    * 一个不对称的红色三角（只画在一个角上）—— 进一步排除镜像/翻转歧义。
- 只用墨水屏四色调色板（黑/白/红/黄），最终经 apply_four_color_dither + image_to_palette_bin 转成 .bin。

运行：
    python3 render_calib_cards.py
产物：
    output/calib_p.bin  （竖屏 480×800，384000 字节）
    output/calib_l.bin  （横屏 800×480，384000 字节）
输出目录解析与 render_daily_photo.py / server.py 完全一致（读取 cfg.BIN_OUTPUT_DIR）。

注意：部署一次即可，不会随每日 render_daily_photo.py 重跑；想更新标定卡图案时手动再跑。
"""

from __future__ import annotations

from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import config as cfg

# 复用渲染管线的 dither 与打包逻辑（同项目内 import）。
from render_daily_photo import (
    apply_four_color_dither,
    image_to_palette_bin,
)

ROOT_DIR = Path(__file__).resolve().parent

# 与 render_daily_photo.py / server.py 完全一致的输出目录解析。
BIN_OUTPUT_DIR = Path(str(getattr(cfg, "BIN_OUTPUT_DIR", "./output") or "./output")).expanduser()
if not BIN_OUTPUT_DIR.is_absolute():
    BIN_OUTPUT_DIR = (ROOT_DIR / BIN_OUTPUT_DIR).resolve()
BIN_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# 字体：优先项目自定义字体，失败回退系统字体，最后回退 PIL 默认字体。
FONT_PATH = Path(str(getattr(cfg, "FONT_PATH", "") or "")).expanduser()
if str(FONT_PATH) and not FONT_PATH.is_absolute():
    FONT_PATH = (ROOT_DIR / FONT_PATH).resolve()


def _get_font(size: int) -> ImageFont.FreeTypeFont:
    if str(FONT_PATH) and FONT_PATH.exists():
        try:
            return ImageFont.truetype(str(FONT_PATH), size)
        except Exception:
            pass
    import os
    if os.name == "nt":
        for fallback in [r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simsun.ttc", r"C:\Windows\Fonts\simhei.ttf"]:
            if os.path.exists(fallback):
                try:
                    return ImageFont.truetype(fallback, size)
                except Exception:
                    continue
    return ImageFont.load_default()


def draw_calib_card(width: int, height: int) -> Image.Image:
    """
    在 width×height 画布上绘制朝向无歧义的标定卡（RGB，使用近四色原色）。
    颜色直接取自调色板，dither 之后会严格收敛到 4 色。
    """
    BLACK = (0, 0, 0)
    WHITE = (255, 255, 255)
    RED = (200, 0, 0)
    YELLOW = (220, 180, 0)

    canvas = Image.new("RGB", (width, height), WHITE)
    draw = ImageDraw.Draw(canvas)

    # 1. 细边框，标识画面边界（便于判断画面是否完整、是否被裁切）。
    margin = max(6, min(width, height) // 60)
    draw.rectangle((margin, margin, width - margin, height - margin), outline=BLACK, width=4)

    cx, cy = width // 2, height // 2

    # 2. 中央大向上箭头（↑）：颠倒时一眼可见。
    #    箭杆为粗矩形，箭头为三角；整体高度约占画布短边的 45%。
    short_side = min(width, height)
    arrow_total_h = int(short_side * 0.45)
    head_h = arrow_total_h // 3
    shaft_w = max(8, short_side // 30)
    head_half_w = head_h  # 等腰直角三角的半底

    shaft_bottom = cy + arrow_total_h // 2
    shaft_top = shaft_bottom - (arrow_total_h - head_h)
    # 箭杆
    draw.rectangle(
        (cx - shaft_w // 2, shaft_top, cx + shaft_w // 2, shaft_bottom),
        fill=BLACK,
    )
    # 箭头三角（顶点朝上）
    head_tip_y = shaft_top - head_h + shaft_w  # 让箭头尖端略高于箭杆顶
    head_base_y = shaft_top + shaft_w // 2
    draw.polygon(
        [
            (cx, head_tip_y),                 # 顶点
            (cx - head_half_w, head_base_y),  # 左下
            (cx + head_half_w, head_base_y),  # 右下
        ],
        fill=BLACK,
    )

    # 3. 顶部 "TOP / 上" 标识（靠近上边，居中）。
    font_label = _get_font(max(20, short_side // 12))
    font_corner = _get_font(max(16, short_side // 18))

    top_text = "TOP / 上"
    tw = draw.textlength(top_text, font=font_label)
    draw.text(
        ((width - tw) // 2, margin + 8),
        top_text,
        font=font_label,
        fill=BLACK,
    )

    # 4. 左右标识 "L / 左" "R / 右"（镜像时左右互换，可检测镜像）。
    left_text = "L / 左"
    right_text = "R / 右"
    # 竖排放在左右两侧中点附近
    left_w = draw.textlength(left_text, font=font_corner)
    right_w = draw.textlength(right_text, font=font_corner)
    side_y = cy - font_corner.size // 2
    draw.text((margin + 12, side_y), left_text, font=font_corner, fill=BLACK)
    draw.text((width - margin - 12 - right_w, side_y), right_text, font=font_corner, fill=BLACK)

    # 5. 不对称标记：左上角画一个红色实心三角，右下角画一个黄色方块。
    #    正常显示：红三角在左上、黄方块在右下；
    #    180° 颠倒：红三角在右下、黄方块在左上（且箭头朝下）；
    #    水平镜像：红三角在右上、黄方块在左下、且 "L/R" 互换。
    tri_size = max(28, short_side // 8)
    tri_x0 = margin + 16
    tri_y0 = margin + 16
    draw.polygon(
        [
            (tri_x0, tri_y0),                          # 左上角顶点
            (tri_x0 + tri_size, tri_y0),               # 右上
            (tri_x0, tri_y0 + tri_size),               # 左下
        ],
        fill=RED,
    )

    sq_size = max(24, short_side // 10)
    draw.rectangle(
        (width - margin - 16 - sq_size, height - margin - 16 - sq_size,
         width - margin - 16, height - margin - 16),
        fill=YELLOW,
    )

    # 6. 底部说明文字：标明这张是哪一朝向的卡，以及画布尺寸。
    font_foot = _get_font(max(14, short_side // 22))
    orient_str = "LANDSCAPE 800x480" if width > height else "PORTRAIT 480x800"
    fw = draw.textlength(orient_str, font=font_foot)
    draw.text(
        ((width - fw) // 2, height - margin - font_foot.size - 10),
        orient_str,
        font=font_foot,
        fill=BLACK,
    )

    return canvas


def render_one(orientation: str) -> Path:
    """渲染单张标定卡：orientation 为 "portrait" 或 "landscape"。返回 .bin 路径。"""
    if orientation == "landscape":
        width = int(getattr(cfg, "LANDSCAPE_CANVAS_WIDTH", 800))
        height = int(getattr(cfg, "LANDSCAPE_CANVAS_HEIGHT", 480))
        out_name = "calib_l.bin"
    else:
        # 直接复用 render_daily_photo 的竖屏常量，保证与渲染管线一致。
        from render_daily_photo import CANVAS_WIDTH, CANVAS_HEIGHT
        width, height = CANVAS_WIDTH, CANVAS_HEIGHT
        out_name = "calib_p.bin"

    card = draw_calib_card(width, height)
    dithered = apply_four_color_dither(card)
    bin_data = image_to_palette_bin(dithered, orientation=orientation)

    out_path = BIN_OUTPUT_DIR / out_name
    out_path.write_bytes(bin_data)
    return out_path


def main():
    print("[calib] 开始生成标定卡 ...")

    p_path = render_one("portrait")
    l_path = render_one("landscape")

    print(f"[OK] 竖屏标定卡: {p_path} （{p_path.stat().st_size} 字节, 期望 {480*800}）")
    print(f"[OK] 横屏标定卡: {l_path} （{l_path.stat().st_size} 字节, 期望 {800*480}）")
    print("[calib] 完成。两张 .bin 通过 server.py 的 calib_p.bin / calib_l.bin 端点下发。")


if __name__ == "__main__":
    main()
