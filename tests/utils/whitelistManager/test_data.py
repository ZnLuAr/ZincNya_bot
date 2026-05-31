"""
tests/utils/whitelistManager/test_data.py

测试 utils/whitelistManager/data.py
"""

import pytest
import time
from unittest.mock import patch, AsyncMock, MagicMock

from utils.whitelistManager.data import (
    whetherAuthorizedUser,
    userOperation,
    handleStart,
    _lastNotifyTime,
    _NOTIFY_COOLDOWN,
    _MAX_NOTIFY_CACHE,
)


# ============================================================================
# Fixture — 清理全局状态
# ============================================================================

@pytest.fixture(autouse=True)
def cleanNotifyCache():
    """每个测试前后清理通知缓存，避免全局状态污染"""
    _lastNotifyTime.clear()
    yield
    _lastNotifyTime.clear()


# ============================================================================
# whetherAuthorizedUser() 测试
# ============================================================================

def test_whether_authorized_user_allowed():
    """用户在 allowed 且不在 suspended 返回 True"""
    whitelist_data = {
        "allowed": {"123": {"comment": ""}},
        "suspended": {}
    }

    with patch('utils.whitelistManager.data.loadWhitelistFile', return_value=whitelist_data):
        assert whetherAuthorizedUser("123") is True
        assert whetherAuthorizedUser(123) is True  # int 也应该工作


def test_whether_authorized_user_not_in_allowed():
    """用户不在 allowed 返回 False"""
    whitelist_data = {
        "allowed": {},
        "suspended": {}
    }

    with patch('utils.whitelistManager.data.loadWhitelistFile', return_value=whitelist_data):
        assert whetherAuthorizedUser("999") is False


def test_whether_authorized_user_suspended():
    """用户在 suspended 返回 False"""
    whitelist_data = {
        "allowed": {"123": {"comment": ""}},
        "suspended": {"123": {"comment": ""}}
    }

    with patch('utils.whitelistManager.data.loadWhitelistFile', return_value=whitelist_data):
        assert whetherAuthorizedUser("123") is False


# ============================================================================
# userOperation() 测试
# ============================================================================

def test_user_operation_add_user():
    """添加用户"""
    whitelist_data = {
        "allowed": {},
        "suspended": {}
    }

    with patch('utils.whitelistManager.data.loadWhitelistFile', return_value=whitelist_data):
        with patch('utils.whitelistManager.data.saveWhitelistFile') as mock_save:
            result = userOperation("addUser", "123")
            assert result is True

            # 验证保存的数据
            saved_data = mock_save.call_args[0][0]
            assert "123" in saved_data["allowed"]
            assert saved_data["allowed"]["123"]["comment"] == ""


def test_user_operation_add_user_already_exists():
    """添加已存在的用户"""
    whitelist_data = {
        "allowed": {"123": {"comment": ""}},
        "suspended": {}
    }

    with patch('utils.whitelistManager.data.loadWhitelistFile', return_value=whitelist_data):
        result = userOperation("addUser", "123")
        assert result is False


def test_user_operation_delete_user():
    """删除用户"""
    whitelist_data = {
        "allowed": {"123": {"comment": "test"}},
        "suspended": {}
    }

    with patch('utils.whitelistManager.data.loadWhitelistFile', return_value=whitelist_data):
        with patch('utils.whitelistManager.data.saveWhitelistFile') as mock_save:
            result = userOperation("deleteUser", "123")
            assert result is True

            saved_data = mock_save.call_args[0][0]
            assert "123" not in saved_data["allowed"]


def test_user_operation_delete_user_not_exists():
    """删除不存在的用户"""
    whitelist_data = {
        "allowed": {},
        "suspended": {}
    }

    with patch('utils.whitelistManager.data.loadWhitelistFile', return_value=whitelist_data):
        result = userOperation("deleteUser", "999")
        assert result is False


def test_user_operation_suspend_user():
    """暂停用户"""
    whitelist_data = {
        "allowed": {"123": {"comment": "test"}},
        "suspended": {}
    }

    with patch('utils.whitelistManager.data.loadWhitelistFile', return_value=whitelist_data):
        with patch('utils.whitelistManager.data.saveWhitelistFile') as mock_save:
            result = userOperation("suspendUser", "123")
            assert result is True

            saved_data = mock_save.call_args[0][0]
            assert "123" not in saved_data["allowed"]
            assert "123" in saved_data["suspended"]
            assert saved_data["suspended"]["123"]["comment"] == "test"


def test_user_operation_suspend_user_already_suspended():
    """暂停已暂停的用户"""
    whitelist_data = {
        "allowed": {},
        "suspended": {"123": {"comment": ""}}
    }

    with patch('utils.whitelistManager.data.loadWhitelistFile', return_value=whitelist_data):
        result = userOperation("suspendUser", "123")
        assert result is False


