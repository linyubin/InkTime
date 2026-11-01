#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
验证 InkTime .bin → EPD-nRF5 fourColor packed 格式转换逻辑。

InkTime .bin 格式：1 字节/像素
  0 = 黑, 1 = 白, 2 = 红, 3 = 黄

EPD-nRF5 fourColor packed 格式：2 bit/像素，4 像素打包成 1 字节（MSB 优先）
  0x00 = 黑, 0x01 = 白, 0x03 = 红, 0x02 = 黄
  像素0 → bit[7:6], 像素1 → bit[5:4], 像素2 → bit[3:2], 像素3 → bit[1:0]
"""

import sys
from pathlib import Path

# ── 格式转换函数 ──────────────────────────────────────
_COLOR_MAP = {0: 0x00, 1: 0x01, 2: 0x03, 3: 0x02}

def inktime_to_fourcolor_packed(raw: bytes) -> bytes:
    """
    将 InkTime .bin（1字节/像素）转换为 EPD-nRF5 fourColor packed（2bit/像素）。
    颜色映射：黑0→0x00, 白1→0x01, 红2→0x03, 黄3→0x02
    4 像素打包为 1 字节，高位在前（MSB first）。
    """
    n   = len(raw)
    out = bytearray((n + 3) // 4)
    for i, v in enumerate(raw):
        epd_val  = _COLOR_MAP.get(v, 0x01)  # 未知像素值默认为白
        out[i // 4] |= (epd_val << (6 - (i % 4) * 2))
    return bytes(out)


# ── 逆向还原（用于人工校验）────────────────────────────
_REVERSE_MAP = {0x00: 0, 0x01: 1, 0x03: 2, 0x02: 3}
_COLOR_NAME  = {0: '黑', 1: '白', 2: '红', 3: '黄'}

def fourcolor_packed_to_inktime(packed: bytes, n_pixels: int) -> bytes:
    """逆向还原 packed 格式回 1字节/像素，用于验证。"""
    out = bytearray(n_pixels)
    for i in range(n_pixels):
        shift   = 6 - (i % 4) * 2
        epd_val = (packed[i // 4] >> shift) & 0x03
        out[i]  = _REVERSE_MAP.get(epd_val, 1)
    return bytes(out)


# ── 测试用例 ──────────────────────────────────────────
def test_basic_mapping():
    """测试1：黑白红黄各1像素的颜色映射"""
    # 输入：[黑, 白, 红, 黄] = [0, 1, 2, 3]
    # EPD值：[0x00, 0x01, 0x03, 0x02]
    # 二进制：[00, 01, 11, 10] → 0b00011110 = 0x1E
    result = inktime_to_fourcolor_packed(bytes([0, 1, 2, 3]))
    assert result == bytes([0x1E]), f"FAIL: {result.hex()} != 1e"
    # 逆向验证
    restored = fourcolor_packed_to_inktime(result, 4)
    assert restored == bytes([0, 1, 2, 3]), f"逆向FAIL: {restored.hex()}"
    print(f"  ✅ 测试1通过：[黑白红黄] → 0x{result.hex().upper()}")


def test_all_white():
    """测试2：全白 4 像素 = 0x55 (01 01 01 01)"""
    result = inktime_to_fourcolor_packed(bytes([1, 1, 1, 1]))
    assert result == bytes([0x55]), f"FAIL: {result.hex()} != 55"
    print(f"  ✅ 测试2通过：[全白] → 0x{result.hex().upper()}")


def test_all_black():
    """测试3：全黑 4 像素 = 0x00 (00 00 00 00)"""
    result = inktime_to_fourcolor_packed(bytes([0, 0, 0, 0]))
    assert result == bytes([0x00]), f"FAIL: {result.hex()} != 00"
    print(f"  ✅ 测试3通过：[全黑] → 0x{result.hex().upper()}")


def test_color_order():
    """测试4：验证红/黄不互换（关键正确性测试）"""
    # 单独红色像素（4个同色）
    red_result    = inktime_to_fourcolor_packed(bytes([2, 2, 2, 2]))
    yellow_result = inktime_to_fourcolor_packed(bytes([3, 3, 3, 3]))
    # 红色 EPD=0x03 → (11 11 11 11) = 0xFF
    # 黄色 EPD=0x02 → (10 10 10 10) = 0xAA
    assert red_result    == bytes([0xFF]), f"红色FAIL: {red_result.hex()} != ff"
    assert yellow_result == bytes([0xAA]), f"黄色FAIL: {yellow_result.hex()} != aa"
    print(f"  ✅ 测试4通过：红色→0x{red_result.hex().upper()}, 黄色→0x{yellow_result.hex().upper()}")


def test_odd_pixel_count():
    """测试5：非4倍数像素数（边界处理）"""
    # 5 个像素：黑黑黑黑白 → 应有 2 个字节
    # 字节1: 00 00 00 00 → 0x00
    # 字节2: 01 00 00 00 → 0x40（白色占高2位，其余为默认0）
    raw    = bytes([0, 0, 0, 0, 1])
    result = inktime_to_fourcolor_packed(raw)
    assert len(result) == 2, f"长度FAIL: {len(result)} != 2"
    assert result[0] == 0x00, f"字节0 FAIL: {result[0]:02x}"
    assert result[1] == 0x40, f"字节1 FAIL: {result[1]:02x}"
    print(f"  ✅ 测试5通过：非4倍数像素边界处理正确")


def test_real_bin_file():
    """测试6：真实 photo_0.bin 文件尺寸与内容范围"""
    root    = Path(__file__).resolve().parent.parent
    bin_dir = root / "output" / "inktime"
    bin_path = bin_dir / "photo_0.bin"

    if not bin_path.exists():
        print(f"  ⚠️  跳过测试6：{bin_path} 不存在")
        print(f"      请先运行 render_daily_photo.py 生成今日照片")
        return

    raw    = bin_path.read_bytes()
    packed = inktime_to_fourcolor_packed(raw)

    # 尺寸验证
    assert len(raw) == 480 * 800, f"原始尺寸FAIL: {len(raw)} != {480*800}"
    assert len(packed) == 480 * 800 // 4, f"packed尺寸FAIL: {len(packed)}"

    # 内容范围验证：原始数据只含 0~3
    invalid = [v for v in raw if v not in (0, 1, 2, 3)]
    assert len(invalid) == 0, f"发现非法像素值: {set(invalid)}"

    # 统计颜色分布
    from collections import Counter
    dist = Counter(raw)
    total = len(raw)
    print(f"  ✅ 测试6通过：{len(raw)}B → {len(packed)}B")
    print(f"     颜色分布：黑{dist[0]/total*100:.1f}% 白{dist[1]/total*100:.1f}% "
          f"红{dist[2]/total*100:.1f}% 黄{dist[3]/total*100:.1f}%")


# ── 主函数 ────────────────────────────────────────────
def main():
    print("=" * 50)
    print("InkTime .bin → EPD-nRF5 fourColor 格式转换验证")
    print("=" * 50)

    tests = [
        test_basic_mapping,
        test_all_white,
        test_all_black,
        test_color_order,
        test_odd_pixel_count,
        test_real_bin_file,
    ]

    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"  ❌ {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  💥 {t.__name__} 异常: {e}")
            failed += 1

    print("=" * 50)
    if failed == 0:
        print("✅ 所有测试通过！格式转换逻辑正确，可以进行 BLE 推送。")
        sys.exit(0)
    else:
        print(f"❌ {failed} 个测试失败，请修复后再进行 BLE 推送。")
        sys.exit(1)


if __name__ == "__main__":
    main()
