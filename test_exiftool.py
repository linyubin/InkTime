import shutil
import subprocess
import os

def check_exiftool():
    print("========================================")
    print("      正在检查 exiftool 可用性          ")
    print("========================================\n")

    # 1. 检查环境变量中的执行路径
    exiftool_path = shutil.which("exiftool")
    
    if exiftool_path:
        print(f"✅ 在系统的 PATH 环境变量中找到了 exiftool!")
        print(f"📁 它的实际路径是: {exiftool_path}\n")
        
        # 2. 尝试实际执行它并获取版本号
        try:
            print("正在尝试运行命令: exiftool -ver")
            result = subprocess.run(
                ["exiftool", "-ver"], 
                capture_output=True, 
                text=True, 
                check=True
            )
            print(f"🎉 成功！您安装的 exiftool 版本为: {result.stdout.strip()}")
            print("\n结论：您的 exiftool 已经完美就绪，analyze_photos.py 将可以正常使用它提取高级 GPS 信息！")
        except Exception as e:
            print(f"❌ 虽然找到了路径，但执行时发生错误: {e}")
            print("可能是程序权限问题，或者是架构不匹配。")
            
    else:
        print("❌ 未能在系统中找到 'exiftool'！")
        print("\n当前终端的 Path 环境变量包含以下目录：")
        print("----------------------------------------")
        paths = os.environ.get("PATH", "").split(os.pathsep)
        for p in paths[:10]:  # 只打印前10个防止刷屏
            if p.strip():
                print(f" - {p}")
        if len(paths) > 10:
            print(f" ... (还有 {len(paths) - 10} 个路径未显示)")
        print("----------------------------------------")
        
        print("\n【诊断建议】：")
        print("1. 如果您刚才用 choco 装的，请务必【完全关闭当前终端窗口】，然后重新打开一个新的终端再试！(环境变量只有新建窗口才会刷新)")
        print("2. 如果您是自己下载解压的，它的名字可能叫 'exiftool(-k).exe'，请将其重命名为 'exiftool.exe'")
        print("3. 如果您重命名了，请确保它的存放路径已被添加进 Windows 的“环境变量 -> Path”中。")

if __name__ == "__main__":
    check_exiftool()
