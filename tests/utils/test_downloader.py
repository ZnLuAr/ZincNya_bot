"""
tests/utils/test_downloader.py

测试 utils/downloader.py
"""

import pytest
import asyncio
from unittest.mock import patch, AsyncMock, MagicMock
from telegram.error import TelegramError, NetworkError

from utils.downloader import (
    _sanitizeSetName,
    getActiveGifJobs,
    _ensureFFmpeg,
    convertToGif,
    downloadEachOne,
    createStickerZip,
    deleteLater,
    deleteMessageLater,
)


# ============================================================================
# _sanitizeSetName() 测试
# ============================================================================

def test_sanitize_set_name_valid():
    """合法名称保留"""
    assert _sanitizeSetName("cute_cats") == "cute_cats"
    assert _sanitizeSetName("Test-123") == "Test-123"


def test_sanitize_set_name_invalid_chars():
    """非法字符替换为下划线"""
    assert _sanitizeSetName("cute@cats!") == "cute_cats_"
    assert _sanitizeSetName("a.b/c") == "a_b_c"


def test_sanitize_set_name_empty():
    """空字符串返回 sticker"""
    assert _sanitizeSetName("") == "sticker"


def test_sanitize_set_name_unicode():
    """Unicode 字符替换"""
    result = _sanitizeSetName("可爱猫咪")
    # 所有非 ASCII 字符都应被替换
    assert all(c == '_' or c.isascii() for c in result)


# ============================================================================
# getActiveGifJobs() 测试
# ============================================================================

def test_get_active_gif_jobs_initial():
    """初始值为 0"""
    # 注意：因为是全局变量，可能受其他测试影响，仅验证返回类型
    assert isinstance(getActiveGifJobs(), int)
    assert getActiveGifJobs() >= 0


# ============================================================================
# _ensureFFmpeg() 测试
# ============================================================================

def test_ensure_ffmpeg_missing():
    """FFMPEG 为 None 时抛出 RuntimeError"""
    with patch('utils.downloader.FFMPEG', None):
        with pytest.raises(RuntimeError, match="缺失 ffmpeg"):
            _ensureFFmpeg()


def test_ensure_ffmpeg_available():
    """FFMPEG 可用时不抛出"""
    with patch('utils.downloader.FFMPEG', "/path/to/ffmpeg"):
        # 不应抛出异常
        _ensureFFmpeg()


# ============================================================================
# convertToGif() 测试
# ============================================================================

@pytest.mark.asyncio
async def test_convert_to_gif_tgs_unsupported():
    """.tgs 格式抛出 ValueError"""
    with pytest.raises(ValueError, match="不支持将 .tgs 格式"):
        await convertToGif("/path/to/sticker.tgs")


@pytest.mark.asyncio
async def test_convert_to_gif_unsupported_format():
    """不支持的格式抛出 ValueError"""
    with pytest.raises(ValueError, match="不支持的格式"):
        await convertToGif("/path/to/sticker.png")

    with pytest.raises(ValueError, match="不支持的格式"):
        await convertToGif("/path/to/sticker.jpg")


@pytest.mark.asyncio
async def test_convert_to_gif_webp_static():
    """静态 WebP 转换（fps=1）"""
    # Mock subprocess: probe + palettegen + paletteuse
    probe_proc = MagicMock()
    probe_proc.communicate = AsyncMock(
        return_value=(b"", b"frame=    1 ... Video: vp9, yuv420p, 100x100")
    )
    probe_proc.returncode = 0

    proc1 = MagicMock()
    proc1.communicate = AsyncMock(return_value=(b"", b""))
    proc1.returncode = 0

    proc2 = MagicMock()
    proc2.communicate = AsyncMock(return_value=(b"", b""))
    proc2.returncode = 0

    procs = [probe_proc, proc1, proc2]
    proc_iter = iter(procs)

    async def mock_create_subprocess(*args, **kwargs):
        return next(proc_iter)

    with patch('utils.downloader.FFMPEG', "/path/to/ffmpeg"):
        with patch('utils.downloader.asyncio.create_subprocess_exec', side_effect=mock_create_subprocess):
            with patch('utils.downloader.tempfile.TemporaryDirectory') as mock_tmp:
                mock_tmp.return_value.__enter__ = MagicMock(return_value="/tmp/fake")
                mock_tmp.return_value.__exit__ = MagicMock(return_value=None)
                result = await convertToGif("/path/to/sticker.webp")

    assert result.endswith(".gif")


