#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""InkTime 兼容入口：真实实现已迁至 inktime/devices/push_to_epd_ble.py。

保留本文件是为了让 systemd 服务、cron 脚本与既有文档中的
"python3 push_to_epd_ble.py" 命令继续可用（systemd ExecStart 与 cron 均无需改动）。
新代码请直接使用 "python3 -m inktime.devices.push_to_epd_ble"。
"""
from runpy import run_module

run_module("inktime.devices.push_to_epd_ble", run_name="__main__", alter_sys=True)
