import sqlite3
import json

conn = sqlite3.connect('photos.db')
c = conn.cursor()

target = r"\\10.168.1.111\Photos\Timeline\Timeline@游玩&随拍\201607@婚纱照\林余斌&陈丽娟\照片\9X0A1998  罗兰水晶小-1.jpg"
row = c.execute("SELECT exif_json, side_caption, memory_score FROM photo_scores WHERE path = ?", (target,)).fetchone()

if row:
    exif_json, side_caption, memory_score = row
    print(f"HAS_EXIF={bool(exif_json)}")
    if exif_json:
        try:
            d = json.loads(exif_json)
            date_keys = [k for k in d if 'date' in k.lower() or 'time' in k.lower()]
            print(f"DATE_KEYS={date_keys}")
            for k in date_keys:
                print(f"  {k}={d[k]}")
        except:
            print("EXIF_PARSE_ERROR")
    print(f"HAS_SIDE={bool(side_caption)}")
    print(f"SCORE={memory_score}")
else:
    print("ROW_NOT_FOUND")

conn.close()
