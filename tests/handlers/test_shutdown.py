"""
tests/handlers/test_shutdown.py

测试 handlers/shutdown.py
"""

import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from telegram.ext import ApplicationHandlerStop

from handlers.shutdown import (
    shutdown,
    restart,
    status,
    _mentionDispatch,
    register,
)


# ============================================================================
# shutdown() 测试
# ============================================================================

@pytest.mark.asyncio
async def test_shutdown_authorized():
    """有权限用户触发关机"""
    mockUser = MagicMock()
    mockUser.id = 123
    mockUser.full_name = "Alice"

    mockMessage = MagicMock()
    mockMessage.reply_text = AsyncMock()

    mockUpdate = MagicMock()
    mockUpdate.effective_user = mockUser
    mockUpdate.message = mockMessage

    mock_state_manager = MagicMock()

    with patch('handlers.shutdown.hasPermission', return_value=True):
        with patch('handlers.shutdown.getStateManager', return_value=mock_state_manager):
            with patch('handlers.shutdown.logSystemEvent', new_callable=AsyncMock):
                await shutdown(mockUpdate, MagicMock())

    mock_state_manager.requestShutdown.assert_called_once()
    mockMessage.reply_text.assert_called_once()
    assert "关机" in mockMessage.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_shutdown_unauthorized():
    """无权限用户被拒绝"""
    mockUser = MagicMock()
    mockUser.id = 999

    mockMessage = MagicMock()
    mockMessage.reply_text = AsyncMock()

    mockUpdate = MagicMock()
    mockUpdate.effective_user = mockUser
    mockUpdate.message = mockMessage

    mock_state_manager = MagicMock()

    with patch('handlers.shutdown.hasPermission', return_value=False):
        with patch('handlers.shutdown.getStateManager', return_value=mock_state_manager):
            await shutdown(mockUpdate, MagicMock())

    mock_state_manager.requestShutdown.assert_not_called()
# ============================================================================
# restart() 测试
# ============================================================================

@pytest.mark.asyncio
async def test_restart_authorized():
    """有权限用户触发重启"""
    mockUser = MagicMock()
    mockUser.id = 123
    mockUser.full_name = "Bob"

    mockMessage = MagicMock()
    mockMessage.reply_text = AsyncMock()

    mockUpdate = MagicMock()
    mockUpdate.effective_user = mockUser
    mockUpdate.message = mockMessage

    mock_state_manager = MagicMock()

    with patch('handlers.shutdown.hasPermission', return_value=True):
        with patch('handlers.shutdown.getStateManager', return_value=mock_state_manager):
            with patch('handlers.shutdown.logSystemEvent', new_callable=AsyncMock):
                await restart(mockUpdate, MagicMock())

    mock_state_manager.requestRestart.assert_called_once()
    mockMessage.reply_text.assert_called_once()
    assert "重启" in mockMessage.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_restart_unauthorized():
    """无权限用户被拒绝"""
    mockUser = MagicMock()
    mockUser.id = 999

    mockMessage = MagicMock()
    mockMessage.reply_text = AsyncMock()

    mockUpdate = MagicMock()
    mockUpdate.effective_user = mockUser
    mockUpdate.message = mockMessage

    mock_state_manager = MagicMock()

    with patch('handlers.shutdown.hasPermission', return_value=False):
        with patch('handlers.shutdown.getStateManager', return_value=mock_state_manager):
            await restart(mockUpdate, MagicMock())

    mock_state_manager.requestRestart.assert_not_called()
    mockMessage.reply_text.assert_called_once()
    assert "权限" in mockMessage.reply_text.call_args[0][0]


# ============================================================================
# status() 测试
# ============================================================================

@pytest.mark.asyncio
async def test_status_authorized():
    """有权限用户查看状态"""
    mockUser = MagicMock()
    mockUser.id = 123

    mockMessage = MagicMock()
    mockMessage.reply_text = AsyncMock()

    mockUpdate = MagicMock()
    mockUpdate.effective_user = mockUser
    mockUpdate.message = mockMessage

    mock_process = MagicMock()
    mock_process.memory_info.return_value.rss = 100 * 1024 * 1024  # 100 MB
    mock_process.cpu_percent.return_value = 5.5

    with patch('handlers.shutdown.hasPermission', return_value=True):
        with patch('handlers.shutdown.psutil.Process', return_value=mock_process):
            with patch('handlers.shutdown.asyncio.to_thread', new_callable=AsyncMock, return_value=5.5):
                with patch('handlers.shutdown._START_TIME', 1000.0):
                    with patch('handlers.shutdown.time.time', return_value=1000.0 + 3661):  # 1小时1分1秒
                        await status(mockUpdate, MagicMock())

    mockMessage.reply_text.assert_called_once()
    status_text = mockMessage.reply_text.call_args[0][0]
    assert "1 小时" in status_text
    assert "1 分钟" in status_text
    assert "1 秒" in status_text
    assert "100.00 MB" in status_text
    assert "5.5%" in status_text


