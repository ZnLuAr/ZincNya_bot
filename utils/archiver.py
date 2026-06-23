"""
utils/archiver.py

文件打包模块（基础 ZIP + 7z 分卷）
"""

import os
import re
import sys
import shutil
import subprocess

from config import PROJECT_ROOT, TELEGRAM_FILE_SIZE_LIMIT_MB




def createZip(srcDir: str, outputBase: str) -> str:
    """
    创建 ZIP 压缩包

    参数：
        srcDir: 要打包的目录
        outputBase: 输出文件基础名（不含 .zip 扩展名）

    返回：
        生成的 ZIP 文件路径

    异常：
        RuntimeError: 打包失败

    示例：
        zipPath = createZip("/tmp/stickers_abc", "/download/stickers_abc")
        # 返回 "/download/stickers_abc.zip"
    """
    try:
        zipPath = shutil.make_archive(outputBase, "zip", srcDir)
        return zipPath
    except Exception as e:
        raise RuntimeError(f"ZIP 打包失败喵：{e}")


def create7zVolumes(srcPath: str, outputBase: str, volumeSizeMB: int = None) -> list[str]:
    """
    创建 7z 分卷压缩包

    优先尝试 py7zr 的 volume 参数（当前 0.22.0 不支持，会自动 fallback），
    否则调用命令行 7z 工具。

    参数：
        srcPath: 要打包的文件路径（单个文件，如 .zip）
        outputBase: 输出文件基础名（不含扩展名）
        volumeSizeMB: 每卷大小（MB），None 则从 config 读取

    返回：
        生成的分卷文件路径列表 [".7z.001", ".7z.002", ...]

    异常：
        ValueError: 源文件不存在
        RuntimeError: 分卷失败

    示例：
        volumes = create7zVolumes("/download/stickers.zip", "/download/stickers_7z", 48)
        # 返回 ["/download/stickers_7z.7z.001", "/download/stickers_7z.7z.002"]
    """
    if volumeSizeMB is None:
        volumeSizeMB = TELEGRAM_FILE_SIZE_LIMIT_MB

    volumeBytes = volumeSizeMB * 1024 * 1024

    # 规范化路径，防止路径遍历（.. 等）；并验证源文件存在
    srcPath = os.path.abspath(srcPath)
    outputBase = os.path.abspath(outputBase)
    if not os.path.isfile(srcPath):
        raise ValueError(f"源文件不存在：{os.path.basename(srcPath)}")

    # 优先尝试 py7zr（如果支持 volume 参数）
    try:
        import py7zr

        with py7zr.SevenZipFile(
            f"{outputBase}.7z",
            "w",
            volume=volumeBytes  # py7zr 0.22.0 不支持，会落入 except
        ) as archive:
            archive.write(srcPath, arcname=os.path.basename(srcPath))

        volumes = _collectVolumes(outputBase)
        if volumes:
            return volumes

    except (ImportError, TypeError, Exception):
        # py7zr 不支持 volume 或未安装，fallback 到命令行 7z
        pass

    # Fallback: 使用命令行 7z（项目中 FFmpeg 在 ffmpeg/ 目录，类似放置）
    sevenZipPath = _find7zExecutable()
    if not sevenZipPath:
        raise RuntimeError(
            "py7zr 不支持分卷且未找到 7z 命令行工具喵——\n"
            "请安装 7z 或等待 py7zr 支持 volume 参数"
        )

    # 调用 7z 命令行：7z a -v48m output.7z input.zip
    outputPath = f"{outputBase}.7z"
    volumeSpec = f"{volumeSizeMB}m"
    os.makedirs(os.path.dirname(outputPath), exist_ok=True)

    try:
        subprocess.run(
            [sevenZipPath, "a", f"-v{volumeSpec}", outputPath, srcPath],
            capture_output=True,
            text=True,
            check=True
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"7z 分卷失败喵：{e.stderr}")

    volumes = _collectVolumes(outputBase)
    if not volumes:
        raise RuntimeError("7z 分卷未生成文件喵")

    return volumes




def _collectVolumes(outputBase: str) -> list[str]:
    """
    收集 outputBase 对应的 7z 分卷文件（严格匹配 baseName.7z.NNN）

    用精确正则匹配，避免误收集同前缀的无关文件（如手动备份）。

    参数：
        outputBase: 输出文件基础名（绝对路径，不含扩展名）

    返回：
        排序后的分卷绝对路径列表
    """
    outputDir = os.path.dirname(outputBase) or '.'
    baseName = os.path.basename(outputBase)
    volumePattern = re.compile(rf"^{re.escape(baseName)}\.7z\.\d{{3}}$")

    return sorted([
        os.path.join(outputDir, f)
        for f in os.listdir(outputDir)
        if volumePattern.match(f)
    ])




def _find7zExecutable() -> str | None:
    """
    查找 7z 可执行文件

    先看项目 7z/ 目录（类似 ffmpeg/ 的放置方式），再看系统 PATH。

    返回：
        7z 可执行文件路径，找不到返回 None
    """
    if sys.platform == "win32":
        localPath = os.path.join(PROJECT_ROOT, "7z", "7z.exe")
    else:
        localPath = os.path.join(PROJECT_ROOT, "7z", "7z")

    if os.path.isfile(localPath):
        return localPath

    # 检查系统 PATH
    return shutil.which("7z") or shutil.which("7za")
