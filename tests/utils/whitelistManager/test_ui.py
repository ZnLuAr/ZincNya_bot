"""
tests/utils/whitelistManager/test_ui.py

测试 utils/whitelistManager/ui.py
"""

import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from telegram.error import Forbidden, BadRequest

from utils.whitelistManager.ui import (
    checkChatAvailable,
    collectWhitelistViewModel,
)


# ============================================================================
# checkChatAvailable() 测试
# ============================================================================

@pytest.mark.asyncio
async def test_check_chat_available_success():
    """用户可用"""
    mock_bot = MagicMock()
    mock_bot.send_chat_action = AsyncMock()

    result = await checkChatAvailable(mock_bot, "123")

    assert result is True
    mock_bot.send_chat_action.assert_called_once_with(chat_id="123", action="typing")


@pytest.mark.asyncio
async def test_check_chat_available_forbidden():
    """用户屏蔽了 Bot"""
    mock_bot = MagicMock()
    mock_bot.send_chat_action = AsyncMock(side_effect=Forbidden("User blocked the bot"))

    result = await checkChatAvailable(mock_bot, "123")

    assert isinstance(result, tuple)
    assert result[0] == "Forbidden"
    assert isinstance(result[1], Forbidden)


@pytest.mark.asyncio
async def test_check_chat_available_not_found():
    """用户不存在"""
    mock_bot = MagicMock()
    mock_bot.send_chat_action = AsyncMock(side_effect=BadRequest("Chat not found"))

    result = await checkChatAvailable(mock_bot, "123")

    assert isinstance(result, tuple)
    assert result[0] == "NotFound"
    assert isinstance(result[1], BadRequest)


@pytest.mark.asyncio
async def test_check_chat_available_error():
    """其他错误"""
    mock_bot = MagicMock()
    mock_bot.send_chat_action = AsyncMock(side_effect=Exception("Network error"))

    result = await checkChatAvailable(mock_bot, "123")

    assert isinstance(result, tuple)
    assert result[0] == "Error"
    assert isinstance(result[1], Exception)


# ============================================================================
# collectWhitelistViewModel() 测试
# ============================================================================

@pytest.mark.asyncio
async def test_collect_whitelist_view_model_empty():
    """空白名单"""
    whitelist_data = {
        "allowed": {},
        "suspended": {}
    }

    mock_bot = MagicMock()

    with patch('utils.whitelistManager.ui.loadWhitelistFile', return_value=whitelist_data):
        entries, meta = await collectWhitelistViewModel(mock_bot)

        assert len(entries) == 0
        assert meta['selected'] == 0
        assert meta['count'] == 0


@pytest.mark.asyncio
async def test_collect_whitelist_view_model_with_add_row():
    """包含 (+) 行"""
    whitelist_data = {
        "allowed": {},
        "suspended": {}
    }

    mock_bot = MagicMock()

    with patch('utils.whitelistManager.ui.loadWhitelistFile', return_value=whitelist_data):
        entries, meta = await collectWhitelistViewModel(mock_bot, includeAddRow=True)

        assert len(entries) == 1
        assert entries[0]['uid'] == "(+)"
        assert entries[0]['displayStatus'] == "添加新用户"
        assert entries[0]['colour'] == "cyan"
        assert entries[0]['isAddRow'] is True


@pytest.mark.asyncio
async def test_collect_whitelist_view_model_allowed_user():
    """Allowed 用户"""
    whitelist_data = {
        "allowed": {"123": {"comment": "test user"}},
        "suspended": {}
    }

    mock_bot = MagicMock()

    with patch('utils.whitelistManager.ui.loadWhitelistFile', return_value=whitelist_data):
        with patch('utils.whitelistManager.ui.checkChatAvailable', new_callable=AsyncMock) as mock_check:
            mock_check.return_value = True

            entries, meta = await collectWhitelistViewModel(mock_bot)

            assert len(entries) == 1
            assert entries[0]['uid'] == "123"
            assert entries[0]['listStatus'] == "Allowed"
            assert entries[0]['displayStatus'] == "Allowed"
            assert entries[0]['colour'] == "wheat1"
            assert entries[0]['comment'] == "test user"
            assert entries[0]['available'] is True


@pytest.mark.asyncio
async def test_collect_whitelist_view_model_suspended_user():
    """Suspended 用户"""
    whitelist_data = {
        "allowed": {},
        "suspended": {"456": {"comment": "suspended user"}}
    }

    mock_bot = MagicMock()

    with patch('utils.whitelistManager.ui.loadWhitelistFile', return_value=whitelist_data):
        with patch('utils.whitelistManager.ui.checkChatAvailable', new_callable=AsyncMock) as mock_check:
            mock_check.return_value = True

            entries, meta = await collectWhitelistViewModel(mock_bot)

            assert len(entries) == 1
            assert entries[0]['uid'] == "456"
            assert entries[0]['listStatus'] == "Suspended"
            assert entries[0]['displayStatus'] == "Suspended"
            assert entries[0]['colour'] == "wheat1"  # available is True
            assert entries[0]['comment'] == "suspended user"