@pytest.mark.asyncio
async def test_status_unauthorized():
    """无权限用户被拒绝"""
    mockUser = MagicMock()
    mockUser.id = 999

    mockMessage = MagicMock()
    mockMessage.reply_text = AsyncMock()

    mockUpdate = MagicMock()
    mockUpdate.effective_user = mockUser
    mockUpdate.message = mockMessage

    with patch('handlers.shutdown.hasPermission', return_value=False):
        await status(mockUpdate, MagicMock())

    mockMessage.reply_text.assert_called_once()
    assert "权限" in mockMessage.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_status_uptime_days():
    """运行时长包含天数"""
    mockUser = MagicMock()
    mockUser.id = 123

    mockMessage = MagicMock()
    mockMessage.reply_text = AsyncMock()

    mockUpdate = MagicMock()
    mockUpdate.effective_user = mockUser
    mockUpdate.message = mockMessage

    mock_process = MagicMock()
    mock_process.memory_info.return_value.rss = 50 * 1024 * 1024
    mock_process.cpu_percent.return_value = 2.0

    with patch('handlers.shutdown.hasPermission', return_value=True):
        with patch('handlers.shutdown.psutil.Process', return_value=mock_process):
            with patch('handlers.shutdown.asyncio.to_thread', new_callable=AsyncMock, return_value=2.0):
                with patch('handlers.shutdown._START_TIME', 1000.0):
                    # 2天3小时4分5秒
                    uptime = 2 * 86400 + 3 * 3600 + 4 * 60 + 5
                    with patch('handlers.shutdown.time.time', return_value=1000.0 + uptime):
                        await status(mockUpdate, MagicMock())

    status_text = mockMessage.reply_text.call_args[0][0]
    assert "2 天" in status_text
    assert "3 小时" in status_text
# ============================================================================
# _mentionDispatch() 测试
# ============================================================================

@pytest.mark.asyncio
async def test_mention_dispatch_keyword_match():
    """@bot + 关键词匹配"""
    mockUser = MagicMock()
    mockUser.id = 123
    mockUser.full_name = "Alice"

    mockMessage = MagicMock()
    mockMessage.text = "@testbot 关机"
    mockMessage.reply_text = AsyncMock()

    mockUpdate = MagicMock()
    mockUpdate.message = mockMessage
    mockUpdate.effective_user = mockUser

    mockContext = MagicMock()
    mockContext.bot.username = "testbot"

    mock_state_manager = MagicMock()

    with patch('handlers.shutdown.hasPermission', return_value=True):
        with patch('handlers.shutdown.getStateManager', return_value=mock_state_manager):
            with patch('handlers.shutdown.logSystemEvent', new_callable=AsyncMock):
                with pytest.raises(ApplicationHandlerStop):
                    await _mentionDispatch(mockUpdate, mockContext)

    mock_state_manager.requestShutdown.assert_called_once()


@pytest.mark.asyncio
async def test_mention_dispatch_alias_match():
    """@bot + 别名匹配"""
    mockUser = MagicMock()
    mockUser.id = 123
    mockUser.full_name = "Bob"

    mockMessage = MagicMock()
    mockMessage.text = "@testbot 去睡觉"
    mockMessage.reply_text = AsyncMock()

    mockUpdate = MagicMock()
    mockUpdate.message = mockMessage
    mockUpdate.effective_user = mockUser

    mockContext = MagicMock()
    mockContext.bot.username = "testbot"

    mock_state_manager = MagicMock()

    with patch('handlers.shutdown.hasPermission', return_value=True):
        with patch('handlers.shutdown.getStateManager', return_value=mock_state_manager):
            with patch('handlers.shutdown.logSystemEvent', new_callable=AsyncMock):
                with pytest.raises(ApplicationHandlerStop):
                    await _mentionDispatch(mockUpdate, mockContext)

    mock_state_manager.requestShutdown.assert_called_once()


@pytest.mark.asyncio
async def test_mention_dispatch_case_insensitive():
    """大小写不敏感"""
    mockUser = MagicMock()
    mockUser.id = 123
    mockUser.full_name = "Charlie"

    mockMessage = MagicMock()
    mockMessage.text = "@TestBot 关机"
    mockMessage.reply_text = AsyncMock()

    mockUpdate = MagicMock()
    mockUpdate.message = mockMessage
    mockUpdate.effective_user = mockUser

    mockContext = MagicMock()
    mockContext.bot.username = "testbot"

    mock_state_manager = MagicMock()

    with patch('handlers.shutdown.hasPermission', return_value=True):
        with patch('handlers.shutdown.getStateManager', return_value=mock_state_manager):
            with patch('handlers.shutdown.logSystemEvent', new_callable=AsyncMock):
                with pytest.raises(ApplicationHandlerStop):
                    await _mentionDispatch(mockUpdate, mockContext)

    mock_state_manager.requestShutdown.assert_called_once()


