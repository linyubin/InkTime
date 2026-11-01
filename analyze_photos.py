#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""InkTime 兼容入口：真实实现已迁至 inktime/analysis/analyze_photos.py。

保留本文件是为了让 systemd 服务、cron 脚本与既有文档中的
"python3 analyze_photos.py" 命令继续可用（systemd ExecStart 与 cron 均无需改动）。
新代码请直接使用 "python3 -m inktime.analysis.analyze_photos"。
"""
from runpy import run_module

run_module("inktime.analysis.analyze_photos", run_name="__main__", alter_sys=True)
