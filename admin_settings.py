#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""管理端设置页的持久化与热应用。

凭据(用户名/密码)落盘到 data/admin.env(0600, KEY=VALUE 行),供 systemd
EnvironmentFile 加载;保存时同步写回 os.environ 立即生效(CSRF token 与密码
绑定,保存后旧 token 自动失效,浏览器需要重新登录)。

config.py 精选配置项按行原位改写:先落时间戳备份,再用 ast 校验改写后的
文件仍是合法 Python,最后原子替换。改动需重启服务生效(config 在导入期读取)。
"""

from __future__ import annotations

import ast
import os
import re
import tempfile
import time
from pathlib import Path

_USERNAME_RE = re.compile(r"[\w.\-]{1,64}\Z", re.ASCII)
_MIN_PASSWORD_LEN = 8
_MAX_PASSWORD_LEN = 128


class SettingsError(ValueError):
    """设置项校验或落盘失败。"""


def validate_admin_credentials(username: str, password: str) -> tuple[str, str]:
    username = (username or "").strip()
    password = password or ""
    if not _USERNAME_RE.fullmatch(username):
        raise SettingsError("用户名仅限 1~64 位字母/数字/下划线/点/连字符")
    if not (_MIN_PASSWORD_LEN <= len(password) <= _MAX_PASSWORD_LEN):
        raise SettingsError(f"密码长度需在 {_MIN_PASSWORD_LEN}~{_MAX_PASSWORD_LEN} 之间")
    if "\x00" in password:
        raise SettingsError("密码包含非法字符")
    return username, password


def write_admin_env_file(env_path: Path, username: str, password: str) -> None:
    """0600 原子落盘 KEY=VALUE;写失败不破坏旧文件。"""
    username, password = validate_admin_credentials(username, password)
    payload = (
        f"INKTIME_ADMIN_USERNAME={username}\n"
        f"INKTIME_ADMIN_PASSWORD={password}\n"
    ).encode("utf-8")
    env_path = Path(env_path)
    directory = env_path.parent
    fd, tmp_name = tempfile.mkstemp(prefix=".admin.env.", dir=str(directory))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, env_path)
        dir_fd = os.open(str(directory), os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def apply_admin_credentials(username: str, password: str) -> None:
    """写回当前进程环境;admin_security 每请求读取,即时生效。"""
    username, password = validate_admin_credentials(username, password)
    os.environ["INKTIME_ADMIN_USERNAME"] = username
    os.environ["INKTIME_ADMIN_PASSWORD"] = password


# ── config.py 精选可编辑项 ─────────────────────────────
# (类型, 下限, 上限);页面渲染与 POST 校验共用本表。
EDITABLE_CONFIG_KEYS: dict[str, tuple[type, float, float]] = {
    "DAILY_PHOTO_QUANTITY": (int, 1, 20),
    "MEMORY_THRESHOLD": (float, 0.0, 100.0),
    "SCORE_MEMORY_WEIGHT": (float, 0.0, 1.0),
    "ESP32_SERVE_WAIT_MIN": (int, 1, 240),
}

_BOOL_CONFIG_KEYS: dict[str, tuple[type, None, None]] = {
    "ENABLE_REVIEW_WEBUI": (bool, None, None),
}

_KEY_LINE_RE = {
    name: re.compile(rf"^(\s*{re.escape(name)}\s*(?::\s*\w+)?\s*=\s*).*$", re.M)
    for name in (*EDITABLE_CONFIG_KEYS, *_BOOL_CONFIG_KEYS)
}


def _format_value(name: str, value) -> str:
    if name in _BOOL_CONFIG_KEYS:
        if isinstance(value, bool):
            return "True" if value else "False"
        if isinstance(value, str) and value.lower() in ("true", "false"):
            return value.lower().capitalize()
        raise SettingsError(f"{name} 需为布尔值")
    spec = EDITABLE_CONFIG_KEYS[name]
    key_type, low, high = spec
    try:
        if key_type is int:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError
        else:
            value = float(value)
    except (TypeError, ValueError):
        raise SettingsError(f"{name} 需为 {key_type.__name__}") from None
    if not (low <= value <= high):
        raise SettingsError(f"{name} 超出允许范围 [{low}, {high}]")
    return str(value)


def validate_config_updates(updates: dict) -> dict[str, str]:
    """过滤白名单外键并校验;返回 {键: 字面量文本}。"""
    if not isinstance(updates, dict):
        raise SettingsError("配置更新必须是对象")
    validated: dict[str, str] = {}
    for name, value in updates.items():
        if name not in EDITABLE_CONFIG_KEYS and name not in _BOOL_CONFIG_KEYS:
            raise SettingsError(f"不支持的配置项: {name}")
        validated[name] = _format_value(name, value)
    return validated


def update_config_file(config_path: Path, updates: dict) -> Path:
    """按行原位改写 config.py;ast 校验 + 备份 + 原子替换,返回备份路径。

    值行被整体替换(保留行内注释会随旧注释丢失,页面提示以文件为准)。
    """
    validated = validate_config_updates(updates)
    config_path = Path(config_path)
    original = config_path.read_text(encoding="utf-8")
    new_text = original
    for name, literal in validated.items():
        pattern = _KEY_LINE_RE[name]
        if pattern.search(new_text):
            new_text = pattern.sub(lambda m: m.group(1) + literal, new_text, count=1)
        else:
            new_text += f"\n{name} = {literal}\n"
    ast.parse(new_text, filename=str(config_path))  # 非法 Python 直接拒绝
    backup = config_path.with_name(f"config.py.bak-{time.strftime('%Y%m%dT%H%M%S')}")
    backup.write_text(original, encoding="utf-8")
    fd, tmp_name = tempfile.mkstemp(prefix=".config.py.", dir=str(config_path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(new_text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, config_path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return backup
