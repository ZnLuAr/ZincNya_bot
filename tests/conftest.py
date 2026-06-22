"""
tests/conftest.py

全局 pytest 配置和 fixture 定义
"""

import sys
import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

import pytest


# ============================================================================
# 项目路径配置
# ============================================================================

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ============================================================================
# 测试常量
# ============================================================================

# 数据库相关
DB_BUSY_TIMEOUT = 5000  # SQLite busy_timeout（毫秒）

# Telegram Mock 数据（仅测试用占位 ID，与真实 Telegram user/chat 无关）
TEST_USER_ID = 123456789
TEST_CHAT_ID = 987654321
TEST_MESSAGE_ID = 1
TEST_UPDATE_ID = 1

# 时间相关
TEST_FROZEN_TIME = "2026-05-26 12:00:00"


# ============================================================================
# pytest 配置钩子
# ============================================================================

def pytest_collection_modifyitems(config, items):
    """自动为 integration/ 目录添加 integration marker"""
    for item in items:
        if "integration" in str(item.fspath):
            item.add_marker(pytest.mark.integration)


# ============================================================================
# 数据库 Fixture
# ============================================================================

@pytest.fixture
def inMemoryDb():
    """提供 in-memory SQLite 连接"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.fixture
def tempDbPath(tmp_path):
    """提供临时数据库文件路径"""
    db_path = tmp_path / "test.db"
    yield str(db_path)


@pytest.fixture
def tempDataDir(tmp_path, monkeypatch):
    """创建临时 data/ 目录并 monkeypatch 路径常量"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    # Monkeypatch config.py 中的路径常量
    import config
    monkeypatch.setattr(config, "PROJECT_ROOT", str(tmp_path))

    yield str(data_dir)


@pytest.fixture
def tempJsonFile(tmp_path):
    """提供临时 JSON 文件路径"""
    json_path = tmp_path / "test.json"
    yield str(json_path)


# ============================================================================
# Telegram Mock Fixture
# ============================================================================

@pytest.fixture
def mockUser():
    """Mock Telegram User"""
    user = MagicMock()
    user.id = TEST_USER_ID
    user.username = "test_user"
    user.first_name = "Test"
    user.last_name = "User"
    user.is_bot = False
    return user


@pytest.fixture
def mockChat():
    """Mock Telegram Chat"""
    chat = MagicMock()
    chat.id = TEST_CHAT_ID
    chat.type = "private"
    chat.username = "test_user"
    chat.first_name = "Test"
    chat.last_name = "User"
    return chat


@pytest.fixture
def mockMessage(mockUser, mockChat):
    """Mock Telegram Message"""
    message = MagicMock()
    message.message_id = TEST_MESSAGE_ID
    message.from_user = mockUser
    message.chat = mockChat
    message.text = "test message"
    message.date = None
    message.reply_to_message = None
    message.reply_text = AsyncMock()
    message.edit_text = AsyncMock()
    message.delete = AsyncMock()
    return message


@pytest.fixture
def mockUpdate(mockMessage):
    """Mock Telegram Update"""
    update = MagicMock()
    update.update_id = TEST_UPDATE_ID
    update.message = mockMessage
    update.effective_user = mockMessage.from_user
    update.effective_chat = mockMessage.chat
    update.effective_message = mockMessage
    # callback_query 默认 None；测试回调时手动设为 mockCallbackQuery
    update.callback_query = None
    return update


@pytest.fixture
def mockCallbackQuery(mockMessage, mockUser):
    """Mock Telegram CallbackQuery（用于 InlineKeyboard 回调测试）"""
    query = MagicMock()
    query.id = "callback_query_id_123"
    query.data = "action:arg1:arg2"
    query.from_user = mockUser
    query.message = mockMessage
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    return query


@pytest.fixture
def mockContext():
    """Mock Telegram CallbackContext"""
    context = MagicMock()
    context.bot = MagicMock()
    context.bot.send_message = AsyncMock()
    context.bot.send_chat_action = AsyncMock()
    context.bot.get_file = AsyncMock()
    context.bot.delete_message = AsyncMock()
    context.bot.edit_message_text = AsyncMock()
    context.bot.set_message_reaction = AsyncMock()
    context.bot.username = "test_bot"
    context.bot_data = {}
    context.user_data = {}
    context.chat_data = {}
    context.args = []
    return context


@pytest.fixture
def mockApp():
    """Mock Telegram Application（用于 todos reminder 等）"""
    app = MagicMock()
    app.bot = MagicMock()
    app.bot.send_message = AsyncMock()
    return app


# ============================================================================
# 时间 Fixture
# ============================================================================

@pytest.fixture
def frozenTime():
    """冻结时间（需要 freezegun）"""
    try:
        from freezegun import freeze_time
        with freeze_time(TEST_FROZEN_TIME) as frozen:
            yield frozen
    except ImportError:
        pytest.skip("freezegun not installed")


# ============================================================================
# HTTP Mock Fixture
# ============================================================================

@pytest.fixture
def mockAiohttp():
    """Mock aiohttp ClientSession（需要 aioresponses）"""
    try:
        from aioresponses import aioresponses
        with aioresponses() as m:
            yield m
    except ImportError:
        pytest.skip("aioresponses not installed")


# ============================================================================
# LLM SDK Mock Fixture
# ============================================================================

@pytest.fixture
def mockAnthropicClient():
    """Mock Anthropic SDK Client"""
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock()
    return client


@pytest.fixture
def mockGeminiClient():
    """Mock Gemini SDK Client"""
    client = MagicMock()
    client.generate_content = AsyncMock()
    return client


# ============================================================================
# Event Loop Fixture
# ============================================================================
# 注意：pytest-asyncio 的 asyncio_mode=auto 已自动提供 event_loop fixture
# 此处的手动 fixture 已移除，避免冲突