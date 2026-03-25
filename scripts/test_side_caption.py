import requests
import base64
import json
from io import BytesIO
from PIL import Image

API_URL = "http://127.0.0.1:11434/api/chat"
MODEL_NAME = "qwen3.5:9b"

img = Image.new('RGB', (224, 224), color='white')
buffered = BytesIO()
img.save(buffered, format="JPEG")
img_b64 = base64.b64encode(buffered.getvalue()).decode("utf-8")

payload = {
    "model": MODEL_NAME,
    "messages": [
        {"role": "system", "content": "You are a test."},
        {"role": "user", "content": "hello", "images": [img_b64]},
    ],
    "stream": False,
    "options": {"temperature": 0.7, "top_p": 0.9}
}

try:
    resp = requests.post(API_URL, json=payload, timeout=30)
    print(f"HTTP: {resp.status_code}")
    print("CONTENT:", resp.json().get("message", {}).get("content"))
except Exception as e:
    print(f"Error: {e}")