@pytest.mark.asyncio
async def test_mention_dispatch_no_mention():
    """无 @bot 时不处理"""
    mockMessage = MagicMock()
    mockMessage.text = "关机"

    mockUpdate = MagicMock()
    mockUpdate.message = mockMessage

    mockContext = MagicMock()
    mockContext.bot.username = "testbot"

    mock_state_manager = MagicMock()

    with patch('handlers.shutdown.getStateManager', return_value=mock_state_manager):
        # 不应抛出 ApplicationHandlerStop
        await _mentionDispatch(mockUpdate, mockContext)

    mock_state_manager.requestShutdown.assert_not_called()


@pytest.mark.asyncio
async def test_mention_dispatch_keyword_not_match():
    """关键词不匹配时不处理"""
    mockMessage = MagicMock()
    mockMessage.text = "@testbot 你好"

    mockUpdate = MagicMock()
    mockUpdate.message = mockMessage

    mockContext = MagicMock()
    mockContext.bot.username = "testbot"

    mock_state_manager = MagicMock()

    with patch('handlers.shutdown.getStateManager', return_value=mock_state_manager):
        # 不应抛出 ApplicationHandlerStop
        await _mentionDispatch(mockUpdate, mockContext)

    mock_state_manager.requestShutdown.assert_not_called()


@pytest.mark.asyncio
async def test_mention_dispatch_no_message():
    """无 message 时不处理"""
    mockUpdate = MagicMock()
    mockUpdate.message = None

    mockContext = MagicMock()

    # 不应抛出异常
    await _mentionDispatch(mockUpdate, mockContext)


@pytest.mark.asyncio
async def test_mention_dispatch_no_text():
    """无 message.text 时不处理"""
    mockMessage = MagicMock()
    mockMessage.text = None

    mockUpdate = MagicMock()
    mockUpdate.message = mockMessage

    mockContext = MagicMock()

    # 不应抛出异常
    await _mentionDispatch(mockUpdate, mockContext)


@pytest.mark.asyncio
async def test_mention_dispatch_restart_keyword():
    """测试重启关键词"""
    mockUser = MagicMock()
    mockUser.id = 123
    mockUser.full_name = "Dave"

    mockMessage = MagicMock()
    mockMessage.text = "@testbot 重启"
    mockMessage.reply_text = AsyncMock()

    mockUpdate = MagicMock()
    mockUpdate.message = mockMessage
    mockUpdate.effective_user = mockUser

    mockContext = MagicMock()
    mockContext.bot.username = "testbot"

    mock_state_manager = MagicMock()

    with patch('handlers.shutdown.hasPermission', return_value=True):
        with patch('handlers.shutdown.getStateManager', return_value=mock_state_manager):
            with patch('handlers.shutdown.logSystemEvent', new_callable=AsyncMock):
                with pytest.raises(ApplicationHandlerStop):
                    await _mentionDispatch(mockUpdate, mockContext)

    mock_state_manager.requestRestart.assert_called_once()


@pytest.mark.asyncio
async def test_mention_dispatch_status_keyword():
    """测试状态关键词"""
    mockUser = MagicMock()
    mockUser.id = 123

    mockMessage = MagicMock()
    mockMessage.text = "@testbot 运行状态"
    mockMessage.reply_text = AsyncMock()

    mockUpdate = MagicMock()
    mockUpdate.message = mockMessage
    mockUpdate.effective_user = mockUser

    mockContext = MagicMock()
    mockContext.bot.username = "testbot"

    mock_process = MagicMock()
    mock_process.memory_info.return_value.rss = 50 * 1024 * 1024
    mock_process.cpu_percent.return_value = 2.0

    with patch('handlers.shutdown.hasPermission', return_value=True):
        with patch('handlers.shutdown.psutil.Process', return_value=mock_process):
            with patch('handlers.shutdown.asyncio.to_thread', new_callable=AsyncMock, return_value=2.0):
                with patch('handlers.shutdown._START_TIME', 1000.0):
                    with patch('handlers.shutdown.time.time', return_value=1100.0):
                        with pytest.raises(ApplicationHandlerStop):
                            await _mentionDispatch(mockUpdate, mockContext)

    mockMessage.reply_text.assert_called_once()


# ============================================================================
# register() 测试
# ============================================================================

def test_register_returns_valid_dict():
    """register() 返回正确的 handler 字典"""
    result = register()

    assert "handlers" in result
    assert "name" in result
    assert "description" in result
    assert "auth" in result
    assert result["auth"] is False
    assert len(result["handlers"]) == 4  # 3个CommandHandler + 1个MessageHandler