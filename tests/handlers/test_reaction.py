"""
tests/handlers/test_reaction.py

测试 handlers/reaction.py
"""

import pytest
from unittest.mock import patch, AsyncMock, MagicMock

from handlers.reaction import handleReaction, register


# ============================================================================
# handleReaction() 测试
# ============================================================================

@pytest.mark.asyncio
async def test_handle_reaction_no_reaction():
    """无 reaction 对象时直接返回"""
    mockUpdate = MagicMock()
    mockUpdate.message_reaction = None

    mockContext = MagicMock()

    with patch('handlers.reaction.saveMessage', new_callable=AsyncMock) as mock_save:
        await handleReaction(mockUpdate, mockContext)

    mock_save.assert_not_called()


@pytest.mark.asyncio
async def test_handle_reaction_no_user():
    """无 user 时直接返回"""
    mock_reaction = MagicMock()
    mock_reaction.user = None
    mock_reaction.chat.id = 123

    mockUpdate = MagicMock()
    mockUpdate.message_reaction = mock_reaction

    mockContext = MagicMock()

    with patch('handlers.reaction.saveMessage', new_callable=AsyncMock) as mock_save:
        await handleReaction(mockUpdate, mockContext)

    mock_save.assert_not_called()


@pytest.mark.asyncio
async def test_handle_reaction_added_emoji():
    """添加新 emoji 保存到 chatHistory"""
    mockUser = MagicMock()
    mockUser.username = "alice"
    mockUser.first_name = "Alice"
    mockUser.id = 456

    new_emoji = MagicMock()
    new_emoji.emoji = "❤"

    mock_reaction = MagicMock()
    mock_reaction.user = mockUser
    mock_reaction.chat.id = 123
    mock_reaction.old_reaction = []
    mock_reaction.new_reaction = [new_emoji]

    mockUpdate = MagicMock()
    mockUpdate.message_reaction = mock_reaction

    mockContext = MagicMock()

    with patch('handlers.reaction.saveMessage', new_callable=AsyncMock) as mock_save:
        await handleReaction(mockUpdate, mockContext)

    mock_save.assert_called_once_with("123", "reaction", "alice", "回应了 ❤")


@pytest.mark.asyncio
async def test_handle_reaction_removed_only():
    """只有移除 emoji，不保存"""
    mockUser = MagicMock()
    mockUser.username = "alice"

    old_emoji = MagicMock()
    old_emoji.emoji = "❤"

    mock_reaction = MagicMock()
    mock_reaction.user = mockUser
    mock_reaction.chat.id = 123
    mock_reaction.old_reaction = [old_emoji]
    mock_reaction.new_reaction = []

    mockUpdate = MagicMock()
    mockUpdate.message_reaction = mock_reaction

    mockContext = MagicMock()

    with patch('handlers.reaction.saveMessage', new_callable=AsyncMock) as mock_save:
        await handleReaction(mockUpdate, mockContext)

    mock_save.assert_not_called()


@pytest.mark.asyncio
async def test_handle_reaction_add_and_remove():
    """同时添加和移除 emoji（只保存新增）"""
    mockUser = MagicMock()
    mockUser.username = "bob"

    old_emoji = MagicMock()
    old_emoji.emoji = "❤"

    new_emoji_1 = MagicMock()
    new_emoji_1.emoji = "👍"
    new_emoji_2 = MagicMock()
    new_emoji_2.emoji = "❤"  # 已存在

    mock_reaction = MagicMock()
    mock_reaction.user = mockUser
    mock_reaction.chat.id = 123
    mock_reaction.old_reaction = [old_emoji]
    mock_reaction.new_reaction = [new_emoji_1, new_emoji_2]

    mockUpdate = MagicMock()
    mockUpdate.message_reaction = mock_reaction

    mockContext = MagicMock()

    with patch('handlers.reaction.saveMessage', new_callable=AsyncMock) as mock_save:
        await handleReaction(mockUpdate, mockContext)

    # 只应保存 👍（❤ 已存在，不算新增）
    mock_save.assert_called_once_with("123", "reaction", "bob", "回应了 👍")


@pytest.mark.asyncio
async def test_handle_reaction_multiple_new_emojis():
    """多个新增 emoji 每个都保存"""
    mockUser = MagicMock()
    mockUser.username = "charlie"

    emoji_1 = MagicMock()
    emoji_1.emoji = "❤"
    emoji_2 = MagicMock()
    emoji_2.emoji = "😂"

    mock_reaction = MagicMock()
    mock_reaction.user = mockUser
    mock_reaction.chat.id = 789
    mock_reaction.old_reaction = []
    mock_reaction.new_reaction = [emoji_1, emoji_2]

    mockUpdate = MagicMock()
    mockUpdate.message_reaction = mock_reaction

    mockContext = MagicMock()

    with patch('handlers.reaction.saveMessage', new_callable=AsyncMock) as mock_save:
        await handleReaction(mockUpdate, mockContext)

    assert mock_save.call_count == 2
    # 由于集合无序，检查两种 emoji 都被保存了
    calls = [c[0][3] for c in mock_save.call_args_list]
    assert "回应了 ❤" in calls
    assert "回应了 😂" in calls


