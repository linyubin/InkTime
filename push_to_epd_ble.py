#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
push_to_epd_ble.py - 将 InkTime 今日照片通过 BLE 推送到 EPD-nRF5 墨水屏

用法：
    # 自动扫描设备并推送第 0 张照片（日常使用）
    python push_to_epd_ble.py

    # 指定设备 MAC（跳过扫描，更快更稳定）
    python push_to_epd_ble.py --device AA:BB:CC:DD:EE:FF

    # 推送指定序号的照片
    python push_to_epd_ble.py --idx 1 --device AA:BB:CC:DD:EE:FF

配置项（在 config.py 中设置）：
    EPD_DEVICE_MAC      = ""       # 目标设备 MAC，空则自动扫描
    EPD_BLE_CHUNK_SIZE  = 238      # BLE 传输块大小，连接不稳时可调小至 128
"""

import asyncio
import argparse
import sys
import logging
from pathlib import Path

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    print("错误：请先安装 bleak：pip install bleak")
    sys.exit(1)

import config as cfg

# ── 日志配置 ──────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("push_epd")

# ── EPD-nRF5 BLE 协议常量 ─────────────────────────────
EPD_SERVICE_UUID = "62750001-d828-918d-fb46-b6c11c675aec"
EPD_CHAR_UUID    = "62750002-d828-918d-fb46-b6c11c675aec"

CMD_INIT      = 0x01
CMD_WRITE_IMG = 0x30
CMD_REFRESH   = 0x05

# ── 路径与配置 ────────────────────────────────────────
ROOT_DIR = Path(__file__).resolve().parent

BIN_OUTPUT_DIR = Path(str(getattr(cfg, "BIN_OUTPUT_DIR", "output/inktime"))).expanduser()
if not BIN_OUTPUT_DIR.is_absolute():
    BIN_OUTPUT_DIR = (ROOT_DIR / BIN_OUTPUT_DIR).resolve()

# 目标设备 MAC（填写后跳过扫描，推荐）
EPD_DEVICE_MAC = str(getattr(cfg, "EPD_DEVICE_MAC", "") or "").strip()

# BLE 分块大小（字节）：每次 write 的 data 部分（不含命令头 2 字节）
# 默认 238 = MTU 244 - 2(ATT header) - 4(L2CAP) = 238
# 传输不稳时调小至 128 或 64
CHUNK_SIZE = int(getattr(cfg, "EPD_BLE_CHUNK_SIZE", 238) or 238)

# ── 格式转换 ──────────────────────────────────────────
# InkTime .bin：1字节/像素，0=黑 1=白 2=红 3=黄，画布尺寸 480×800（竖屏行序）
# EPD-nRF5 fourColor packed：2bit/像素，4像素→1字节，MSB优先，硬件行宽=800
# 颜色值映射：黑0→0x00, 白1→0x01, 红2→0x03, 黄3→0x02（注意红黄互换！）
_COLOR_MAP = {0: 0x00, 1: 0x01, 2: 0x03, 3: 0x02}

# 原始画布尺寸（与 render_daily_photo.py 的 CANVAS_WIDTH/HEIGHT 对应）
_SRC_W = 480  # 竖屏宽
_SRC_H = 800  # 竖屏高

# 旋转方向配置（从 config.py 读取）
# 硬件原生横屏 800×480；竖屏放置时需旋转图像使行序匹配
_ROTATE = str(getattr(cfg, "EPD_ROTATE", "CW90") or "CW90").upper().strip()


def rotate_raw(raw: bytes, rotate: str) -> tuple:
    """
    将 _SRC_W × _SRC_H 的原始像素数组旋转后，返回 (rotated_bytes, out_w, out_h)。

    旋转方向说明（以屏幕正面朝向用户为准）：
      CW90   - 顺时针旋转 90°，结果尺寸 800×480
      CCW90  - 逆时针旋转 90°，结果尺寸 800×480
      ROT180 - 旋转 180°，      结果尺寸 480×800（行列互换后字节数不变）
      NONE   - 不旋转，         结果尺寸 480×800（直接透传，调试用）
    """
    sw, sh = _SRC_W, _SRC_H

    if rotate == "NONE":
        return raw, sw, sh

    if rotate == "ROT180":
        # 逐像素翻转：最后一个像素变第一个
        rotated = bytearray(len(raw))
        total = sw * sh
        for i in range(total):
            rotated[i] = raw[total - 1 - i]
        return bytes(rotated), sw, sh

    # CW90 / CCW90：输出尺寸互换为 sh×sw（即 800×480）
    dw, dh = sh, sw  # 旋转后：宽=800, 高=480
    rotated = bytearray(dw * dh)

    if rotate == "CW90":
        # 顺时针 90°：原 (ox, oy) → 新 (sh-1-oy, ox)
        for oy in range(sh):
            row_base = oy * sw
            for ox in range(sw):
                nx = sh - 1 - oy
                ny = ox
                rotated[ny * dw + nx] = raw[row_base + ox]
    elif rotate == "CCW90":
        # 逆时针 90°：原 (ox, oy) → 新 (oy, sw-1-ox)
        for oy in range(sh):
            row_base = oy * sw
            for ox in range(sw):
                nx = oy
                ny = sw - 1 - ox
                rotated[ny * dw + nx] = raw[row_base + ox]
    else:
        log.warning(f"未知旋转方向 '{rotate}'，将使用 CW90。")
        return rotate_raw(raw, "CW90")

    return bytes(rotated), dw, dh


def inktime_to_fourcolor_packed(raw: bytes) -> bytes:
    """将 InkTime .bin 旋转并转为 EPD-nRF5 fourColor packed 格式（2bit/像素）。"""
    rotated, out_w, out_h = rotate_raw(raw, _ROTATE)
    log.info(f"图像旋转模式：{_ROTATE}，输出尺寸 {out_w}×{out_h}")

    n   = len(rotated)
    out = bytearray((n + 3) // 4)
    for i, v in enumerate(rotated):
        epd_val  = _COLOR_MAP.get(v, 0x01)          # 未知值默认白
        out[i // 4] |= (epd_val << (6 - (i % 4) * 2))
    return bytes(out)


# ── BLE 扫描 ──────────────────────────────────────────
async def find_epd_device(target_mac: str = "", timeout: float = 10.0) -> str:
    """扫描附近 EPD-nRF5 设备，返回 MAC 地址。"""
    if target_mac:
        log.info(f"使用指定设备 MAC：{target_mac}")
        return target_mac

    log.info(f"自动扫描 EPD-nRF5 设备（最多 {timeout:.0f}s）...")
    devices = await BleakScanner.discover(timeout=timeout)

    found = []
    for d in devices:
        name = d.name or ""
        if name.startswith("NRF_EPD"):
            log.info(f"  ✅ 发现目标设备：{name} ({d.address})")
            found.append(d.address)
        else:
            log.debug(f"  忽略设备：{name} ({d.address})")

    if not found:
        log.warning("未发现 NRF_EPD_* 设备，将列出所有扫描到的设备：")
        for d in devices:
            log.warning(f"  {d.name or '(无名)'} ({d.address})")
        return ""

    if len(found) > 1:
        log.warning(f"发现多台 EPD 设备，将使用第一台：{found[0]}")

    return found[0]


# ── BLE 推送 ──────────────────────────────────────────
async def push_image(device_addr: str, packed: bytes) -> None:
    """连接 nRF5 设备并将 packed 图像数据分块传输，完成后发送刷新指令。"""
    log.info(f"正在连接设备：{device_addr} ...")

    async with BleakClient(device_addr, timeout=20.0) as client:
        if not client.is_connected:
            raise RuntimeError("BLE 连接失败，请检查设备状态")

        log.info("已连接！开始初始化屏幕...")

        # Step 1：INIT
        await client.write_gatt_char(EPD_CHAR_UUID, bytes([CMD_INIT]), response=True)
        await asyncio.sleep(0.2)

        # Step 2：分块传输图像数据
        total   = len(packed)
        sent    = 0
        chunk_n = 0

        # 每隔 3 包等待一次 ACK（模拟 main.js interleavedCount=3 逻辑，
        # 避免连续无 ACK 写入导致固件缓冲区溢出）
        ACK_INTERVAL = 3
        last_printed_pct = -1

        while sent < total:
            chunk   = packed[sent: sent + CHUNK_SIZE]
            is_last = (sent + len(chunk) >= total)
            # 首包：flag=0x0F（fourColor + first），续包：flag=0xF0
            flag    = 0x0F if sent == 0 else 0xF0
            payload = bytes([CMD_WRITE_IMG, flag]) + chunk

            with_resp = is_last or (chunk_n % ACK_INTERVAL == ACK_INTERVAL - 1)
            await client.write_gatt_char(EPD_CHAR_UUID, payload, response=with_resp)

            sent    += len(chunk)
            chunk_n += 1
            pct      = int(sent / total * 100)
            
            if pct // 20 > last_printed_pct // 20 or is_last:
                print(f"\r  [传输] {pct:3d}% ({sent:>6}/{total} 字节)", end="", flush=True)
                last_printed_pct = pct

        print()  # 换行

        # Step 3：REFRESH
        log.info("数据传输完成，正在发送屏幕刷新指令...")
        await client.write_gatt_char(EPD_CHAR_UUID, bytes([CMD_REFRESH]), response=True)
        log.info("✅ 刷新指令已发送！墨水屏将在 10~30 秒内完成刷新。")


# ── 主流程 ────────────────────────────────────────────
async def main(photo_idx: int, device_mac: str) -> None:
    # 1. 加载 .bin 文件
    bin_path = BIN_OUTPUT_DIR / f"photo_{photo_idx}.bin"
    if not bin_path.exists():
        log.error(f"找不到文件：{bin_path}")
        log.error("请先运行 render_daily_photo.py 生成今日照片。")
        sys.exit(1)

    raw = bin_path.read_bytes()
    log.info(f"加载照片 #{photo_idx}：{bin_path}（{len(raw):,} 字节）")

    # 尺寸验证
    expected = _SRC_W * _SRC_H
    if len(raw) != expected:
        log.error(f"文件尺寸异常：期望 {expected:,} 字节（{_SRC_W}×{_SRC_H}），实际 {len(raw):,} 字节")
        sys.exit(1)

    # 2. 格式转换
    packed = inktime_to_fourcolor_packed(raw)
    log.info(f"格式转换：{len(raw):,}B（1字节/像素）→ {len(packed):,}B（2bit/像素 packed）")

    # 3. 查找设备
    mac = await find_epd_device(device_mac or EPD_DEVICE_MAC)
    if not mac:
        log.error("未找到 EPD-nRF5 设备，推送取消。")
        log.error("排查建议：")
        log.error("  1. 确认设备已开机，LED 正在闪烁（广播状态）")
        log.error("  2. 确认 NAS 蓝牙适配器正常：hciconfig -a")
        log.error("  3. 在 config.py 中设置 EPD_DEVICE_MAC 以跳过扫描")
        sys.exit(1)

    # 4. 推送
    await push_image(mac, packed)

    # 5. 标记为已展示（防重复）
    path_file = BIN_OUTPUT_DIR / f"photo_{photo_idx}.path.txt"
    if path_file.exists():
        try:
            target_path = path_file.read_text(encoding="utf-8").strip()
            if target_path:
                import render_daily_photo as rdp
                rdp.mark_photo_used(target_path)
        except Exception as e:
            log.warning(f"无法标记照片已展示: {e}")


def cli() -> None:
    parser = argparse.ArgumentParser(
        description="将 InkTime 今日照片推送到 EPD-nRF5 蓝牙墨水屏",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--idx", type=int, default=0,
        help="照片序号（默认 0，对应 photo_0.bin）",
    )
    parser.add_argument(
        "--device", type=str, default="",
        metavar="MAC",
        help="BLE 设备 MAC 地址（留空则自动扫描 NRF_EPD_* 设备）",
    )
    args = parser.parse_args()
    asyncio.run(main(args.idx, args.device))


if __name__ == "__main__":
    cli()
