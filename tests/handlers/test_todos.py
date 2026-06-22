"""
tests/handlers/test_todos.py

测试 handlers/todos.py 的待办事项功能
"""

import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from datetime import datetime

import handlers.todos as todos_module
from handlers.todos import (
    handleTodosCommand,
    handleTodosCallback,
    register,
    _lastQueryMessages,
    _MAX_CACHED_MESSAGES,
)


@pytest.fixture(autouse=True)
def clearLruCache():
    """每个测试前后清空 LRU 缓存，避免测试间互相影响"""
    _lastQueryMessages.clear()
    yield
    _lastQueryMessages.clear()


# ============================================================================
# TestTodosCommand - 命令处理测试
# ============================================================================

class TestTodosCommand:
    """测试 /todos 命令处理"""

    @pytest.mark.asyncio
    async def test_no_args_shows_list(self, mockUpdate, mockContext):
        """无参数时显示待办列表"""
        mockContext.args = []
        mockUpdate.message.reply_text = AsyncMock(return_value=MagicMock(message_id=100))

        with patch('handlers.todos.getTodosCount', new_callable=AsyncMock, return_value=0), \
             patch('handlers.todos.getTodos', new_callable=AsyncMock, return_value=[]), \
             patch('handlers.todos.renderListView', return_value=("列表", None)):

            await handleTodosCommand(mockUpdate, mockContext)

            mockUpdate.message.reply_text.assert_called_once()

    @pytest.mark.asyncio
    async def test_help_shows_usage(self, mockUpdate, mockContext):
        """help 参数显示用法"""
        mockContext.args = ["help"]

        await handleTodosCommand(mockUpdate, mockContext)

        call_args = mockUpdate.message.reply_text.call_args[0][0]
        assert "用法" in call_args

    @pytest.mark.asyncio
    async def test_with_content_adds_todo(self, mockUpdate, mockContext):
        """有内容时添加待办"""
        mockContext.args = ["买牛奶"]

        with patch('handlers.todos.parsePriority', return_value=("P_", "买牛奶")), \
             patch('handlers.todos.parseTime', return_value=(None, "买牛奶")), \
             patch('handlers.todos.addTodo', new_callable=AsyncMock, return_value=1), \
             patch('handlers.todos.logAction', new_callable=AsyncMock):

            await handleTodosCommand(mockUpdate, mockContext)

            call_args = mockUpdate.message.reply_text.call_args[0][0]
            assert "记下来了" in call_args
            assert "买牛奶" in call_args

    @pytest.mark.asyncio
    async def test_parse_time_from_text(self, mockUpdate, mockContext):
        """解析时间表达式"""
        mockContext.args = ["2h", "吃饭"]
        remind_time = datetime(2026, 6, 20, 14, 0)

        with patch('handlers.todos.parsePriority', return_value=("P_", "2h 吃饭")), \
             patch('handlers.todos.parseTime', return_value=(remind_time, "吃饭")) as mock_parse, \
             patch('handlers.todos.addTodo', new_callable=AsyncMock, return_value=1), \
             patch('handlers.todos.formatRemindTime', return_value="14:00"), \
             patch('handlers.todos.logAction', new_callable=AsyncMock):

            await handleTodosCommand(mockUpdate, mockContext)

            mock_parse.assert_called_once()
            # addTodo 应该收到 remind_time
            from handlers.todos import addTodo
            addTodo.assert_called_once()

    @pytest.mark.asyncio
    async def test_parse_priority_from_text(self, mockUpdate, mockContext):
        """解析优先级"""
        mockContext.args = ["P1", "买牛奶"]

        with patch('handlers.todos.parsePriority', return_value=("P1", "买牛奶")) as mock_pri, \
             patch('handlers.todos.parseTime', return_value=(None, "买牛奶")), \
             patch('handlers.todos.addTodo', new_callable=AsyncMock, return_value=1), \
             patch('handlers.todos.logAction', new_callable=AsyncMock):

            await handleTodosCommand(mockUpdate, mockContext)

            mock_pri.assert_called_once()
            call_args = mockUpdate.message.reply_text.call_args[0][0]
            assert "P1" in call_args

    @pytest.mark.asyncio
    async def test_empty_content_rejected(self, mockUpdate, mockContext):
        """空内容被拒绝"""
        mockContext.args = ["P1"]  # 只有优先级，无内容

        with patch('handlers.todos.parsePriority', return_value=("P1", "")), \
             patch('handlers.todos.parseTime', return_value=(None, "")):

            await handleTodosCommand(mockUpdate, mockContext)

            call_args = mockUpdate.message.reply_text.call_args[0][0]
            assert "不能为空" in call_args

    @pytest.mark.asyncio
    async def test_add_failure_shows_message(self, mockUpdate, mockContext):
        """添加失败时显示错误"""
        mockContext.args = ["买牛奶"]

        with patch('handlers.todos.parsePriority', return_value=("P_", "买牛奶")), \
             patch('handlers.todos.parseTime', return_value=(None, "买牛奶")), \
             patch('handlers.todos.addTodo', new_callable=AsyncMock, return_value=None):

            await handleTodosCommand(mockUpdate, mockContext)

            call_args = mockUpdate.message.reply_text.call_args[0][0]
            assert "添加失败" in call_args


