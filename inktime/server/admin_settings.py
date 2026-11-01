#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""管理端设置页的持久化与热应用。

凭据(用户名/密码)落盘到 data/admin.env(0600, KEY=VALUE 行),供 systemd
EnvironmentFile 加载;保存时同步写回 os.environ 立即生效(CSRF token 与密码
绑定,保存后旧 token 自动失效,浏览器需要重新登录)。

config.py 全量展示与编辑:
- introspect_config_file() 用 ast+tokenize 解析配置文件,按 ``═══`` 分节
  标题分组,提取每个键上方的注释块作为悬停帮助;标量(布尔/整数/浮点/字符串)
  可编辑,复杂结构(dict/list 等)与多行值只读。
- update_config_file() 用 AST 值区间精确替换(保留行首目标与行尾注释),
  改写结果先过 ast 校验,时间戳备份后原子替换。
"""

from __future__ import annotations

import ast
import io
import json
import os
import re
import tempfile
import time
import tokenize
from pathlib import Path

_USERNAME_RE = re.compile(r"[\w.\-]{1,64}\Z", re.ASCII)
_MIN_PASSWORD_LEN = 8
_MAX_PASSWORD_LEN = 128
_SECRET_NAME_RE = re.compile(r"(API_KEY|DOWNLOAD_KEY|PASSWORD|TOKEN|SECRET)\Z", re.ASCII)
_MAX_STR_LEN = 500
_SECTION_MARK = "═══"


class SettingsError(ValueError):
    """设置项校验或落盘失败。"""


# ── 管理员凭据 ─────────────────────────────────────────

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


# ── config.py 全量解析 ─────────────────────────────────

def _comment_map(text: str) -> dict[int, str]:
    """行号 -> 该行的 # 注释文本(不含 # 前导)。"""
    result: dict[int, str] = {}
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.type == tokenize.COMMENT:
            result[tok.start[0]] = tok.string.lstrip("#").strip()
    return result


def _classify(node: ast.AST) -> tuple[str, bool]:
    """返回 (kind, is_scalar)。kind: bool/int/float/str/none/complex。"""
    if not isinstance(node, ast.Constant):
        return "complex", False
    value = node.value
    if isinstance(value, bool):
        return "bool", True
    if isinstance(value, int):
        return "int", True
    if isinstance(value, float):
        return "float", True
    if isinstance(value, str):
        return "str", True
    if value is None:
        return "none", False
    return "complex", False


def _is_section_header(comment: str) -> bool:
    return _SECTION_MARK in comment


def introspect_config_file(config_path: Path) -> list[dict]:
    """解析 config.py,返回分组的设置项列表(顺序与文件一致)。

    每项:name/kind/literal(当前源码字面量)/editable/secret/help。
    """
    config_path = Path(config_path)
    text = config_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    comments = _comment_map(text)
    tree = ast.parse(text, filename=str(config_path))

    sections: list[dict] = [{"title": "常规", "fields": []}]
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            target, value_node = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and node.value is not None:
            target, value_node = node.target, node.value
        else:
            continue
        name = target.id
        if not name.isupper() or name.startswith("_"):
            continue

        line_no = value_node.lineno
        # 向上收集紧邻的注释块作为帮助文本;遇分节标题则记录并开启新分组
        help_lines: list[str] = []
        header_found: str | None = None
        walk = line_no - 1
        while walk >= 1:
            comment = comments.get(walk)
            raw_line = lines[walk - 1].strip() if walk - 1 < len(lines) else ""
            if comment is not None and raw_line.startswith("#"):
                if _is_section_header(comment):
                    header_found = comment
                    break
                help_lines.insert(0, comment)
                walk -= 1
                continue
            if raw_line == "":
                # 空行:仅当上一行仍是纯注释行时继续向上(避免吸收别的键的行内注释)
                above_comment = comments.get(walk - 1)
                above_line = lines[walk - 2].strip() if walk - 2 >= 0 else ""
                if above_comment is not None and above_line.startswith("#"):
                    walk -= 1
                    continue
                break
            break
        inline_help = comments.get(value_node.end_lineno or line_no, "")
        # 行号指向值;若赋值目标行另有独立行注释(多行赋值场景)也并入
        if not inline_help:
            inline_help = comments.get(node.lineno, "")

        kind, is_scalar = _classify(value_node)
        single_line = value_node.lineno == (value_node.end_lineno or value_node.lineno)
        literal = ""
        value = None
        if is_scalar and single_line:
            line = lines[line_no - 1]
            literal = line[value_node.col_offset:value_node.end_col_offset].strip()
            try:
                value = ast.literal_eval(literal)
            except (ValueError, SyntaxError):  # pragma: no cover - 常量字面量必可求值
                value = None

        item = {
            "name": name,
            "kind": kind,
            "literal": literal,
            "value": value,
            "editable": is_scalar and single_line and kind != "none",
            "secret": bool(_SECRET_NAME_RE.search(name)),
            "help": "\n".join(help_lines).strip(),
            "inline_help": inline_help,
        }
        if header_found is not None:
            title = header_found.strip(_SECTION_MARK).strip()
            sections.append({"title": title or "未命名分组", "fields": []})
        sections[-1]["fields"].append(item)
    return [s for s in sections if s["fields"]]