@pytest.mark.asyncio
async def test_convert_to_gif_palettegen_failure():
    """palettegen 失败抛出 RuntimeError"""
    probe_proc = MagicMock()
    probe_proc.communicate = AsyncMock(
        return_value=(b"", b"frame=  100 ... Video: vp9, yuv420p, 512x512")
    )
    probe_proc.returncode = 0

    proc1 = MagicMock()
    proc1.communicate = AsyncMock(return_value=(b"", b"palettegen error"))
    proc1.returncode = 1  # 失败

    procs = [probe_proc, proc1]
    proc_iter = iter(procs)

    async def mock_create_subprocess(*args, **kwargs):
        return next(proc_iter)

    with patch('utils.downloader.FFMPEG', "/path/to/ffmpeg"):
        with patch('utils.downloader.asyncio.create_subprocess_exec', side_effect=mock_create_subprocess):
            with patch('utils.downloader.tempfile.TemporaryDirectory') as mock_tmp:
                mock_tmp.return_value.__enter__ = MagicMock(return_value="/tmp/fake")
                mock_tmp.return_value.__exit__ = MagicMock(return_value=None)
                with pytest.raises(RuntimeError):
                    await convertToGif("/path/to/sticker.webp")


# ============================================================================
# downloadEachOne() 测试
# ============================================================================

@pytest.mark.asyncio
async def test_download_each_one_webp_success():
    """WebP 格式下载成功"""
    mock_file = MagicMock()
    mock_file.download_to_drive = AsyncMock()

    mock_bot = MagicMock()
    mock_bot.get_file = AsyncMock(return_value=mock_file)

@pytest.mark.asyncio
async def test_download_each_one_webm_renamed():
    """WebM 格式 MIME 检测和重命名"""
    mock_file = MagicMock()
    mock_file.download_to_drive = AsyncMock()

    mock_bot = MagicMock()
    mock_bot.get_file = AsyncMock(return_value=mock_file)

    with patch('utils.downloader.magic.from_file', return_value="video/webm"):
        with patch('utils.downloader.os.rename') as mock_rename:
            result = await downloadEachOne(mock_bot, "file_id", "/path/to/out.webp", "webm")

    assert result["ok"] is True
    mock_rename.assert_called_once()
    # 验证重命名为 .webm
    call_args = mock_rename.call_args
    assert call_args[0][1].endswith(".webm")


@pytest.mark.asyncio
async def test_download_each_one_tgs_renamed():
    """TGS 格式 MIME 检测和重命名"""
    mock_file = MagicMock()
    mock_file.download_to_drive = AsyncMock()

    mock_bot = MagicMock()
    mock_bot.get_file = AsyncMock(return_value=mock_file)

    with patch('utils.downloader.magic.from_file', return_value="application/x-tgsticker"):
        with patch('utils.downloader.os.rename') as mock_rename:
            result = await downloadEachOne(mock_bot, "file_id", "/path/to/out.webp", "webp")

    assert result["ok"] is True
    call_args = mock_rename.call_args
    assert call_args[0][1].endswith(".tgs")