def test_user_operation_unsuspend_user():
    """恢复用户"""
    whitelist_data = {
        "allowed": {},
        "suspended": {"123": {"comment": "test"}}
    }

    with patch('utils.whitelistManager.data.loadWhitelistFile', return_value=whitelist_data):
        with patch('utils.whitelistManager.data.saveWhitelistFile') as mock_save:
            result = userOperation("unsuspendUser", "123")
            assert result is True

            saved_data = mock_save.call_args[0][0]
            assert "123" in saved_data["allowed"]
            assert "123" not in saved_data["suspended"]
            assert saved_data["allowed"]["123"]["comment"] == "test"


def test_user_operation_unsuspend_user_not_suspended():
    """恢复未暂停的用户"""
    whitelist_data = {
        "allowed": {"123": {"comment": ""}},
        "suspended": {}
    }

    with patch('utils.whitelistManager.data.loadWhitelistFile', return_value=whitelist_data):
        result = userOperation("unsuspendUser", "123")
        assert result is False


def test_user_operation_list_users():
    """列出所有用户"""
    whitelist_data = {
        "allowed": {"123": {"comment": "user1"}},
        "suspended": {"456": {"comment": "user2"}}
    }

    with patch('utils.whitelistManager.data.loadWhitelistFile', return_value=whitelist_data):
        result = userOperation("listUsers")
        assert result == whitelist_data


def test_user_operation_set_comment_allowed():
    """设置 allowed 用户的备注"""
    whitelist_data = {
        "allowed": {"123": {"comment": "old"}},
        "suspended": {}
    }

    with patch('utils.whitelistManager.data.loadWhitelistFile', return_value=whitelist_data):
        with patch('utils.whitelistManager.data.saveWhitelistFile') as mock_save:
            result = userOperation("setComment", "123", "new comment")
            assert result is True

            saved_data = mock_save.call_args[0][0]
            assert saved_data["allowed"]["123"]["comment"] == "new comment"


def test_user_operation_set_comment_suspended():
    """设置 suspended 用户的备注"""
    whitelist_data = {
        "allowed": {},
        "suspended": {"123": {"comment": "old"}}
    }

    with patch('utils.whitelistManager.data.loadWhitelistFile', return_value=whitelist_data):
        with patch('utils.whitelistManager.data.saveWhitelistFile') as mock_save:
            result = userOperation("setComment", "123", "new comment")
            assert result is True

            saved_data = mock_save.call_args[0][0]
            assert saved_data["suspended"]["123"]["comment"] == "new comment"


def test_user_operation_set_comment_not_exists():
    """设置不存在用户的备注"""
    whitelist_data = {
        "allowed": {},
        "suspended": {}
    }

    with patch('utils.whitelistManager.data.loadWhitelistFile', return_value=whitelist_data):
        result = userOperation("setComment", "999", "comment")
        assert result is False


def test_user_operation_unknown():
    """未知操作抛出异常"""
    whitelist_data = {
        "allowed": {},
        "suspended": {}
    }

    with patch('utils.whitelistManager.data.loadWhitelistFile', return_value=whitelist_data):
        with pytest.raises(ValueError, match="未知的操作类型"):
            userOperation("unknownOperation", "123")


# ============================================================================
# handleStart() 测试
# ============================================================================

@pytest.mark.asyncio
async def test_handle_start_authorized():
    """已授权用户"""
    mockUpdate = MagicMock()
    mockUpdate.effective_user.id = 123
    mockUpdate.effective_user.username = "testuser"
    mockUpdate.effective_user.first_name = "Test"
    mockUpdate.effective_user.last_name = "User"
    mockUpdate.message.reply_text = AsyncMock()

    mockContext = MagicMock()

    with patch('utils.whitelistManager.data.whetherAuthorizedUser', return_value=True):
        with patch('utils.whitelistManager.data.logAction', new_callable=AsyncMock):
            await handleStart(mockUpdate, mockContext)

            # 应该回复欢迎消息
            mockUpdate.message.reply_text.assert_called_once_with("欢迎回来喵——")