# ============================================================================
# TestTodosLRUCache - LRU 缓存测试（审计重点）
# ============================================================================

class TestTodosLRUCache:
    """测试 LRU 缓存机制"""

    @pytest.mark.asyncio
    async def test_lru_cache_stores_last_message(self, mockUpdate, mockContext):
        """缓存存储最后一条列表消息 ID"""
        mockContext.args = []
        mockUpdate.message.reply_text = AsyncMock(return_value=MagicMock(message_id=555))

        with patch('handlers.todos.getTodosCount', new_callable=AsyncMock, return_value=0), \
             patch('handlers.todos.getTodos', new_callable=AsyncMock, return_value=[]), \
             patch('handlers.todos.renderListView', return_value=("列表", None)):

            await handleTodosCommand(mockUpdate, mockContext)

            chatID = str(mockUpdate.effective_chat.id)
            userID = str(mockUpdate.effective_user.id)
            key = (chatID, userID)
            assert _lastQueryMessages.get(key) == 555

    @pytest.mark.asyncio
    async def test_lru_cache_deletes_old_message(self, mockUpdate, mockContext):
        """新列表请求时删除旧消息"""
        chatID = str(mockUpdate.effective_chat.id)
        userID = str(mockUpdate.effective_user.id)
        key = (chatID, userID)
        # 预设一条旧消息
        _lastQueryMessages[key] = 100

        mockContext.args = []
        mockUpdate.message.reply_text = AsyncMock(return_value=MagicMock(message_id=200))
        mockContext.bot.delete_message = AsyncMock()

        with patch('handlers.todos.getTodosCount', new_callable=AsyncMock, return_value=0), \
             patch('handlers.todos.getTodos', new_callable=AsyncMock, return_value=[]), \
             patch('handlers.todos.renderListView', return_value=("列表", None)):

            await handleTodosCommand(mockUpdate, mockContext)

            # 旧消息应被删除
            mockContext.bot.delete_message.assert_called_once()
            # 新消息 ID 被记录
            assert _lastQueryMessages.get(key) == 200

    @pytest.mark.asyncio
    async def test_lru_cache_evicts_oldest_when_full(self, mockUpdate, mockContext):
        """缓存满时淘汰最旧条目"""
        # 填满缓存到上限
        for i in range(_MAX_CACHED_MESSAGES):
            _lastQueryMessages[(f"chat{i}", f"user{i}")] = i

        assert len(_lastQueryMessages) == _MAX_CACHED_MESSAGES
        oldest_key = ("chat0", "user0")
        assert oldest_key in _lastQueryMessages

        mockContext.args = []
        mockUpdate.message.reply_text = AsyncMock(return_value=MagicMock(message_id=9999))

        with patch('handlers.todos.getTodosCount', new_callable=AsyncMock, return_value=0), \
             patch('handlers.todos.getTodos', new_callable=AsyncMock, return_value=[]), \
             patch('handlers.todos.renderListView', return_value=("列表", None)):

            await handleTodosCommand(mockUpdate, mockContext)

            # 最旧条目应被淘汰
            assert oldest_key not in _lastQueryMessages
            # 缓存大小不超过上限
            assert len(_lastQueryMessages) <= _MAX_CACHED_MESSAGES

    @pytest.mark.asyncio
    async def test_cache_isolated_by_chat_and_user(self, mockUpdate, mockContext):
        """缓存按 (chat_id, user_id) 隔离"""
        mockContext.args = []
        mockUpdate.message.reply_text = AsyncMock(return_value=MagicMock(message_id=111))

        with patch('handlers.todos.getTodosCount', new_callable=AsyncMock, return_value=0), \
             patch('handlers.todos.getTodos', new_callable=AsyncMock, return_value=[]), \
             patch('handlers.todos.renderListView', return_value=("列表", None)):

            await handleTodosCommand(mockUpdate, mockContext)

            chatID = str(mockUpdate.effective_chat.id)
            userID = str(mockUpdate.effective_user.id)
            # 不同用户不应共享缓存键
            assert (chatID, userID) in _lastQueryMessages
            assert ("other_chat", "other_user") not in _lastQueryMessages


