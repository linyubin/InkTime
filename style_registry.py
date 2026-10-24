#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
声明式版式注册表（ADR 0002）：加载 layouts/*.json，供渲染管线与样式编辑器共用。

一样式一文件，文件名即版式 ID；本模块只读 JSON，不依赖任何渲染模块，
可被 layout_strategy / render_layouts / webui 安全导入（无循环依赖）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

LAYOUTS_DIR = Path(__file__).resolve().parent / "layouts"

_STYLES: Dict[str, dict] = {}


def load_styles(force: bool = False) -> Dict[str, dict]:
    """扫描 layouts/ 目录装载全部版式。

    每次调用都重扫（12 个小 JSON 读取 <5ms）——编辑器保存/外部改动（含 git
    回滚）后立即可见，不缓存陈旧列表。force 参数保留只为 API 兼容。
    """
    global _STYLES
    styles: Dict[str, dict] = {}
    for p in sorted(LAYOUTS_DIR.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[STYLE_REGISTRY] ⚠️ 跳过无法解析的 {p.name}: {e}")
            continue
        sid = str(data.get("id") or p.stem).upper()
        data["id"] = sid
        styles[sid] = data
    _STYLES = styles
    return _STYLES


def get_style(layout_id: str) -> Optional[dict]:
    return load_styles().get(str(layout_id or "").upper())


def all_styles() -> Dict[str, dict]:
    return load_styles()


def portrait_ids() -> List[str]:
    return sorted(k for k, v in load_styles().items()
                  if v.get("orientation") == "portrait")


def landscape_ids() -> List[str]:
    return sorted(k for k, v in load_styles().items()
                  if v.get("orientation") == "landscape")


def layout_names() -> Dict[str, str]:
    return {k: str(v.get("name") or "") for k, v in load_styles().items()}


def crop_reserves() -> Dict[str, Dict[str, int]]:
    """裁切避让区（版式固有属性，随样式数据走；空对象=无避让）。"""
    return {k: dict(v.get("crop_reserve") or {}) for k, v in load_styles().items()}