@pytest.mark.asyncio
async def test_download_each_one_telegram_error_retry():
    """TelegramError 重试机制"""
    mock_file = MagicMock()
    mock_file.download_to_drive = AsyncMock()

    mock_bot = MagicMock()
    # 前两次失败，第三次成功
    mock_bot.get_file = AsyncMock(
        side_effect=[
            TelegramError("Network error"),
            TelegramError("Network error"),
            mock_file,
        ]
    )

    with patch('utils.downloader.magic.from_file', return_value="image/webp"):
        with patch('utils.downloader.asyncio.sleep', new_callable=AsyncMock):
            with patch('utils.downloader.MAX_DOWNLOADS_ATTEMPTS', 3):
                result = await downloadEachOne(mock_bot, "file_id", "/path/to/out.webp", "webp")

    assert result["ok"] is True
    assert mock_bot.get_file.call_count == 3


@pytest.mark.asyncio
async def test_download_each_one_max_retries_exhausted():
    """达到最大重试次数失败"""
    mock_bot = MagicMock()
    mock_bot.get_file = AsyncMock(side_effect=TelegramError("Always fail"))

    with patch('utils.downloader.asyncio.sleep', new_callable=AsyncMock):
        with patch('utils.downloader.MAX_DOWNLOADS_ATTEMPTS', 2):
            result = await downloadEachOne(mock_bot, "file_id", "/path/to/out.webp", "webp")

    assert result["ok"] is False
    assert "error" in result


@pytest.mark.asyncio
async def test_download_each_one_unexpected_error():
    """不可预期异常返回错误"""
    mock_bot = MagicMock()
    mock_bot.get_file = AsyncMock(side_effect=ValueError("Unexpected error"))

# ============================================================================
# createStickerZip() 测试
# ============================================================================

@pytest.mark.asyncio
async def test_create_sticker_zip_basic(tmp_path):
    """正常下载并打包"""
    # Mock sticker 对象
    mock_sticker = MagicMock()
    mock_sticker.file_id = "file_id_123"

    mock_sticker_set = MagicMock()
    mock_sticker_set.stickers = [mock_sticker]

    mock_bot = MagicMock()

    # Mock downloadEachOne 返回成功
    async def mock_download(*args, **kwargs):
        return {"ok": True, "converted": False}

    with patch('utils.downloader.downloadEachOne', side_effect=mock_download):
        with patch('utils.downloader.shutil.make_archive', return_value=str(tmp_path / "test.zip")):
            with patch('utils.downloader.shutil.rmtree') as mock_rmtree:
                with patch('utils.downloader.logAction', new_callable=AsyncMock):
                    result = await createStickerZip(
                        bot=mock_bot,
                        stickerSet=mock_sticker_set,
                        setName="test_set",
                        stickerSuffix="webp",
                        outputDir=str(tmp_path)
                    )

    assert result.endswith(".zip")
    # 验证清理临时目录
    mock_rmtree.assert_called_once()


@pytest.mark.asyncio
async def test_create_sticker_zip_gif_counter(tmp_path):
    """GIF 模式下，活跃任务计数增加然后释放"""
    mock_sticker = MagicMock()
    mock_sticker.file_id = "file_id_123"

    mock_sticker_set = MagicMock()
    mock_sticker_set.stickers = [mock_sticker]

    mock_bot = MagicMock()

    async def mock_download(*args, **kwargs):
        # 在下载过程中，计数应该 >= 1
        assert getActiveGifJobs() >= 1
        return {"ok": True, "converted": True}

    initial_count = getActiveGifJobs()

    with patch('utils.downloader.downloadEachOne', side_effect=mock_download):
        with patch('utils.downloader.shutil.make_archive', return_value=str(tmp_path / "test.zip")):
            with patch('utils.downloader.shutil.rmtree'):
                with patch('utils.downloader.logAction', new_callable=AsyncMock):
                    await createStickerZip(
                        bot=mock_bot,
                        stickerSet=mock_sticker_set,
                        setName="test_set",
                        stickerSuffix="gif",
                        outputDir=str(tmp_path)
                    )

    # 任务结束后计数恢复
    assert getActiveGifJobs() == initial_count


