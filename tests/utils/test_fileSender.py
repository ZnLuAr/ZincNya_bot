"""
tests/utils/test_fileSender.py

测试 utils/fileSender.py
"""

import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from utils.fileSender import sendFileSmart


# ============================================================================
# sendFileSmart() 小文件路径
# ============================================================================

@pytest.mark.asyncio
async def test_sendFileSmart_small_file():
    """小文件直接发送一次 send_document"""
    with tempfile.TemporaryDirectory() as tmpdir:
        testFile = os.path.join(tmpdir, "small.txt")
        with open(testFile, "w") as f:
            f.write("small content")

        mockBot = AsyncMock()
        mockBot.send_document = AsyncMock(return_value=MagicMock(message_id=123))

        messages = await sendFileSmart(
            bot=mockBot,
            chatID=456,
            filePath=testFile,
            caption="test caption",
            deleteAfter=False
        )

        assert len(messages) == 1
        mockBot.send_document.assert_called_once()
        assert os.path.exists(testFile)


@pytest.mark.asyncio
async def test_sendFileSmart_delete_after():
    """deleteAfter=True 时发送后删除原文件"""
    with tempfile.TemporaryDirectory() as tmpdir:
        testFile = os.path.join(tmpdir, "test.txt")
        with open(testFile, "w") as f:
            f.write("content")

        mockBot = AsyncMock()
        mockBot.send_document = AsyncMock(return_value=MagicMock(message_id=123))

        await sendFileSmart(
            bot=mockBot,
            chatID=456,
            filePath=testFile,
            deleteAfter=True
        )

        assert not os.path.exists(testFile)


# ============================================================================
# sendFileSmart() 大文件分卷路径
# ============================================================================

@pytest.mark.asyncio
async def test_sendFileSmart_large_file():
    """大文件分卷：先发说明消息，再逐卷发送"""
    with tempfile.TemporaryDirectory() as tmpdir:
        testFile = os.path.join(tmpdir, "large.bin")
        # 用 mock create7zVolumes 避免真实压缩，造两个假分卷
        vol1 = os.path.join(tmpdir, "large_7z.7z.001")
        vol2 = os.path.join(tmpdir, "large_7z.7z.002")
        for v in (vol1, vol2):
            with open(v, "wb") as f:
                f.write(b"\x00" * 1024)
        # 原文件需 > 阈值才走分卷分支
        with open(testFile, "wb") as f:
            f.write(b"\x00" * (50 * 1024 * 1024))

        mockBot = AsyncMock()
        mockBot.send_message = AsyncMock()
        mockBot.send_document = AsyncMock(return_value=MagicMock(message_id=123))

        with patch("utils.fileSender.logAction", new_callable=AsyncMock), \
             patch("utils.fileSender.create7zVolumes", return_value=[vol1, vol2]):
            messages = await sendFileSmart(
                bot=mockBot,
                chatID=456,
                filePath=testFile,
                caption="large file",
                deleteAfter=True
            )

        assert len(messages) == 2
        mockBot.send_message.assert_called_once()         # 解压说明
        assert mockBot.send_document.call_count == 2      # 两卷
        assert not os.path.exists(testFile)               # 原文件已删
        assert not os.path.exists(vol1)                   # 分卷已删
        assert not os.path.exists(vol2)


@pytest.mark.asyncio
async def test_sendFileSmart_large_file_volume_failure():
    """分卷失败时抛出异常"""
    with tempfile.TemporaryDirectory() as tmpdir:
        testFile = os.path.join(tmpdir, "large.bin")
        with open(testFile, "wb") as f:
            f.write(b"\x00" * (50 * 1024 * 1024))

        mockBot = AsyncMock()

        with patch("utils.fileSender.logAction", new_callable=AsyncMock), \
             patch("utils.fileSender.create7zVolumes", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError):
                await sendFileSmart(
                    bot=mockBot,
                    chatID=456,
                    filePath=testFile,
                )