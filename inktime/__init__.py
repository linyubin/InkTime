# -*- coding: utf-8 -*-
"""InkTime 主包。

仅定义仓库根锚点 ROOT_DIR，不导入任何子包（保持无副作用，
core.common 等基础模块可在不触发重依赖的情况下导入）。

config.py 仍位于仓库根目录（用户私有配置，gitignore），
各模块直接 ``import config`` 即可，前提是仓库根在 sys.path 上——
根目录的入口脚本（server.py 等 shim）与 cron 脚本已保证这一点。
"""
from pathlib import Path

# 仓库根：inktime/ 的上一级
ROOT_DIR = Path(__file__).resolve().parents[1]