def _item_index(groups: list[dict]) -> dict[str, dict]:
    return {item["name"]: item
            for group in groups for item in group["fields"]}


def _python_literal(kind: str, value) -> str:
    """把客户端提交的 JSON 值渲染成对应类型的 Python 字面量。"""
    if kind == "bool":
        if not isinstance(value, bool):
            raise SettingsError("需为布尔值")
        return "True" if value else "False"
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise SettingsError("需为整数")
        return str(value)
    if kind == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SettingsError("需为数字")
        return repr(float(value))
    if kind == "str":
        if not isinstance(value, str):
            raise SettingsError("需为字符串")
        if not value:
            raise SettingsError("字符串不能为空(请改用删除该行的方式处理)")
        if len(value) > _MAX_STR_LEN or any(c in value for c in "\n\r\x00"):
            raise SettingsError("字符串过长或包含换行/控制字符")
        return json.dumps(value, ensure_ascii=False)
    raise SettingsError("该配置项不支持在线修改")


def update_config_file(config_path: Path, updates: dict) -> Path:
    """按 AST 值区间精确改写 config.py;返回备份路径。

    updates: {键名: 新值(JSON 类型)}。仅接受文件中已存在、单行、标量的键;
    复杂结构/多行值/未知键一律拒绝。改写保留行首赋值目标与行尾注释。
    """
    if not isinstance(updates, dict) or not updates:
        raise SettingsError("配置更新必须是非空对象")
    config_path = Path(config_path)
    text = config_path.read_text(encoding="utf-8")
    index = _item_index(introspect_config_file(config_path))

    planned: list[tuple[int, int, int, str, str]] = []  # (line_idx, col_start, col_end, literal, name)
    for name, value in updates.items():
        item = index.get(name)
        if item is None:
            raise SettingsError(f"config.py 中不存在该配置项: {name}")
        if not item["editable"]:
            raise SettingsError(f"{name} 是复杂/多行/空值项,请在文件中手动编辑")
        if item["secret"] and value == "":
            continue  # 掩码占位未修改
        literal = _python_literal(item["kind"], value)
        node_line = None
        tree = ast.parse(text)
        for node in tree.body:
            target = node.targets[0] if isinstance(node, ast.Assign) else getattr(node, "target", None)
            if isinstance(target, ast.Name) and target.id == name and node.value is not None:
                node_line = node.value
                break
        if node_line is None:  # pragma: no cover - introspection 已过滤
            raise SettingsError(f"{name} 定位失败")
        planned.append((node_line.lineno - 1, node_line.col_offset,
                        node_line.end_col_offset, literal, name))

    lines = text.splitlines(keepends=True)
    for line_idx, col_start, col_end, literal, name in planned:
        line = lines[line_idx]
        # 保留值起点之前(annotation/空格)与值终点之后(行尾注释)的内容
        lines[line_idx] = line[:col_start] + literal + line[col_end:]
    new_text = "".join(lines)
    ast.parse(new_text, filename=str(config_path))

    backup = config_path.with_name(f"config.py.bak-{time.strftime('%Y%m%dT%H%M%S')}")
    backup.write_text(text, encoding="utf-8")
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
