import sqlite3

def count_photos():
    db_path = "photos.db"
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # 获取所有行，手动解析 type
    cursor.execute("SELECT type, memory_score, beauty_score FROM photo_scores")
    rows = cursor.fetchall()
    
    count = 0
    for row_type, memory_score, beauty_score in rows:
        if row_type and '旅行' in row_type:
            if memory_score > 80 and beauty_score > 50:
                count += 1
    
    conn.close()
    return count

if __name__ == "__main__":
    result = count_photos()
    print(f"RESULT: {result}")
