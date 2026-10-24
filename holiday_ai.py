#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
节假日 AI 选片（2026-09-12 设计 §6，docs/plans/2026-09-12-holiday-mode-design.md）。

仅当日命中节假日且配置了 HOLIDAY_AI_* 时介入：
把当日候选照片的缩略图发给远端视觉大模型，让其按节日氛围重排、
生成每张照片的一句话描述与横幅祝福语。任何失败整体回退（返回 None），
每日主流程零风险。

协议自动识别（与 analyze_photos.py 同一套双协议口径）：
- URL 以 /api/chat 结尾                → Ollama native（图片放 messages[].images）
- 其余（含 /v1/chat/completions）      → OpenAI 兼容（image_url data URI）；
  只填 base（如 https://open.bigmodel.cn/api/v1）自动补 /chat/completions
"""

from __future__ import annotations

import base64
import io
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from PIL import Image, ImageOps

import config as cfg
from common import resolve_chat_endpoint

# 模板 → 给模型的场景说明（帮助它理解"找什么氛围的照片"）
_TEMPLATE_DESC = {
    "general_ribbon": "通用节假日",
    "child_birthday": "儿童生日（优先有孩子、生日聚会、蛋糕的场景）",
    "elder_birthday": "长辈生日（优先这位长辈本人、全家福、寿宴场景）",
    "adult_birthday": "成人生日（优先寿星本人、朋友聚会、蛋糕蜡烛场景）",
    "anniversary": "结婚纪念日（优先夫妻合影、婚纱照、婚礼相关场景）",
}


def enabled() -> bool:
    return bool(getattr(cfg, "HOLIDAY_AI_ENABLED", False)
                and str(getattr(cfg, "HOLIDAY_AI_MODEL", "") or "").strip()
                and str(getattr(cfg, "HOLIDAY_AI_API_URL", "") or "").strip())


# ========== 图片编码 ==========

def _image_to_b64_from_img(img: Image.Image, max_edge: int) -> Optional[str]:
    """PIL 图直接缩放编码（供质检用：成品画布不走磁盘路径）。"""
    try:
        if img.mode != "RGB":
            img = img.convert("RGB")
        w, h = img.size
        long_edge = max(w, h)
        if long_edge > max_edge:
            scale = max_edge / long_edge
            img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))),
                             Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=88)
        return base64.b64encode(buf.getvalue()).decode("utf-8")
    except Exception as e:
        print(f"[HOLIDAY-QA] ⚠️ 成品图编码失败: {e}")
        return None


def _image_to_b64(path: str, max_edge: int) -> Optional[str]:
    """读图 → EXIF 纠偏 → RGB → 长边缩放 → JPEG base64。损坏图返回 None 跳过。"""
    try:
        # DB 路径可能是 Windows UNC 等跨平台路径，与主渲染管线共用同一解析
        from render_daily_photo import resolve_photo_path
        try:
            path = str(resolve_photo_path(path))
        except RuntimeError as e:
            print(f"[HOLIDAY-AI] ⚠️ 缩略图路径解析失败 {path}: {str(e).splitlines()[0]}")
            return None
        img = Image.open(path)
        try:
            img = ImageOps.exif_transpose(img)
        except Exception:
            pass
        if img.mode != "RGB":
            img = img.convert("RGB")
        w, h = img.size
        long_edge = max(w, h)
        if long_edge > max_edge:
            scale = max_edge / long_edge
            img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))),
                             Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=88)
        return base64.b64encode(buf.getvalue()).decode("utf-8")
    except Exception as e:
        print(f"[HOLIDAY-AI] ⚠️ 缩略图编码失败 {path}: {e}")
        return None


# ========== 请求与解析 ==========

def _endpoint(url: str) -> tuple:
    """(完整 URL, 协议) ；协议 ∈ {"openai", "ollama"}。
    委托 common.resolve_chat_endpoint：完整端点或 base 均可，
    与 analyze_photos.py 共用同一套识别口径。"""
    return resolve_chat_endpoint(url)


def _build_prompt(candidates: List[Dict[str, Any]], holiday: Dict[str, Any]) -> str:
    n = len(candidates)
    scene = _TEMPLATE_DESC.get(holiday.get("template", ""), "通用节假日")
    lines = [
        f"你是电子相框的节日选片助手。今天是「{holiday['name']}」，需要展示最贴合节日氛围的照片。",
        f"节日语境：{holiday.get('ai_hint') or holiday['name']}。画面将使用{scene}模板。",
        f"下面按顺序给你 {n} 张候选照片（编号 1~{n}），每张附有原选片文案供参考：",
    ]
    for i, c in enumerate(candidates, 1):
        cap = str(c.get("side") or "").strip()
        city = str(c.get("city") or "").strip()
        ctx = " / ".join(x for x in (cap, city) if x)
        lines.append(f"{i}) {ctx if ctx else '（无文案）'}")
    lines += [
        "请从中挑出最贴合今天节日氛围的照片并重排全部编号。",
        "严格只输出一个 JSON 对象，不要输出任何其他文字、解释或代码围栏：",
        f'{{"ranking": [贴合度从高到低的全部编号], '
        f'"descriptions": {{"编号": "不超过20字的照片描述"}}, '
        f'"greeting": "不超过12字的「{holiday["name"]}」祝福语"}}',
    ]
    return "\n".join(lines)


def _extract_json(text: str) -> Optional[dict]:
    """剥围栏、截取首尾大括号，容错解析模型输出。"""
    text = re.sub(r"```(?:json)?|```", "", str(text or "")).strip()
    i, j = text.find("{"), text.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        data = json.loads(text[i:j + 1])
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _call_vlm(images_b64: List[str], prompt: str) -> Optional[str]:
    url, proto = _endpoint(str(cfg.HOLIDAY_AI_API_URL))
    model = str(cfg.HOLIDAY_AI_MODEL).strip()
    key = str(getattr(cfg, "HOLIDAY_AI_API_KEY", "") or "").strip()
    timeout = int(getattr(cfg, "HOLIDAY_AI_TIMEOUT_SEC", 120) or 120)
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if proto == "ollama":
        payload = {"model": model, "stream": False, "messages": [
            {"role": "user", "content": prompt, "images": images_b64}]}
    else:
        content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
        for b64 in images_b64:
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
        payload = {"model": model, "messages": [{"role": "user", "content": content}]}
    resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if proto == "ollama":
        return str(data.get("message", {}).get("content", "") or "")
    choices = data.get("choices") or []
    if not choices:
        return None
    msg = choices[0].get("message", {})
    content = msg.get("content")
    if isinstance(content, list):  # 部分网关把 content 拆成分段数组
        content = "".join(c.get("text", "") for c in content if isinstance(c, dict))
    return str(content or "")


def qa_check(img: Image.Image, holiday: Dict[str, Any], layout_id: str) -> Dict[str, Any]:
    """
    成品图质检（2026-09-13 用户需求）：把叠加节日层之后的渲染图发给视觉大模型，
    检查装饰缺陷（遮主体/压文字/错位截断）。
    返回 {"ok": bool, "action": "keep"|"shift"|"drop", "problems": [...]}。
    未启用 / 调用失败 / 解析失败一律 fail-open（ok=True），绝不阻塞每日流程。
    """
    if not enabled() or not bool(getattr(cfg, "HOLIDAY_AI_QA", True)):
        return {"ok": True, "action": "keep", "problems": []}
    prompt = (
        "这是电子墨水屏相框的节日渲染成品图，图中红色/黄色的节日装饰"
        f"（横幅、印章、圆环、文字带等）是程序叠加的。今天是「{holiday['name']}」，"
        f"装饰模板 {holiday.get('template')}，文字版式 {layout_id or '旧版底部卡片'}。\n"
        "请只检查以下三类缺陷，不要评价照片本身的美术水准、也不要评价版式设计：\n"
        "1) 节日装饰遮挡了照片中人物的面部或完整的主要主体；\n"
        "2) 节日装饰与图中已有的文字卡片/文字内容相互重叠；\n"
        "3) 节日装饰自身文字与图形明显错位、被画布边缘裁断。\n"
        "严格只输出一个 JSON 对象，不要任何其他文字：\n"
        '{"ok": true或false, "problems": ["缺陷简述，没有则空数组"], '
        '"action": "keep"或"shift"或"drop"}\n'
        'action 含义：keep=无缺陷；shift=装饰与主体或文字冲突，换一个位置重画即可；'
        'drop=无法靠移动解决，这张建议不叠加装饰。'
    )
    b64 = _image_to_b64_from_img(img, int(getattr(cfg, "HOLIDAY_AI_IMAGE_MAX_EDGE", 512) or 512))
    if b64 is None:
        return {"ok": True, "action": "keep", "problems": []}
    try:
        raw = _call_vlm([b64], prompt)
    except Exception as e:
        print(f"[HOLIDAY-QA] ⚠️ 质检调用失败（不阻塞）: {e}")
        return {"ok": True, "action": "keep", "problems": []}
    data = _extract_json(raw or "")
    if not data or not isinstance(data.get("ok"), bool):
        print(f"[HOLIDAY-QA] ⚠️ 质检返回不可解析（不阻塞）: {str(raw)[:120]}")
        return {"ok": True, "action": "keep", "problems": []}
    action = str(data.get("action") or "keep")
    if action not in ("keep", "shift", "drop"):
        action = "keep" if data["ok"] else "drop"
    problems = [str(p) for p in data.get("problems", []) if p][:3]
    return {"ok": bool(data["ok"]), "action": action, "problems": problems}


def select_and_describe(candidates: List[Dict[str, Any]],
                        holiday: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    主入口：重排 candidates 并覆写每张的 side 描述。
    成功返回 {"photos": [重排后候选], "greeting": str}；任何失败返回 None（调用方回退）。
    """
    if not candidates:
        return None
    max_edge = int(getattr(cfg, "HOLIDAY_AI_IMAGE_MAX_EDGE", 512) or 512)
    images, kept = [], []
    for c in candidates:
        b64 = _image_to_b64(c.get("path", ""), max_edge)
        if b64 is not None:
            images.append(b64)
            kept.append(c)
    if not kept:
        print("[HOLIDAY-AI] ⚠️ 无可用缩略图，跳过 AI 选片")
        return None

    prompt = _build_prompt(kept, holiday)
    try:
        raw = _call_vlm(images, prompt)
    except Exception as e:
        print(f"[HOLIDAY-AI] ⚠️ VLM 调用失败: {e}")
        return None
    data = _extract_json(raw or "")
    if not data:
        print(f"[HOLIDAY-AI] ⚠️ 返回内容不是有效 JSON，原文前 200 字: {str(raw)[:200]}")
        return None

    n = len(kept)
    # ranking：钳制到合法下标、去重保序；漏掉的编号按原顺序补在末尾
    order: List[int] = []
    for v in data.get("ranking") if isinstance(data.get("ranking"), list) else []:
        try:
            idx = int(v) - 1
        except Exception:
            continue
        if 0 <= idx < n and idx not in order:
            order.append(idx)
    order += [i for i in range(n) if i not in order]

    desc_map = data.get("descriptions") if isinstance(data.get("descriptions"), dict) else {}
    for i, c in enumerate(kept):
        # 长度限制（2026-10-09）：L10 铭牌 368px 宽只容得下 ~24 字，超长会溢出文本框。
        desc = str(desc_map.get(str(i + 1), "") or "").strip()
        if desc:
            c["side"] = desc[:30] + ("…" if len(desc) > 30 else "")

    greeting = str(data.get("greeting", "") or "").strip()[:12]
    print(f"[HOLIDAY-AI] 模型返回: greeting=\"{greeting}\" 首选编号={order[0] + 1 if order else '-'}")
    return {"photos": [kept[i] for i in order], "greeting": greeting}
