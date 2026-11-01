import requests
import base64
import json

API_URL = "http://127.0.0.1:11434/api/chat"
MODEL_NAME = "qwen3.5:9b"

# 用系统里真实存在的图片路径
image_path = r"V:\Timeline\Timeline@兮兮\P1000895.JPG"

with open(image_path, "rb") as f:
    img_b64 = base64.b64encode(f.read()).decode("utf-8")

system_prompt = (
    "你是一位为「电子相框」撰写旁白短句的中文文案助手。\n"
    "你的目标不是描述画面，而是为画面补上一点“画外之意”。\n\n"
    "创作原则：\n"
    "1. 避免使用以下词语：世界、梦、时光、岁月、温柔、治愈、刚刚好、悄悄、慢慢 等（但不是绝对禁止）。\n"
    "2. 严禁使用如下句式：……里……着整个世界；……里……着整个夏天；……得像……（简单的比喻）; ……比……还……； ……得比……更……。\n"
    "3. 只基于图片中能确定的信息进行联想，不要虚构时间、人物关系、事件背景。\n"
    "4. 文案应自然、有趣，带一点幽默或者诗意，但请避免煽情、鸡汤。\n"
    "5. 不要复述画面内容本身，而是写“看完画面后，心里多出来的一句话”。\n"
    "6. 可以偏向以下风格之一：\n"
    "   - 日常中的微妙情绪\n"
    "   - 轻微自嘲或冷幽默\n"
    "   - 对时间、记忆、瞬间的含蓄感受\n"
    "   - 看似平淡但有余味的一句判断\n"
    "7. 避免小学生作文式的、套路式的模板化表达\n"
    "格式要求：\n"
    "1. 只输出一句中文短句，不要换行，不要引号，不要任何解释。\n"
    "2. 建议长度 8～24 个汉字，最多不超过 30 个汉字。\n"
    "3. 不要出现“这张照片”“这一刻”“那天”等指代照片本身的词。\n"
)
user_prompt = "请基于这张照片，生成一句符合规则的中文文案。"

payload = {
    "model": MODEL_NAME,
    "messages": [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt, "images": [img_b64]},
    ],
    "stream": False,
    "options": {"temperature": 0.7, "top_p": 0.9}
}

try:
    print("开始调用...")
    resp = requests.post(API_URL, json=payload, timeout=120)
    print(f"HTTP Code: {resp.status_code}")
    data = resp.json()
    print("===== JSON DUMP =====")
    print(json.dumps(data, indent=2, ensure_ascii=False))
    print("===== CONTENT =====")
    content = data.get("message", {}).get("content", "")
    print(repr(content))
except Exception as e:
    print(f"Error: {e}")
