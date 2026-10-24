#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""为存量 photo_scores 行回填人脸框（face 字段）。

背景：2026-10-24 选型端启用「覆盖区压脸硬否决」，依赖 subjects_json 的
face 字段（yolov8n-pose 派生）。新分析由 analyze_photos.detect_subjects
自动写入；本脚本给已入库但无 face 的 person 行补跑 pose，使防护对老照片生效。

只读 NAS 照片 + 更新 subjects_json 字段，不动其他列；已有 face 的行跳过。
在装有 ultralytics 的环境运行（建库机或 pip 装好 ultralytics 的 venv）；
YOLO 权重按运行目录解析，请在建库机平时跑分析的目录下启动。
用法:
  # 抽样 20 张试跑（默认）
  /path/to/python scripts/backfill_face_boxes.py --sample 20
  # 全量回填（约 1-2s/张，建议 nohup 后台）
  /path/to/python scripts/backfill_face_boxes.py --all
"""
import sys, json, sqlite3, argparse
from pathlib import Path

sys.path.insert(0, '/home/andy/inktime')
import config as cfg
from analyze_photos import detect_subjects, PATH_MAP  # 复用检测+脸框派生

DB_PATH = Path('/home/andy/inktime/photos.db')


def db_to_local(db_path: str) -> Path | None:
    """数据库规范路径（UNC/正斜杠）→ 本机可读路径（PATH_MAP 反向）。"""
    s = str(db_path)
    for db_prefix, local_prefix in PATH_MAP.items():
        db_prefix, local_prefix = str(db_prefix), str(local_prefix)
        if not local_prefix:
            continue
        if s.startswith(db_prefix):
            return Path(local_prefix + s[len(db_prefix):].replace('\\', '/'))
    return Path(s)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--sample', type=int, default=0, help='随机抽样张数')
    ap.add_argument('--all', action='store_true', help='全量回填')
    ap.add_argument('--db', default=str(DB_PATH))
    args = ap.parse_args()
    if not args.all and not args.sample:
        args.sample = 20

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT path, subjects_json FROM photo_scores "
        "WHERE subjects_json LIKE '%\"person\"%' "
        "AND subjects_json NOT LIKE '%\"face\"%'").fetchall()
    print(f"待回填 person 行: {len(rows)}")
    if args.sample:
        import random
        rows = random.sample(rows, min(args.sample, len(rows)))
        print(f"抽样: {len(rows)}")

    ok = miss = nofile = 0
    for i, r in enumerate(rows, 1):
        local = db_to_local(r['path'])
        if not local or not local.exists():
            nofile += 1
            continue
        subs = detect_subjects(local)  # 全量重跑（检测框+脸框），口径与主分析一致
        new_json = json.dumps(subs, ensure_ascii=False) if subs else ""
        if not subs:
            # 重检测意外为空时保留旧数据（宁可无脸，不丢检测框）
            miss += 1
            print(f"[{i}/{len(rows)}] ! {local.name}: 重检测为空，保留原 subjects_json")
            continue
        with con:
            con.execute("UPDATE photo_scores SET subjects_json=? WHERE path=?",
                        (new_json, r['path']))
        if any(s.get('face') for s in subs):
            ok += 1
            faces = [s['face'] for s in subs if s.get('face')]
            print(f"[{i}/{len(rows)}] ✓ {local.name}: face={faces}")
        else:
            miss += 1
            print(f"[{i}/{len(rows)}] − {local.name}: 无可见脸（保留检测框）")
    print(f"\n完成: 有脸={ok} 无脸={miss} 文件缺失={nofile}")


if __name__ == '__main__':
    main()