# ============================================================================
# TestTodosCallbacks - 回调处理测试
# ============================================================================

class TestTodosCallbacks:
    """测试 InlineKeyboard 回调"""

    @pytest.mark.asyncio
    async def test_close_deletes_message(self, mockUpdate, mockContext, mockCallbackQuery):
        """关闭按钮删除消息"""
        mockCallbackQuery.data = "todos:close"
        mockUpdate.callback_query = mockCallbackQuery

        await handleTodosCallback(mockUpdate, mockContext)

        mockCallbackQuery.answer.assert_called_once()
        mockCallbackQuery.message.delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_list_callback_shows_list(self, mockUpdate, mockContext, mockCallbackQuery):
        """列表回调显示待办列表"""
        mockCallbackQuery.data = "todos:list:1"
        mockUpdate.callback_query = mockCallbackQuery

        with patch('handlers.todos.getTodosCount', new_callable=AsyncMock, return_value=0), \
             patch('handlers.todos.getTodos', new_callable=AsyncMock, return_value=[]), \
             patch('handlers.todos.renderListView', return_value=("列表", None)), \
             patch('handlers.todos.safeEditMessage', new_callable=AsyncMock) as mock_edit:

            await handleTodosCallback(mockUpdate, mockContext)

            mock_edit.assert_called_once()

    @pytest.mark.asyncio
    async def test_detail_callback_shows_detail(self, mockUpdate, mockContext, mockCallbackQuery):
        """详情回调显示待办详情"""
        mockCallbackQuery.data = "todos:detail:5"
        mockUpdate.callback_query = mockCallbackQuery
        userID = str(mockUpdate.effective_user.id)

        with patch('handlers.todos.getTodoByID', new_callable=AsyncMock,
                   return_value={'id': 5, 'user_id': userID, 'content': '测试', 'status': 'pending'}), \
             patch('handlers.todos.renderDetailView', return_value=("详情", None)), \
             patch('handlers.todos.safeEditMessage', new_callable=AsyncMock) as mock_edit:

            await handleTodosCallback(mockUpdate, mockContext)

            mock_edit.assert_called_once()

    @pytest.mark.asyncio
    async def test_detail_callback_not_owner_denied(self, mockUpdate, mockContext, mockCallbackQuery):
        """非所有者访问详情被拒绝"""
        mockCallbackQuery.data = "todos:detail:5"
        mockUpdate.callback_query = mockCallbackQuery

        with patch('handlers.todos.getTodoByID', new_callable=AsyncMock,
                   return_value={'id': 5, 'user_id': 'different_user', 'content': '测试'}), \
             patch('handlers.todos.safeEditMessage', new_callable=AsyncMock) as mock_edit:

            await handleTodosCallback(mockUpdate, mockContext)

            call_args = mock_edit.call_args[0][1]
            assert "不属于你" in call_args

    @pytest.mark.asyncio
    async def test_set_priority_updates_db(self, mockUpdate, mockContext, mockCallbackQuery):
        """修改优先级更新数据库"""
        mockCallbackQuery.data = "todos:pri:5:P0"
        mockUpdate.callback_query = mockCallbackQuery
        userID = str(mockUpdate.effective_user.id)

        with patch('handlers.todos.getTodoByID', new_callable=AsyncMock,
                   return_value={'id': 5, 'user_id': userID, 'content': '测试', 'priority': 'P1'}), \
             patch('handlers.todos.updateTodo', new_callable=AsyncMock) as mock_update, \
             patch('handlers.todos.renderDetailView', return_value=("详情", None)), \
             patch('handlers.todos.safeEditMessage', new_callable=AsyncMock):

            await handleTodosCallback(mockUpdate, mockContext)

            mock_update.assert_called_once_with(5, priority="P0")

    @pytest.mark.asyncio
    async def test_done_callback_marks_complete(self, mockUpdate, mockContext, mockCallbackQuery):
        """完成回调标记待办完成"""
        mockCallbackQuery.data = "todos:done:5"
        mockUpdate.callback_query = mockCallbackQuery
        userID = str(mockUpdate.effective_user.id)

        with patch('handlers.todos.getTodoByID', new_callable=AsyncMock,
                   return_value={'id': 5, 'user_id': userID, 'content': '测试'}), \
             patch('handlers.todos.markDone', new_callable=AsyncMock) as mock_done, \
             patch('handlers.todos.safeEditMessage', new_callable=AsyncMock) as mock_edit:

            await handleTodosCallback(mockUpdate, mockContext)

            mock_done.assert_called_once_with(5)
            call_args = mock_edit.call_args[0][1]
            assert "完成" in call_args

    @pytest.mark.asyncio
    async def test_delete_callback_removes_todo(self, mockUpdate, mockContext, mockCallbackQuery):
        """删除回调移除待办"""
        mockCallbackQuery.data = "todos:del:5"
        mockUpdate.callback_query = mockCallbackQuery
        userID = str(mockUpdate.effective_user.id)

        with patch('handlers.todos.getTodoByID', new_callable=AsyncMock,
                   return_value={'id': 5, 'user_id': userID, 'content': '测试'}), \
             patch('handlers.todos.deleteTodo', new_callable=AsyncMock) as mock_del, \
             patch('handlers.todos.getTodosCount', new_callable=AsyncMock, return_value=0), \
             patch('handlers.todos.getTodos', new_callable=AsyncMock, return_value=[]), \
             patch('handlers.todos.renderListView', return_value=("列表", None)), \
             patch('handlers.todos.safeEditMessage', new_callable=AsyncMock):

            await handleTodosCallback(mockUpdate, mockContext)

            mock_del.assert_called_once_with(5)

    @pytest.mark.asyncio
    async def test_invalid_callback_data(self, mockUpdate, mockContext, mockCallbackQuery):
        """无效 callback_data 显示错误"""
        mockCallbackQuery.data = "todos"  # 缺少 action
        mockUpdate.callback_query = mockCallbackQuery

        with patch('handlers.todos.safeEditMessage', new_callable=AsyncMock) as mock_edit:
            await handleTodosCallback(mockUpdate, mockContext)

            mock_edit.assert_called_once()
            call_args = mock_edit.call_args[0][1]
            assert "无效" in call_args

    @pytest.mark.asyncio
    async def test_invalid_todo_id(self, mockUpdate, mockContext, mockCallbackQuery):
        """非数字 todoID 显示错误"""
        mockCallbackQuery.data = "todos:detail:abc"  # 非数字 ID
        mockUpdate.callback_query = mockCallbackQuery

        with patch('handlers.todos.safeEditMessage', new_callable=AsyncMock) as mock_edit:
            await handleTodosCallback(mockUpdate, mockContext)

            mock_edit.assert_called_once()
            call_args = mock_edit.call_args[0][1]
            assert "无效" in call_args

    @pytest.mark.asyncio
    async def test_invalid_priority_rejected(self, mockUpdate, mockContext, mockCallbackQuery):
        """无效优先级被拒绝"""
        mockCallbackQuery.data = "todos:pri:5:P99"  # 无效优先级
        mockUpdate.callback_query = mockCallbackQuery

        with patch('handlers.todos.safeEditMessage', new_callable=AsyncMock) as mock_edit:
            await handleTodosCallback(mockUpdate, mockContext)

            call_args = mock_edit.call_args[0][1]
            assert "无效" in call_args


# ============================================================================
# TestTodosRegister - 注册测试
# ============================================================================

class TestTodosRegister:
    """测试 handler 注册"""

    def test_register_returns_correct_metadata(self):
        """register() 返回正确的元数据结构"""
        result = register()

        assert isinstance(result, dict)
        assert "handlers" in result
        assert "name" in result
        assert "description" in result
        assert result["name"] == "待办事项"

    def test_register_includes_callback_handler(self):
        """register() 包含命令和回调 handler"""
        result = register()

        assert len(result["handlers"]) == 2  # CommandHandler + CallbackQueryHandler