@pytest.mark.asyncio
async def test_handle_start_unauthorized_first_time():
    """未授权用户（首次触发）"""
    mockUpdate = MagicMock()
    mockUpdate.effective_user.id = 999
    mockUpdate.effective_user.username = "stranger"
    mockUpdate.effective_user.first_name = "Stranger"
    mockUpdate.effective_user.last_name = None

    mockContext = MagicMock()
    mockContext.bot.send_message = AsyncMock()

    with patch('utils.whitelistManager.data.whetherAuthorizedUser', return_value=False):
        with patch('utils.whitelistManager.data.getOperatorsWithPermission', return_value=["123"]):
            with patch('utils.whitelistManager.data.logAction', new_callable=AsyncMock):
                await handleStart(mockUpdate, mockContext)

                # 应该通知 operator
                mockContext.bot.send_message.assert_called_once()
                call_args = mockContext.bot.send_message.call_args
                assert call_args[1]['chat_id'] == 123
                assert "有不认识的人碰到锌酱了喵" in call_args[1]['text']
                assert "999" in call_args[1]['text']


@pytest.mark.asyncio
async def test_handle_start_unauthorized_cooldown():
    """未授权用户（冷却期内）"""
    # 设置最近通知时间（fixture 已清理，这里设置测试数据）
    _lastNotifyTime["999"] = time.monotonic()

    mockUpdate = MagicMock()
    mockUpdate.effective_user.id = 999
    mockUpdate.effective_user.username = "stranger"
    mockUpdate.effective_user.first_name = "Stranger"
    mockUpdate.effective_user.last_name = None

    mockContext = MagicMock()
    mockContext.bot.send_message = AsyncMock()

    with patch('utils.whitelistManager.data.whetherAuthorizedUser', return_value=False):
        with patch('utils.whitelistManager.data.getOperatorsWithPermission', return_value=["123"]):
            with patch('utils.whitelistManager.data.logAction', new_callable=AsyncMock):
                await handleStart(mockUpdate, mockContext)

                # 不应该通知 operator（冷却期内）
                mockContext.bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_handle_start_unauthorized_expired_cleanup():
    """未授权用户（清理过期条目）"""
    # 设置过期的通知时间（fixture 已清理，这里设置测试数据）
    _lastNotifyTime["888"] = time.monotonic() - _NOTIFY_COOLDOWN - 1

    mockUpdate = MagicMock()
    mockUpdate.effective_user.id = 999
    mockUpdate.effective_user.username = "stranger"
    mockUpdate.effective_user.first_name = "Stranger"
    mockUpdate.effective_user.last_name = None

    mockContext = MagicMock()
    mockContext.bot.send_message = AsyncMock()

    with patch('utils.whitelistManager.data.whetherAuthorizedUser', return_value=False):
        with patch('utils.whitelistManager.data.getOperatorsWithPermission', return_value=["123"]):
            with patch('utils.whitelistManager.data.logAction', new_callable=AsyncMock):
                await handleStart(mockUpdate, mockContext)

                # 应该清理过期条目
                assert "888" not in _lastNotifyTime
                # 应该通知 operator
                mockContext.bot.send_message.assert_called_once()


@pytest.mark.asyncio
async def test_handle_start_unauthorized_cache_limit():
    """未授权用户（缓存上限兜底）"""
    # 填满缓存（fixture 已清理，这里设置测试数据）
    for i in range(_MAX_NOTIFY_CACHE):
        _lastNotifyTime[str(i)] = time.monotonic()

    mockUpdate = MagicMock()
    mockUpdate.effective_user.id = 999999
    mockUpdate.effective_user.username = "stranger"
    mockUpdate.effective_user.first_name = "Stranger"
    mockUpdate.effective_user.last_name = None

    mockContext = MagicMock()
    mockContext.bot.send_message = AsyncMock()

    with patch('utils.whitelistManager.data.whetherAuthorizedUser', return_value=False):
        with patch('utils.whitelistManager.data.getOperatorsWithPermission', return_value=["123"]):
            with patch('utils.whitelistManager.data.logAction', new_callable=AsyncMock):
                await handleStart(mockUpdate, mockContext)

                # 缓存应该被限制在上限
                assert len(_lastNotifyTime) <= _MAX_NOTIFY_CACHE


@pytest.mark.asyncio
async def test_handle_start_unauthorized_send_message_error():
    """未授权用户（发送消息失败）"""
    mockUpdate = MagicMock()
    mockUpdate.effective_user.id = 999
    mockUpdate.effective_user.username = "stranger"
    mockUpdate.effective_user.first_name = "Stranger"
    mockUpdate.effective_user.last_name = None

    mockContext = MagicMock()
    mockContext.bot.send_message = AsyncMock(side_effect=Exception("Network error"))

    with patch('utils.whitelistManager.data.whetherAuthorizedUser', return_value=False):
        with patch('utils.whitelistManager.data.getOperatorsWithPermission', return_value=["123"]):
            with patch('utils.whitelistManager.data.logAction', new_callable=AsyncMock):
                # 不应该抛出异常（错误被捕获）
                await handleStart(mockUpdate, mockContext)