@pytest.mark.asyncio
async def test_create_sticker_zip_cleanup_on_exception(tmp_path):
    """异常时仍清理临时目录"""
    mock_sticker = MagicMock()
    mock_sticker.file_id = "file_id_123"

    mock_sticker_set = MagicMock()
    mock_sticker_set.stickers = [mock_sticker]

    mock_bot = MagicMock()

    async def mock_download(*args, **kwargs):
        raise RuntimeError("Download failed")

    with patch('utils.downloader.downloadEachOne', side_effect=mock_download):
        with patch('utils.downloader.shutil.rmtree') as mock_rmtree:
            with patch('utils.downloader.logAction', new_callable=AsyncMock):
                with pytest.raises(RuntimeError):
                    await createStickerZip(
                        bot=mock_bot,
                        stickerSet=mock_sticker_set,
                        setName="test_set",
                        stickerSuffix="webp",
                        outputDir=str(tmp_path)
                    )

# ============================================================================
# deleteLater() 测试
# ============================================================================

@pytest.mark.asyncio
async def test_delete_later_success():
    """延迟后删除消息和文件"""
    mockContext = MagicMock()
    mockContext.bot.delete_message = AsyncMock()

    with patch('utils.downloader.asyncio.sleep', new_callable=AsyncMock):
        with patch('utils.downloader.os.path.exists', return_value=True):
            with patch('utils.downloader.os.remove') as mock_remove:
                await deleteLater(mockContext, 123, 456, "/path/to/file.zip", 1)

    mockContext.bot.delete_message.assert_called_once_with(123, message_id=456)
    mock_remove.assert_called_once_with("/path/to/file.zip")


@pytest.mark.asyncio
async def test_delete_later_no_file_path():
    """filePath 为 None 不删除"""
    mockContext = MagicMock()
    mockContext.bot.delete_message = AsyncMock()

    with patch('utils.downloader.asyncio.sleep', new_callable=AsyncMock):
        with patch('utils.downloader.os.remove') as mock_remove:
            await deleteLater(mockContext, 123, 456, None, 1)

    mock_remove.assert_not_called()


@pytest.mark.asyncio
async def test_delete_later_file_not_exists():
    """文件不存在不抛出异常"""
    mockContext = MagicMock()
    mockContext.bot.delete_message = AsyncMock()

    with patch('utils.downloader.asyncio.sleep', new_callable=AsyncMock):
        with patch('utils.downloader.os.path.exists', return_value=False):
            with patch('utils.downloader.os.remove') as mock_remove:
                # 不应抛出异常
                await deleteLater(mockContext, 123, 456, "/path/to/missing.zip", 1)

    mock_remove.assert_not_called()


@pytest.mark.asyncio
async def test_delete_later_message_delete_fails():
    """delete_message 失败不抛出异常"""
    mockContext = MagicMock()
    mockContext.bot.delete_message = AsyncMock(side_effect=Exception("Failed"))

    with patch('utils.downloader.asyncio.sleep', new_callable=AsyncMock):
        with patch('utils.downloader.os.path.exists', return_value=False):
            # 不应抛出异常
            await deleteLater(mockContext, 123, 456, None, 1)


# ============================================================================
# deleteMessageLater() 测试
# ============================================================================

@pytest.mark.asyncio
async def test_delete_message_later_success():
    """延迟后删除消息"""
    mockContext = MagicMock()
    mockContext.bot.delete_message = AsyncMock()

    with patch('utils.downloader.asyncio.sleep', new_callable=AsyncMock):
        await deleteMessageLater(mockContext, 123, 456, 1)

    mockContext.bot.delete_message.assert_called_once_with(123, message_id=456)


@pytest.mark.asyncio
async def test_delete_message_later_failure_silent():
    """删除消息失败不抛出异常"""
    mockContext = MagicMock()
    mockContext.bot.delete_message = AsyncMock(side_effect=Exception("Failed"))

    with patch('utils.downloader.asyncio.sleep', new_callable=AsyncMock):
        # 不应抛出异常
        await deleteMessageLater(mockContext, 123, 456, 1)

