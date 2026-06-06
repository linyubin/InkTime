import sys
from pathlib import Path
import sqlite3
import json
from PIL import Image, ImageDraw, ImageFont, ImageOps

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT_DIR))

import config as cfg
from render_daily_photo import compute_crop_window

def main():
    db_path = Path(str(getattr(cfg, "DB_PATH", "./photos.db"))).expanduser()
    if not db_path.is_absolute():
        db_path = (ROOT_DIR / db_path).resolve()

    if not db_path.exists():
        print(f"数据库不存在: {db_path}")
        return

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # 取出带有 "人物" 或 "宠物" 标签的 5 张照片用于对比测试
    cur.execute('''
        SELECT path, subjects_json 
        FROM photo_scores 
        WHERE type LIKE '%人物%' OR type LIKE '%宠物%' OR type LIKE '%猫咪%' OR type LIKE '%孩子%'
        LIMIT 5
    ''')
    rows = cur.fetchall()
    
    if not rows:
        print("没有在数据库中找到包含'人物'、'宠物'等标签的照片用于测试对比！")
        conn.close()
        return

    output_dir = ROOT_DIR / "output"
    output_dir.mkdir(exist_ok=True)
    
    img_area_w = 480
    img_area_h = 800 - 120 # 680

    from analyze_photos import detect_subjects

    for idx, (path_str, subjects_json) in enumerate(rows, start=1):
        p = Path(path_str)
        if not p.exists():
            print(f"文件不存在: {path_str}")
            continue

        # 如果数据库里的 subjects_json 为空或 []，在测试脚本里现场跑一下 YOLO
        if not subjects_json or subjects_json == '[]':
            print(f"[{idx}/5] 正在现场跑 YOLO 分析: {path_str}")
            subjects = detect_subjects(p)
            subjects_json = json.dumps(subjects, ensure_ascii=False) if subjects else "[]"
        else:
            print(f"[{idx}/5] 使用数据库中已有的 subjects_json: {path_str}")

        try:
            img = Image.open(p)
            img = ImageOps.exif_transpose(img)
            img = img.convert("RGB")
        except Exception as e:
            print(f"打开图片失败 {path_str}: {e}")
            continue
            
        img_w, img_h = img.size
        if img_w == 0 or img_h == 0:
            continue
            
        scale = max(img_area_w / img_w, img_area_h / img_h)
        draw_w = int(img_w * scale)
        draw_h = int(img_h * scale)
        
        img_resized = img.resize((draw_w, draw_h), Image.LANCZOS)
        
        # 老版本：居中裁剪
        center_left = max(0, (draw_w - img_area_w) // 2)
        center_top = max(0, (draw_h - img_area_h) // 2)
        img_center_crop = img_resized.crop((center_left, center_top, center_left + img_area_w, center_top + img_area_h))
        
        # 新版本：直接使用 render_image 渲染完整成品图
        # 构造假的 meta 字典传给 render_image
        meta = {
            "path": path_str,
            "subjects_json": subjects_json,
            "side": "测试文案",
            "date": "2024-01-01",
            "city": "测试城市"
        }
        
        try:
            from render_daily_photo import render_image
            img_smart_rendered = render_image(meta)
        except Exception as e:
            print(f"渲染新版本失败: {e}")
            continue

        # 将两张图拼接到一起 (960 x 800)
        # img_center_crop 是 480x680，补上白底使其变成 480x800
        old_full = Image.new("RGB", (480, 800), (255, 255, 255))
        old_full.paste(img_center_crop, (0, 0))
        
        combined = Image.new("RGB", (img_area_w * 2, 800))
        combined.paste(old_full, (0, 0))
        combined.paste(img_smart_rendered, (img_area_w, 0))
        
        # 可选：绘制中线以作区分
        draw = ImageDraw.Draw(combined)
        draw.line([(img_area_w, 0), (img_area_w, 800)], fill="red", width=2)
        
        # 写入标题以区分
        try:
            font = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", 24)
        except Exception:
            font = ImageFont.load_default()
            
        draw.text((10, 10), "Before: Center Crop", fill="red", font=font)
        draw.text((img_area_w + 10, 10), "After: Smart Crop (Render)", fill="red", font=font)
        
        out_path = output_dir / f"crop_compare_{idx}.jpg"
        combined.save(out_path, quality=90)
        print(f"已生成对比图 {idx}: {out_path.relative_to(ROOT_DIR)} (左:老版居中裁切, 右:新版完整渲染)")
        
if __name__ == "__main__":
    main()
