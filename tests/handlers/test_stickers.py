"""
tests/handlers/test_stickers.py

测试 handlers/stickers.py 的表情包搜索与下载功能
"""

import pytest
from unittest.mock import patch, AsyncMock, MagicMock

from handlers.stickers import findSticker, getCachedSticker, setCachedSticker, register


@pytest.fixture(autouse=True)
def clearStickerCache():
    """每个测试前后清空贴纸缓存"""
    import handlers.stickers as stickers_module
    stickers_module._stickerCache.clear()
    yield
    stickers_module._stickerCache.clear()


# ============================================================================
# TestFindSticker - 命令处理测试
# ============================================================================

class TestFindSticker:
    """测试 /findSticker 命令"""

    @pytest.mark.asyncio
    async def test_not_reply_shows_usage(self, mockUpdate, mockContext):
        """未回复消息时提示用法"""
        mockUpdate.message.reply_to_message = None

        with patch('handlers.stickers.logAction', new_callable=AsyncMock):
            await findSticker(mockUpdate, mockContext)

        call_args = mockUpdate.message.reply_text.call_args[0][0]
        assert "回复" in call_args

    @pytest.mark.asyncio
    async def test_reply_without_sticker_shows_usage(self, mockUpdate, mockContext):
        """回复非贴纸消息时提示用法"""
        mockUpdate.message.reply_to_message = MagicMock(sticker=None)

        with patch('handlers.stickers.logAction', new_callable=AsyncMock):
            await findSticker(mockUpdate, mockContext)

        call_args = mockUpdate.message.reply_text.call_args[0][0]
        assert "回复" in call_args

    @pytest.mark.asyncio
    async def test_sticker_without_set_shows_error(self, mockUpdate, mockContext):
        """无 set_name 的贴纸显示错误"""
        sticker = MagicMock(set_name=None)
        mockUpdate.message.reply_to_message = MagicMock(sticker=sticker)

        with patch('handlers.stickers.logAction', new_callable=AsyncMock):
            await findSticker(mockUpdate, mockContext)

        call_args = mockUpdate.message.reply_text.call_args[0][0]
        assert "没有找到" in call_args

    @pytest.mark.asyncio
    async def test_valid_sticker_shows_set_info(self, mockUpdate, mockContext):
        """有效贴纸回复贴纸集信息"""
        sticker = MagicMock(set_name="test_pack")
        mockUpdate.message.reply_to_message = MagicMock(sticker=sticker)
        mockUpdate.message.reply_text = AsyncMock(return_value=MagicMock(chat_id=123, message_id=1))

        mock_set = MagicMock(title="Test Pack", stickers=[MagicMock()] * 5)
        mockContext.bot.get_sticker_set = AsyncMock(return_value=mock_set)

        with patch('handlers.stickers.logAction', new_callable=AsyncMock), \
             patch('asyncio.create_task'):
            await findSticker(mockUpdate, mockContext)

        call_args = mockUpdate.message.reply_text.call_args[0][0]
        assert "Test Pack" in call_args or "test_pack" in call_args

    @pytest.mark.asyncio
    async def test_valid_sticker_sets_cache(self, mockUpdate, mockContext):
        """成功获取贴纸集后写入缓存（供下载阶段使用）"""
        sticker = MagicMock(set_name="new_pack")
        mockUpdate.message.reply_to_message = MagicMock(sticker=sticker)
        mockUpdate.message.reply_text = AsyncMock(return_value=MagicMock(chat_id=123, message_id=1))

        mock_set = MagicMock(title="New Pack", stickers=[MagicMock()])
        mockContext.bot.get_sticker_set = AsyncMock(return_value=mock_set)

        with patch('handlers.stickers.logAction', new_callable=AsyncMock), \
             patch('asyncio.create_task'):
            await findSticker(mockUpdate, mockContext)

        # 成功后应缓存结果
        assert getCachedSticker("new_pack") is mock_set

    @pytest.mark.asyncio
    async def test_api_error_shows_message(self, mockUpdate, mockContext):
        """API 失败时显示错误"""
        sticker = MagicMock(set_name="error_pack")
        mockUpdate.message.reply_to_message = MagicMock(sticker=sticker)
        mockContext.bot.get_sticker_set = AsyncMock(side_effect=Exception("Network error"))

        with patch('handlers.stickers.logAction', new_callable=AsyncMock):
            await findSticker(mockUpdate, mockContext)

        call_args = mockUpdate.message.reply_text.call_args[0][0]
        assert "失败" in call_args


# ============================================================================
# TestStickerCache - 缓存测试
# ============================================================================

class TestStickerCache:
    """测试贴纸集缓存"""

    def test_cache_hit_returns_data(self):
        """缓存命中返回数据"""
        mock_set = MagicMock(title="Test")
        setCachedSticker("test_pack", mock_set)

        result = getCachedSticker("test_pack")

        assert result is mock_set

    def test_cache_miss_returns_none(self):
        """缓存未命中返回 None"""
        result = getCachedSticker("nonexistent_pack")

        assert result is None


# ============================================================================
# TestStickersRegister - 注册测试
# ============================================================================

class TestStickersRegister:
    """测试 handler 注册"""

    def test_register_returns_correct_metadata(self):
        """register() 返回正确的元数据结构"""
        result = register()

        assert isinstance(result, dict)
        assert "handlers" in result
        assert "name" in result
        assert len(result["handlers"]) == 2  # CommandHandler + CallbackQueryHandler