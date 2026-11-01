import sys
from PIL import Image, ImageOps

def generate_epd_header(image_path, output_path):
    img = Image.open(image_path).convert('RGB')
    
    # 裁剪并缩放，保持比例，防止图像被拉伸变形
    img = ImageOps.fit(img, (480, 800), Image.Resampling.LANCZOS)
    
    # 创建墨水屏的4色调色板
    # GUI_Paint.h 映射：
    # 0 = WHITE0
    # 1 = YELLOW0
    # 2 = RED0
    # 3 = BLACK0
    pal_img = Image.new("P", (1, 1))
    palette = [
        255, 255, 255,  # 0: White
        255, 255, 0,    # 1: Yellow
        255, 0, 0,      # 2: Red
        0, 0, 0,        # 3: Black
    ]
    # 填充剩余的调色板
    palette.extend([0, 0, 0] * 252)
    pal_img.putpalette(palette)
    
    # 转换图像并应用抖动算法 (Floyd-Steinberg)
    img_dithered = img.quantize(palette=pal_img, dither=Image.Dither.FLOYDSTEINBERG)
    
    # 获取像素数据
    pixels = list(img_dithered.getdata())
    width, height = img_dithered.size
    
    out_data = bytearray(96000)
    
    for y in range(height):
        for x in range(width):
            # 此时 pixels 里面直接就是调色板的索引 (0, 1, 2, 3)
            best_c = pixels[y * width + x]
            
            byte_idx = (x // 4) + y * (width // 4)
            bit_shift = 6 - (x % 4) * 2
            out_data[byte_idx] |= (best_c << bit_shift)

    with open(output_path, 'w') as f:
        f.write('#ifndef _AP_BG_H_\n#define _AP_BG_H_\n')
        f.write('const unsigned char ap_bg_data[96000] = {\n')
        for i in range(0, 96000, 16):
            chunk = out_data[i:i+16]
            f.write('    ' + ', '.join(f'0x{b:02x}' for b in chunk) + ',\n')
        f.write('};\n#endif\n')

generate_epd_header('/home/andy/tech_tools/InkTime/esp32/ink-display-wifi-epd/8Z5A4170.jpg', '/home/andy/tech_tools/InkTime/esp32/ink-display-wifi-epd/ap_bg.h')
print("Image converted successfully using Pillow quantize!")