@pytest.mark.asyncio
async def test_collect_whitelist_view_model_forbidden_user():
    """Forbidden 用户（屏蔽了 Bot）"""
    whitelist_data = {
        "allowed": {"123": {"comment": ""}},
        "suspended": {}
    }

    mock_bot = MagicMock()

    with patch('utils.whitelistManager.ui.loadWhitelistFile', return_value=whitelist_data):
        with patch('utils.whitelistManager.ui.checkChatAvailable', new_callable=AsyncMock) as mock_check:
            mock_check.return_value = ("Forbidden", Forbidden("Blocked"))

            entries, meta = await collectWhitelistViewModel(mock_bot)

            assert len(entries) == 1
            assert entries[0]['displayStatus'] == "Forbidden"
            assert entries[0]['colour'] == "grey70"
            assert entries[0]['available'] is False


@pytest.mark.asyncio
async def test_collect_whitelist_view_model_not_found_user():
    """Not Found 用户"""
    whitelist_data = {
        "allowed": {"123": {"comment": ""}},
        "suspended": {}
    }

    mock_bot = MagicMock()

    with patch('utils.whitelistManager.ui.loadWhitelistFile', return_value=whitelist_data):
        with patch('utils.whitelistManager.ui.checkChatAvailable', new_callable=AsyncMock) as mock_check:
            mock_check.return_value = ("NotFound", BadRequest("Not found"))

            entries, meta = await collectWhitelistViewModel(mock_bot)

            assert len(entries) == 1
            assert entries[0]['displayStatus'] == "Not Found"
            assert entries[0]['colour'] == "grey70"


@pytest.mark.asyncio
async def test_collect_whitelist_view_model_error_user():
    """Error 用户"""
    whitelist_data = {
        "allowed": {"123": {"comment": ""}},
        "suspended": {}
    }

    mock_bot = MagicMock()

    with patch('utils.whitelistManager.ui.loadWhitelistFile', return_value=whitelist_data):
        with patch('utils.whitelistManager.ui.checkChatAvailable', new_callable=AsyncMock) as mock_check:
            mock_check.return_value = ("Error", Exception("Network error"))

            entries, meta = await collectWhitelistViewModel(mock_bot)

            assert len(entries) == 1
            assert entries[0]['displayStatus'] == "Error"
            assert entries[0]['colour'] == "grey70"


@pytest.mark.asyncio
async def test_collect_whitelist_view_model_concurrent_check():
    """并发检查用户可用性"""
    whitelist_data = {
        "allowed": {
            "123": {"comment": "user1"},
            "456": {"comment": "user2"},
            "789": {"comment": "user3"},
        },
        "suspended": {}
    }

    mock_bot = MagicMock()

    with patch('utils.whitelistManager.ui.loadWhitelistFile', return_value=whitelist_data):
        with patch('utils.whitelistManager.ui.checkChatAvailable', new_callable=AsyncMock) as mock_check:
            # 返回不同的结果
            mock_check.side_effect = [
                True,
                ("Forbidden", Forbidden("Blocked")),
                ("NotFound", BadRequest("Not found"))
            ]

            entries, meta = await collectWhitelistViewModel(mock_bot)

            # 应该并发调用 3 次
            assert mock_check.call_count == 3
            assert len(entries) == 3

            # 验证状态映射
            assert entries[0]['displayStatus'] == "Allowed"
            assert entries[1]['displayStatus'] == "Forbidden"
            assert entries[2]['displayStatus'] == "Not Found"


@pytest.mark.asyncio
async def test_collect_whitelist_view_model_selected_index():
    """selectedIndex 边界处理"""
    whitelist_data = {
        "allowed": {
            "123": {"comment": ""},
            "456": {"comment": ""},
        },
        "suspended": {}
    }

    mock_bot = MagicMock()

    with patch('utils.whitelistManager.ui.loadWhitelistFile', return_value=whitelist_data):
        with patch('utils.whitelistManager.ui.checkChatAvailable', new_callable=AsyncMock) as mock_check:
            mock_check.return_value = True

            # 正常索引
            entries, meta = await collectWhitelistViewModel(mock_bot, selectedIndex=1)
            assert meta['selected'] == 1

            # 超出范围（钳制到最大值）
            entries, meta = await collectWhitelistViewModel(mock_bot, selectedIndex=10)
            assert meta['selected'] == 1  # 最大索引

            # 负数（钳制到 0）
            entries, meta = await collectWhitelistViewModel(mock_bot, selectedIndex=-5)
            assert meta['selected'] == 0


@pytest.mark.asyncio
async def test_collect_whitelist_view_model_mixed():
    """混合 allowed 和 suspended 用户"""
    whitelist_data = {
        "allowed": {
            "123": {"comment": "allowed user"},
            "456": {"comment": "another allowed"},
        },
        "suspended": {
            "789": {"comment": "suspended user"},
        }
    }

    mock_bot = MagicMock()

    with patch('utils.whitelistManager.ui.loadWhitelistFile', return_value=whitelist_data):
        with patch('utils.whitelistManager.ui.checkChatAvailable', new_callable=AsyncMock) as mock_check:
            mock_check.return_value = True

            entries, meta = await collectWhitelistViewModel(mock_bot)

            assert len(entries) == 3
            assert meta['count'] == 3

            # 验证 listStatus
            list_statuses = [e['listStatus'] for e in entries]
            assert list_statuses.count("Allowed") == 2
            assert list_statuses.count("Suspended") == 1