#!/usr/bin/env python3
"""
setup_ffmpeg.py

自动下载并配置 FFmpeg 的脚本。
支持 Windows 和 Linux 平台。
"""

import os
import sys
import platform
import urllib.request
import zipfile
import tarfile
import shutil
from pathlib import Path


SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent
FFMPEG_DIR = PROJECT_ROOT / "ffmpeg"


def detect_platform():
    """检测操作系统平台"""
    system = platform.system()
    if system == "Windows":
        return "windows"
    elif system == "Linux":
        return "linux"
    elif system == "Darwin":
        return "macos"
    else:
        return "unknown"


def download_file(url, destination):
    """下载文件并显示进度"""
    print(f"正在下载：{url}")

    def progress_hook(block_num, block_size, total_size):
        downloaded = block_num * block_size
        if total_size > 0:
            percent = min(downloaded * 100 / total_size, 100)
            bar_length = 40
            filled = int(bar_length * percent / 100)
            bar = "█" * filled + "░" * (bar_length - filled)
            print(f"\r下载中 [{bar}] {percent:.1f}%", end="", flush=True)

    try:
        urllib.request.urlretrieve(url, destination, reporthook=progress_hook)
        print("\n下载完成！")
        return True
    except Exception as e:
        print(f"\n❌ 下载失败：{e}")
        return False


def setup_windows():
    """Windows 平台配置"""
    print("\n检测到 Windows 平台")

    # FFmpeg 下载 URL（使用 GitHub releases）
    url = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"

    # 临时下载路径
    temp_zip = PROJECT_ROOT / "temp_ffmpeg.zip"

    # 下载
    if not download_file(url, temp_zip):
        return False

    # 解压
    print("\n正在解压文件...")
    try:
        with zipfile.ZipFile(temp_zip, 'r') as zip_ref:
            # 查找 ffmpeg.exe
            for member in zip_ref.namelist():
                if member.endswith("bin/ffmpeg.exe"):
                    # 提取到 ffmpeg 文件夹
                    source = zip_ref.open(member)
                    target = FFMPEG_DIR / "ffmpeg.exe"
                    with open(target, 'wb') as f:
                        shutil.copyfileobj(source, f)
                    print(f"✓ 已提取：{target}")
                    break

        # 删除临时文件
        temp_zip.unlink()
        print("✓ 清理完成")
        return True

    except Exception as e:
        print(f"❌ 解压失败：{e}")
        if temp_zip.exists():
            temp_zip.unlink()
        return False


def setup_linux():
    """Linux 平台配置"""
    print("\n检测到 Linux 平台")
    print("\n推荐使用系统包管理器安装 FFmpeg：")
    print("  Debian/Ubuntu: sudo apt install ffmpeg")
    print("  Fedora:        sudo dnf install ffmpeg")
    print("  Arch:          sudo pacman -S ffmpeg")

    response = input("\n是否使用系统包管理器安装？(y/n): ").strip().lower()

    if response == 'y':
        print("\n请在终端中运行相应的安装命令。")
        return True
    else:
        print("\n如需手动下载静态构建版本，请访问：")
        print("https://johnvansickle.com/ffmpeg/")
        return False


def setup_macos():
    """macOS 平台配置"""
    print("\n检测到 macOS 平台")
    print("\n推荐使用 Homebrew 安装 FFmpeg：")
    print("  brew install ffmpeg")

    response = input("\n是否使用 Homebrew 安装？(y/n): ").strip().lower()

    if response == 'y':
        print("\n请在终端中运行：brew install ffmpeg")
        return True
    else:
        print("\n如需手动下载，请访问：https://ffmpeg.org/download.html")
        return False


def verify_ffmpeg():
    """验证 FFmpeg 是否可用"""
    print("\n正在验证 FFmpeg...")

    # 检查项目文件夹
    ffmpeg_exe = FFMPEG_DIR / ("ffmpeg.exe" if platform.system() == "Windows" else "ffmpeg")

    if ffmpeg_exe.exists():
        print(f"✓ 找到 FFmpeg：{ffmpeg_exe}")
        return True

    # 检查系统 PATH
    ffmpeg_cmd = "ffmpeg.exe" if platform.system() == "Windows" else "ffmpeg"
    if shutil.which(ffmpeg_cmd):
        print(f"✓ 系统 PATH 中存在 FFmpeg")
        return True

    print("❌ 未找到 FFmpeg")
    return False


def main():
    print("=" * 60)
    print("ZincNya Bot - FFmpeg 自动配置脚本")
    print("=" * 60)

    # 确保 ffmpeg 文件夹存在
    FFMPEG_DIR.mkdir(exist_ok=True)

    # 检测平台
    platform_type = detect_platform()

    if platform_type == "windows":
        success = setup_windows()
    elif platform_type == "linux":
        success = setup_linux()
    elif platform_type == "macos":
        success = setup_macos()
    else:
        print(f"❌ 不支持的平台：{platform.system()}")
        print("请手动下载 FFmpeg：https://ffmpeg.org/download.html")
        return 1

    # 验证
    if success:
        verify_ffmpeg()

    print("\n" + "=" * 60)
    print("配置完成！")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
