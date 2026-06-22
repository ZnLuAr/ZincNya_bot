"""
tests/handlers/test_nya.py

测试 handlers/nya.py 的 Nya 语录功能
"""

import pytest
from unittest.mock import patch, AsyncMock

from handlers.nya import sendNya, register


class TestSendNya:

    @pytest.mark.asyncio
    async def test_sends_single_message(self, mockUpdate, mockContext):
        """正常发送单条语录"""
        with patch('handlers.nya.getRandomQuote', return_value=["喵~"]), \
             patch('handlers.nya.logAction', new_callable=AsyncMock):
            await sendNya(mockUpdate, mockContext)

        mockUpdate.message.reply_text.assert_called_once_with("喵~")

    @pytest.mark.asyncio
    async def test_sends_multiple_messages_with_delay(self, mockUpdate, mockContext):
        """多条语录依次发送"""
        with patch('handlers.nya.getRandomQuote', return_value=["消息1", "消息2"]), \
             patch('handlers.nya.asyncio.sleep', new_callable=AsyncMock) as mock_sleep, \
             patch('handlers.nya.logAction', new_callable=AsyncMock):
            await sendNya(mockUpdate, mockContext)

        assert mockUpdate.message.reply_text.call_count == 2
        mock_sleep.assert_called_once()  # 两条消息之间有一次延迟

    @pytest.mark.asyncio
    async def test_skips_empty_messages(self, mockUpdate, mockContext):
        """跳过空消息"""
        with patch('handlers.nya.getRandomQuote', return_value=["", "喵~", "  "]), \
             patch('handlers.nya.logAction', new_callable=AsyncMock):
            await sendNya(mockUpdate, mockContext)

        mockUpdate.message.reply_text.assert_called_once_with("喵~")

    @pytest.mark.asyncio
    async def test_empty_list_shows_fallback(self, mockUpdate, mockContext):
        """语录库空时显示备用消息"""
        with patch('handlers.nya.getRandomQuote', return_value=[]), \
             patch('handlers.nya.logAction', new_callable=AsyncMock):
            await sendNya(mockUpdate, mockContext)

        call_args = mockUpdate.message.reply_text.call_args[0][0]
        assert "ご主人" in call_args or "语录库" in call_args or "呜" in call_args


class TestNyaRegister:

    def test_register_returns_correct_metadata(self):
        result = register()

        assert isinstance(result, dict)
        assert "handlers" in result
        assert result["name"] == "Nya 语录"
        assert len(result["handlers"]) == 1