@pytest.mark.asyncio
async def test_handle_reaction_username_priority_username():
    """username 优先级：有 username 用 username"""
    mockUser = MagicMock()
    mockUser.username = "alice"
    mockUser.first_name = "Alice"
    mockUser.id = 456

    new_emoji = MagicMock()
    new_emoji.emoji = "👍"

    mock_reaction = MagicMock()
    mock_reaction.user = mockUser
    mock_reaction.chat.id = 123
    mock_reaction.old_reaction = []
    mock_reaction.new_reaction = [new_emoji]

    mockUpdate = MagicMock()
    mockUpdate.message_reaction = mock_reaction

    with patch('handlers.reaction.saveMessage', new_callable=AsyncMock) as mock_save:
        await handleReaction(mockUpdate, MagicMock())

    mock_save.assert_called_once_with("123", "reaction", "alice", "回应了 👍")


@pytest.mark.asyncio
async def test_handle_reaction_username_priority_first_name():
    """username 优先级：无 username 用 first_name"""
    mockUser = MagicMock()
    mockUser.username = None
    mockUser.first_name = "Alice"
    mockUser.id = 456

    new_emoji = MagicMock()
    new_emoji.emoji = "👍"

    mock_reaction = MagicMock()
    mock_reaction.user = mockUser
    mock_reaction.chat.id = 123
    mock_reaction.old_reaction = []
    mock_reaction.new_reaction = [new_emoji]

    mockUpdate = MagicMock()
    mockUpdate.message_reaction = mock_reaction

    with patch('handlers.reaction.saveMessage', new_callable=AsyncMock) as mock_save:
        await handleReaction(mockUpdate, MagicMock())

    mock_save.assert_called_once_with("123", "reaction", "Alice", "回应了 👍")


@pytest.mark.asyncio
async def test_handle_reaction_username_priority_id():
    """username 优先级：无 username 和 first_name 用 id"""
    mockUser = MagicMock()
    mockUser.username = None
    mockUser.first_name = None
    mockUser.id = 456

    new_emoji = MagicMock()
    new_emoji.emoji = "👍"

    mock_reaction = MagicMock()
    mock_reaction.user = mockUser
    mock_reaction.chat.id = 123
    mock_reaction.old_reaction = []
    mock_reaction.new_reaction = [new_emoji]

    mockUpdate = MagicMock()
    mockUpdate.message_reaction = mock_reaction

    with patch('handlers.reaction.saveMessage', new_callable=AsyncMock) as mock_save:
        await handleReaction(mockUpdate, MagicMock())

    mock_save.assert_called_once_with("123", "reaction", "456", "回应了 👍")


@pytest.mark.asyncio
async def test_handle_reaction_old_reaction_none():
    """old_reaction 为 None"""
    mockUser = MagicMock()
    mockUser.username = "alice"

    new_emoji = MagicMock()
    new_emoji.emoji = "❤"

    mock_reaction = MagicMock()
    mock_reaction.user = mockUser
    mock_reaction.chat.id = 123
    mock_reaction.old_reaction = None
    mock_reaction.new_reaction = [new_emoji]

    mockUpdate = MagicMock()
    mockUpdate.message_reaction = mock_reaction

    with patch('handlers.reaction.saveMessage', new_callable=AsyncMock) as mock_save:
        await handleReaction(mockUpdate, MagicMock())

    mock_save.assert_called_once()


@pytest.mark.asyncio
async def test_handle_reaction_new_reaction_none():
    """new_reaction 为 None"""
    mockUser = MagicMock()
    mockUser.username = "alice"

    mock_reaction = MagicMock()
    mock_reaction.user = mockUser
    mock_reaction.chat.id = 123
    mock_reaction.old_reaction = None
    mock_reaction.new_reaction = None

    mockUpdate = MagicMock()
    mockUpdate.message_reaction = mock_reaction

    with patch('handlers.reaction.saveMessage', new_callable=AsyncMock) as mock_save:
        await handleReaction(mockUpdate, MagicMock())

    # new_reaction 为 None → newEmojis 为空集 → addedEmojis 为空 → 不保存
    mock_save.assert_not_called()


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
    assert len(result["handlers"]) == 1