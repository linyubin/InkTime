import os
import sys
import base64
import json
import requests
from io import BytesIO
from PIL import Image

# 构造一个 224x224 纯白图片用于测试 (10x10有概率因尺寸过小导致模型崩溃)
img = Image.new('RGB', (224, 224), color='white')
buffered = BytesIO()
img.save(buffered, format="JPEG")
img_str = base64.b64encode(buffered.getvalue()).decode("utf-8")

# Ollama 原生接口地址和 OpenAI 兼容地址
api_url_openai = "http://127.0.0.1:11434/v1/chat/completions"
api_url_native = "http://127.0.0.1:11434/api/chat"

# 使用您提到的模型
model_name = "qwen3.5:9b"

print(f"========================================")
print(f"正在测试 Ollama 视觉模型调用...")
print(f"模型名称: {model_name}")
print(f"========================================\n")

# 测试1：原生 Ollama API 格式
payload_native = {
    "model": model_name,
    "messages": [
        {
            "role": "user",
            "content": "请描述这张图片看到了什么？",
            "images": [img_str]
        }
    ],
    "stream": False,
    "options": {"temperature": 0.1}
}

print("【测试 1】：使用 Ollama 原生 /api/chat 格式请求")
try:
    resp1 = requests.post(api_url_native, json=payload_native, timeout=30)
    print(f"[HTTP 状态码]: {resp1.status_code}")
    if resp1.ok:
        print("[调用成功] 返回的文本:", resp1.json().get("message", {}).get("content", ""))
    else:
        print("[调用失败] 错误详情:", resp1.text)
except Exception as e:
    print(f"[异常]: {e}")

print("\n" + "-"*40 + "\n")

# 测试2：OpenAI 兼容 API 格式
payload_openai = {
    "model": model_name,
    "messages": [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "请描述这张图片看到了什么？"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_str}"}}
            ]
        }
    ],
    "stream": False,
    "temperature": 0.1
}

print("【测试 2】：使用 OpenAI 兼容 /v1/chat/completions 格式请求")
try:
    resp2 = requests.post(api_url_openai, json=payload_openai, timeout=30)
    print(f"[HTTP 状态码]: {resp2.status_code}")
    if resp2.ok:
        print("[调用成功] 返回的文本:")
        print(resp2.json().get("choices", [{}])[0].get("message", {}).get("content", ""))
    else:
        print("[调用失败] 错误详情:", resp2.text)
except Exception as e:
    print(f"[异常]: {e}")
