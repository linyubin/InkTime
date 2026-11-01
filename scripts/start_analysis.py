#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
跨平台一键启动脚本 (支持 Windows, macOS, Linux)
本脚本会自动寻找项目的虚拟环境 (venv)，并使用它来启动 analyze_photos.py
"""

import os
import sys
import subprocess
import platform
from pathlib import Path

def main():
    root_dir = Path(__file__).resolve().parent
    os.chdir(root_dir)

    print("========================================")
    print("       InkTime - 照片分析启动进程       ")
    print("========================================")

    # 识别当前操作系统
    system = platform.system().lower()
    is_windows = system == "windows"
    
    # 构建虚拟环境 Python 解释器路径
    if is_windows:
        venv_python_path = root_dir / "venv" / "Scripts" / "python.exe"
    else:
        venv_python_path = root_dir / "venv" / "bin" / "python"

    # 确定要使用的 python 可执行文件
    if venv_python_path.exists():
        print(f"[INFO] 成功检测到并激活虚拟环境：{venv_python_path}")
        python_executable = str(venv_python_path)
    else:
        print("[WARN] 未检测到项目内 venv 虚拟环境，将尝试使用全局 Python 环境...")
        python_executable = "python" if is_windows else "python3"

    target_script = root_dir / "analyze_photos.py"
    if not target_script.exists():
        print(f"[ERROR] 找不到核心分析脚本：{target_script}")
        print("[ERROR] 请确保您把本启动脚本文件放在了 InkTime 项目的根目录中。")
        sys.exit(1)

    print(f"[INFO] 执行解释器: {python_executable}")
    print(f"[INFO] 执行主脚本: {target_script.name}")
    print("-" * 40)

    try:
        # 使用 subprocess 启动 analyze_photos.py，将控制台输出接管为原生体验
        subprocess.run([python_executable, target_script.name])
        print("-" * 40)
        print("[INFO] 任务结束。")
        
    except FileNotFoundError:
        print(f"\n[ERROR] 无法启动 '{python_executable}'。")
        print("请检查您的系统是否正确安装了 Python 并添加到了环境变量。")
    except KeyboardInterrupt:
        print("\n[INFO] 已收到用户的中断信号，分析进程已停止。")
    except Exception as e:
        print(f"\n[ERROR] 执行发生未知错误: {e}")

if __name__ == "__main__":
    main()
