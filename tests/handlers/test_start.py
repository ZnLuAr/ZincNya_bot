"""
tests/handlers/test_start.py

测试 handlers/start.py 的 /start 命令注册
"""

import pytest
from unittest.mock import patch, AsyncMock

from handlers.start import register


class TestStartRegister:

    def test_register_returns_correct_metadata(self):
        result = register()

        assert isinstance(result, dict)
        assert "handlers" in result
        assert len(result["handlers"]) == 1

    def test_register_auth_false(self):
        """/start 自己处理鉴权，auth 应为 False"""
        result = register()

        assert result.get("auth") is False

    @pytest.mark.asyncio
    async def test_delegates_to_whitelist_manager(self, mockUpdate, mockContext):
        """处理 /start 时委托给 whitelistManager.handleStart"""
        with patch('handlers.start.handleStart', new_callable=AsyncMock) as mock_handle:
            # 直接调用已注册的 handler 函数
            handler = register()["handlers"][0]
            callback = handler.callback
            await callback(mockUpdate, mockContext)

        mock_handle.assert_called_once_with(mockUpdate, mockContext)