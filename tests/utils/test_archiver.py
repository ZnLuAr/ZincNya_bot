"""
tests/utils/test_archiver.py

测试 utils/archiver.py
"""

import os
import zipfile
import tempfile

import pytest

from utils.archiver import createZip, create7zVolumes


# ============================================================================
# createZip() 测试
# ============================================================================

def test_createZip_basic():
    """基础 ZIP 打包，包含正确条目"""
    with tempfile.TemporaryDirectory() as tmpdir:
        srcDir = os.path.join(tmpdir, "src")
        os.makedirs(srcDir)
        for i in range(3):
            with open(os.path.join(srcDir, f"file{i}.txt"), "w") as f:
                f.write(f"content {i}")

        zipPath = createZip(srcDir, os.path.join(tmpdir, "archive"))

        assert os.path.exists(zipPath)
        assert zipPath.endswith(".zip")
        with zipfile.ZipFile(zipPath, "r") as zf:
            assert len(zf.namelist()) == 3


def test_createZip_empty_dir():
    """空目录不报错"""
    with tempfile.TemporaryDirectory() as tmpdir:
        srcDir = os.path.join(tmpdir, "empty")
        os.makedirs(srcDir)

        zipPath = createZip(srcDir, os.path.join(tmpdir, "archive"))
        assert os.path.exists(zipPath)


def test_createZip_returns_zip_extension():
    """返回路径以 .zip 结尾"""
    with tempfile.TemporaryDirectory() as tmpdir:
        srcDir = os.path.join(tmpdir, "src")
        os.makedirs(srcDir)
        open(os.path.join(srcDir, "f.txt"), "w").close()

        zipPath = createZip(srcDir, os.path.join(tmpdir, "out"))
        assert zipPath.endswith(".zip")


# ============================================================================
# create7zVolumes() 测试
# ============================================================================

@pytest.mark.slow
@pytest.mark.skipif(
    not os.path.exists("7z/7z.exe") and not os.path.exists("7z/7z"),
    reason="需要 7z 命令行工具（py7zr 0.22.0 不支持 volume 参数）"
)
def test_create7zVolumes_large_file():
    """大文件分成多卷，每卷不超过指定大小"""
    with tempfile.TemporaryDirectory() as tmpdir:
        # 60MB 文件
        testFile = os.path.join(tmpdir, "large.bin")
        with open(testFile, "wb") as f:
            f.write(b"\x00" * (60 * 1024 * 1024))

        outputBase = os.path.join(tmpdir, "large_7z")
        volumes = create7zVolumes(testFile, outputBase, volumeSizeMB=48)

        assert len(volumes) >= 2
        assert all(os.path.exists(v) for v in volumes)
        assert all(".7z." in v for v in volumes)
        for vol in volumes:
            assert os.path.getsize(vol) / (1024 * 1024) <= 50  # 允许少量头部开销


@pytest.mark.skipif(
    not os.path.exists("7z/7z.exe") and not os.path.exists("7z/7z"),
    reason="需要 7z 命令行工具（py7zr 0.22.0 不支持 volume 参数）"
)
def test_create7zVolumes_small_file():
    """小文件生成至少 1 个分卷"""
    with tempfile.TemporaryDirectory() as tmpdir:
        testFile = os.path.join(tmpdir, "small.txt")
        with open(testFile, "w") as f:
            f.write("hello")

        outputBase = os.path.join(tmpdir, "small_7z")
        volumes = create7zVolumes(testFile, outputBase, volumeSizeMB=48)

        assert len(volumes) >= 1
        assert all(os.path.exists(v) for v in volumes)