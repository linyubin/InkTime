# -*- coding: utf-8 -*-
import sqlite3
import re
import datetime
import json
from pathlib import Path
from common import parse_datetime_from_filename

DB_PATH = Path("photos.db")

def main():
    if not DB_PATH.exists():
        print(f"[ERROR] 数据库文件不存在：{DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # 查询所有没有拍摄日期的记录
    cur.execute("SELECT path, exif_json FROM photo_scores WHERE exif_datetime IS NULL OR exif_datetime = ''")
    rows = cur.fetchall()

    print(f"[INFO] 数据库中没有拍摄日期的记录总数: {len(rows)}")
    
    updated_count = 0
    
    for path_str, exif_json_str in rows:
        filename = Path(path_str).name
        parsed_dt = parse_datetime_from_filename(filename)
        
        if parsed_dt:
            # 更新 exif_json
            try:
                exif_data = json.loads(exif_json_str) if exif_json_str else {}
            except Exception:
                exif_data = {}
                
            exif_data["datetime"] = parsed_dt
            new_exif_json = json.dumps(exif_data, ensure_ascii=False)
            
            # 执行更新
            cur.execute(
                "UPDATE photo_scores SET exif_datetime = ?, exif_json = ? WHERE path = ?",
                (parsed_dt, new_exif_json, path_str)
            )
            updated_count += 1
            if updated_count % 500 == 0:
                print(f"[PROGRESS] 已成功修复并写入 {updated_count} 条记录...")

    conn.commit()
    conn.close()

    print(f"[SUCCESS] 修复完成！共检测到 {len(rows)} 条无日期的图片记录，成功从文件名中提取并更新了 {updated_count} 条记录。")

if __name__ == "__main__":
    main()
