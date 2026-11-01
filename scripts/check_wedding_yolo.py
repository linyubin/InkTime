import sqlite3
import json

conn = sqlite3.connect('photos.db')
c = conn.cursor()
c.execute("SELECT path, subjects_json FROM photo_scores WHERE date LIKE '2016-06-05%' OR exif_datetime LIKE '2016:06:05%' OR exif_datetime LIKE '2016-06-05%'")
rows = c.fetchall()

print(f"Found {len(rows)} photos from 2016-06-05.")
for path, subjects_json in rows:
    # Filter for photos that might be the wedding one
    # The wedding photo has a caption "他站在那儿..."
    # Let's just print all of them that have subjects
    print(f"\nPath: {path}")
    if subjects_json:
        try:
            subs = json.loads(subjects_json)
            for s in subs:
                print(f"  - {s['label']}: conf={s['conf']}, bbox={s['bbox']}")
        except:
            print("  - Invalid JSON")
    else:
        print("  - No subjects")
