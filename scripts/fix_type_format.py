"""
修复脚本：
1. 修改 analyze_photos.py 中的 ptype 标准化逻辑
2. 迁移 photos.db 中现有的 type 字段为 JSON list 格式
"""
import re
import json
import sqlite3

# ===================================================
# 第一步：修改 analyze_photos.py 中 ptype 的处理逻辑
# ===================================================

def normalize_type_to_json_list(type_val):
    """将各种格式的 type 字段标准化为 JSON list 字符串"""
    if type_val is None or str(type_val).strip() == '':
        return '[]'
    
    s = str(type_val).strip()
    
    # 如果已经是合法的 JSON list，直接返回
    try:
        parsed = json.loads(s)
        if isinstance(parsed, list):
            # 确保每个元素都是字符串
            result = [str(t).strip() for t in parsed if str(t).strip()]
            return json.dumps(result, ensure_ascii=False)
    except Exception:
        pass
    
    # 处理 Python list 格式：['人物', '旅行', '风景']
    if s.startswith('[') and s.endswith(']'):
        try:
            # 使用 eval 但限制为安全的字面量
            import ast
            parsed = ast.literal_eval(s)
            if isinstance(parsed, list):
                result = [str(t).strip() for t in parsed if str(t).strip()]
                return json.dumps(result, ensure_ascii=False)
        except Exception:
            # 如果 eval 失败，手动去掉括号后解析
            inner = s[1:-1]
            parts = re.split(r'[,/]\s*', inner)
            result = [p.strip().strip("'\"\u4eba") for p in parts if p.strip().strip("'\" ")]
            result = [p for p in result if p]
            return json.dumps(result, ensure_ascii=False)
    
    # 处理斜杠分隔格式：人物/旅行/风景
    if '/' in s:
        parts = [p.strip() for p in s.split('/') if p.strip()]
        return json.dumps(parts, ensure_ascii=False)
    
    # 处理逗号分隔格式：人物, 旅行, 风景
    if ',' in s or '，' in s:
        parts = re.split(r'[,，]\s*', s)
        parts = [p.strip().strip("'\" ") for p in parts if p.strip().strip("'\" ")]
        return json.dumps(parts, ensure_ascii=False)
    
    # 单个值
    return json.dumps([s], ensure_ascii=False)

# ===================================================
# 第二步：修改 analyze_photos.py 的源码
# ===================================================
print("正在修改 analyze_photos.py ...")
path = 'e:/InkTime/analyze_photos.py'
with open(path, 'r', encoding='utf-8', errors='replace') as f:
    content = f.read()

OLD_PTYPE = '        ptype = str(result.get("type", "")).strip()'
NEW_PTYPE = '''        # 将 type 字段标准化为 JSON list 格式，如 ["人物", "旅行"]
        raw_type = result.get("type", "")
        ptype = normalize_type_to_list(raw_type)'''

if OLD_PTYPE in content:
    content = content.replace(OLD_PTYPE, NEW_PTYPE)
    print("  [OK] 已更新 ptype 赋值逻辑")
else:
    print("  [SKIP] 未找到 ptype 赋值语句（可能已修改）")

# 注入 normalize_type_to_list 函数（插在 call_vlm 之前，约 def main 之前的位置）
HELPER_FUNC = '''
def normalize_type_to_list(type_val) -> str:
    """将各种格式的 type 字段标准化为 JSON list 字符串，如 [\"人物\", \"旅行\"]。"""
    import ast as _ast
    if type_val is None or str(type_val).strip() == "":
        return "[]"
    s = str(type_val).strip()
    # 尝试解析为合法 JSON list
    try:
        parsed = json.loads(s)
        if isinstance(parsed, list):
            result = [str(t).strip() for t in parsed if str(t).strip()]
            return json.dumps(result, ensure_ascii=False)
    except Exception:
        pass
    # 处理 Python list 字面量
    if s.startswith("[") and s.endswith("]"):
        try:
            parsed = _ast.literal_eval(s)
            if isinstance(parsed, list):
                result = [str(t).strip() for t in parsed if str(t).strip()]
                return json.dumps(result, ensure_ascii=False)
        except Exception:
            pass
    # 处理斜杠分隔
    if "/" in s:
        parts = [p.strip() for p in s.split("/") if p.strip()]
        return json.dumps(parts, ensure_ascii=False)
    # 处理中英文逗号分隔
    if "," in s or "，" in s:
        parts = re.split(r"[,，]\\s*", s)
        parts = [p.strip().strip("\\'\\" ") for p in parts if p.strip().strip("\\'\\" ")]
        return json.dumps(parts, ensure_ascii=False)
    # 单个值
    return json.dumps([s], ensure_ascii=False)

'''

if 'def normalize_type_to_list' not in content:
    # 插在 def main() 之前
    content = content.replace('def main():', HELPER_FUNC + 'def main():')
    print("  [OK] 已注入 normalize_type_to_list 函数")
else:
    print("  [SKIP] normalize_type_to_list 函数已存在")

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)
print("  [OK] analyze_photos.py 已保存")

# ===================================================
# 第三步：迁移 photos.db 中现有数据
# ===================================================
print("\n正在迁移 photos.db 中现有数据 ...")
conn = sqlite3.connect('e:/InkTime/photos.db')
cur = conn.cursor()

cur.execute("SELECT path, type FROM photo_scores WHERE type IS NOT NULL")
rows = cur.fetchall()
print(f"  共查询到 {len(rows)} 条记录")

updated = 0
for path_val, type_val in rows:
    normalized = normalize_type_to_json_list(type_val)
    # 只有在确实变化时才更新
    if normalized != type_val:
        cur.execute("UPDATE photo_scores SET type = ? WHERE path = ?", (normalized, path_val))
        updated += 1

conn.commit()
conn.close()
print(f"  [OK] 已更新 {updated} 条记录")

# ===================================================
# 第四步：验证（抽样检查）
# ===================================================
print("\n正在验证（抽样检查）...")
conn = sqlite3.connect('e:/InkTime/photos.db')
cur = conn.cursor()
cur.execute("SELECT type FROM photo_scores WHERE type IS NOT NULL ORDER BY RANDOM() LIMIT 10")
samples = cur.fetchall()
conn.close()

all_ok = True
for (t,) in samples:
    try:
        parsed = json.loads(t)
        if isinstance(parsed, list):
            print(f"  [PASS] {t}")
        else:
            print(f"  [FAIL] 不是 list: {t}")
            all_ok = False
    except Exception:
        print(f"  [FAIL] 无法解析为 JSON: {t}")
        all_ok = False

if all_ok:
    print("\n所有抽样记录均为合法 JSON list 格式。迁移完成！")
else:
    print("\n存在不合规记录，请检查。")
