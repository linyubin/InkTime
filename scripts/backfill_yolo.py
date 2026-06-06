#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
from pathlib import Path
import sqlite3
import json
import time

# 添加上级目录到 sys.path，以便能导入 config 和 analyze_photos
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT_DIR))

import config as cfg
from analyze_photos import detect_subjects

def main():
    db_path = Path(str(getattr(cfg, "DB_PATH", "./photos.db") or "./photos.db")).expanduser()
    if not db_path.is_absolute():
        db_path = (ROOT_DIR / db_path).resolve()

    if not db_path.exists():
        print(f"数据库不存在: {db_path}")
        return

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN subjects_json TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    # 查找尚未进行 YOLO 检测的数据（subjects_json 为 NULL）
    cur.execute("SELECT path FROM photo_scores WHERE subjects_json IS NULL")
    rows = cur.fetchall()

    if not rows:
        print("所有已有数据都已经包含了主体检测数据，无需增量补充。")
        conn.close()
        return

    print(f"找到 {len(rows)} 张历史照片需要进行增量 YOLO 检测。")
    
    start_time = time.time()
    
    try:
        for idx, (path_str,) in enumerate(rows, start=1):
            p = Path(path_str)
            
            t0 = time.perf_counter()
            print(f"[{idx}/{len(rows)}] 补充检测: {path_str} ", end="", flush=True)
            
            if not p.exists():
                print(" -> [跳过] 文件不存在")
                continue
                
            subjects = detect_subjects(p)
            subjects_json_str = json.dumps(subjects, ensure_ascii=False) if subjects else ""
            
            cur.execute(
                "UPDATE photo_scores SET subjects_json = ? WHERE path = ?",
                (subjects_json_str, path_str)
            )
            
            t1 = time.perf_counter()
            print(f"-> 发现 {len(subjects)} 个主体，耗时 {t1-t0:.2f}s")
            
            # 每 50 条提交一次以防止意外中断丢失进度
            if idx % 50 == 0:
                conn.commit()
                
    except KeyboardInterrupt:
        print("\n[中止] 用户手动中断了操作。")
    finally:
        conn.commit()
        conn.close()
        
    elapsed = time.time() - start_time
    print(f"\n增量补充完成！总耗时: {elapsed:.2f}s")

if __name__ == "__main__":
    main()
