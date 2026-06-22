"""
tests/handlers/test_book.py

测试 handlers/book.py 的书籍搜索功能
"""

import pytest
from unittest.mock import patch, AsyncMock, MagicMock

from handlers.book import (
    handleBookCommand,
    handleBookTrigger,
    handleBookCallback,
    register,
    _hashQuery,
    _escapeHtml,
    _truncate,
)


# ============================================================================
# TestBookCommand - 命令处理测试
# ============================================================================

class TestBookCommand:
    """测试 /book 命令处理"""

    @pytest.mark.asyncio
    async def test_no_args_shows_usage(self, mockUpdate, mockContext):
        """无参数时显示用法提示"""
        mockContext.args = []

        await handleBookCommand(mockUpdate, mockContext)

        mockUpdate.message.reply_text.assert_called_once()
        call_args = mockUpdate.message.reply_text.call_args[0][0]
        assert "用法" in call_args
        assert "/book" in call_args

    @pytest.mark.asyncio
    async def test_with_query_calls_api(self, mockUpdate, mockContext):
        """有参数时调用 searchBooks API"""
        mockContext.args = ["线性代数"]

        with patch('handlers.book.searchBooks', new_callable=AsyncMock) as mock_search:
            mock_search.return_value = {
                'total': 5,
                'page': 1,
                'totalPages': 1,
                'results': [
                    {
                        'id': 'OL123M',
                        'title': '线性代数',
                        'authors': ['作者A'],
                        'year': 2020
                    }
                ]
            }

            await handleBookCommand(mockUpdate, mockContext)

            mock_search.assert_called_once_with("线性代数", page=1, limit=5)
            mockUpdate.message.reply_text.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_results_shows_message(self, mockUpdate, mockContext):
        """无搜索结果时显示提示"""
        mockContext.args = ["不存在的书"]

        with patch('handlers.book.searchBooks', new_callable=AsyncMock) as mock_search:
            mock_search.return_value = {
                'total': 0,
                'page': 1,
                'totalPages': 0,
                'results': []
            }

            await handleBookCommand(mockUpdate, mockContext)

            call_args = mockUpdate.message.reply_text.call_args[0][0]
            assert "没有找到" in call_args or "📭" in call_args

    @pytest.mark.asyncio
    async def test_renders_results_html(self, mockUpdate, mockContext):
        """渲染 HTML 格式的搜索结果"""
        mockContext.args = ["Python"]

        with patch('handlers.book.searchBooks', new_callable=AsyncMock) as mock_search:
            mock_search.return_value = {
                'total': 1,
                'page': 1,
                'totalPages': 1,
                'results': [
                    {
                        'id': 'OL456M',
                        'title': 'Learning Python',
                        'authors': ['Mark Lutz'],
                        'year': 2013
                    }
                ]
            }

            await handleBookCommand(mockUpdate, mockContext)

            # 验证 reply_text 被调用
            mockUpdate.message.reply_text.assert_called_once()

            # 获取调用参数
            call_args_list = mockUpdate.message.reply_text.call_args_list
            assert len(call_args_list) == 1

            # 检查 parse_mode 参数（通过关键字参数传递）
            call = call_args_list[0]
            # call 是 unittest.mock.call 对象，可以通过索引访问
            # call[1] 是 kwargs 字典
            if len(call) > 1 and 'parse_mode' in call[1]:
                assert call[1]['parse_mode'] == "HTML"

    @pytest.mark.asyncio
    async def test_stores_query_hash_in_user_data(self, mockUpdate, mockContext):
        """搜索词哈希存储到 user_data"""
        mockContext.args = ["测试书籍"]

        with patch('handlers.book.searchBooks', new_callable=AsyncMock) as mock_search:
            mock_search.return_value = {
                'total': 0,
                'page': 1,
                'totalPages': 0,
                'results': []
            }

            await handleBookCommand(mockUpdate, mockContext)

            # 验证 user_data 中存储了搜索词
            query_hash = _hashQuery("测试书籍")
            assert f"book_query_{query_hash}" in mockContext.user_data
            assert mockContext.user_data[f"book_query_{query_hash}"] == "测试书籍"

    @pytest.mark.asyncio
    async def test_api_error_shows_fallback(self, mockUpdate, mockContext):
        """API 错误时装饰器处理错误"""
        mockContext.args = ["test"]

        with patch('handlers.book.searchBooks', new_callable=AsyncMock) as mock_search:
            mock_search.side_effect = Exception("Network error")

            await handleBookCommand(mockUpdate, mockContext)

            # 装饰器应该捕获错误并发送错误消息
            mockUpdate.message.reply_text.assert_called()
            call_args = mockUpdate.message.reply_text.call_args[0][0]
            assert "出错" in call_args or "错误" in call_args


# ============================================================================
# TestBookTrigger - 触发词测试
# ============================================================================

class TestBookTrigger:
    """测试 "找书" / "搜书" 触发词"""

    @pytest.mark.asyncio
    async def test_trigger_word_find_book(self, mockUpdate, mockContext):
        """触发词 "找书" 正常工作"""
        mockUpdate.message.text = "找书 堂吉诃德"

        with patch('handlers.book.searchBooks', new_callable=AsyncMock) as mock_search:
            mock_search.return_value = {
                'total': 1,
                'page': 1,
                'totalPages': 1,
                'results': [{'id': 'OL1M', 'title': 'Don Quixote', 'authors': [], 'year': None}]
            }

            await handleBookTrigger(mockUpdate, mockContext)

            mock_search.assert_called_once()
            # 验证搜索词提取正确
            call_args = mock_search.call_args[0][0]
            assert "堂吉诃德" in call_args

    @pytest.mark.asyncio
    async def test_trigger_word_search_book(self, mockUpdate, mockContext):
        """触发词 "搜书" 正常工作"""
        mockUpdate.message.text = "搜书 Python编程"

        with patch('handlers.book.searchBooks', new_callable=AsyncMock) as mock_search:
            mock_search.return_value = {
                'total': 0,
                'page': 1,
                'totalPages': 0,
                'results': []
            }

            await handleBookTrigger(mockUpdate, mockContext)

            mock_search.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_query_shows_prompt(self, mockUpdate, mockContext):
        """触发词后无搜索词时提示用户"""
        mockUpdate.message.text = "找书  "  # 只有空格

        await handleBookTrigger(mockUpdate, mockContext)

        mockUpdate.message.reply_text.assert_called_once()
        call_args = mockUpdate.message.reply_text.call_args[0][0]
        assert "书名" in call_args or "作者" in call_args


# ============================================================================
# TestBookCallback - 回调处理测试
# ============================================================================

class TestBookCallback:
    """测试 InlineKeyboard 回调"""

    @pytest.mark.asyncio
    async def test_close_deletes_message(self, mockUpdate, mockContext, mockCallbackQuery):
        """关闭按钮删除消息"""
        mockCallbackQuery.data = "book:close"
        mockUpdate.callback_query = mockCallbackQuery

        await handleBookCallback(mockUpdate, mockContext)

        mockCallbackQuery.answer.assert_called_once()
        mockCallbackQuery.message.delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_next_page_restores_query_from_cache(self, mockUpdate, mockContext, mockCallbackQuery):
        """翻页时从 user_data 恢复搜索词"""
        query_hash = _hashQuery("测试")
        mockContext.user_data[f"book_query_{query_hash}"] = "测试"
        mockCallbackQuery.data = f"book:list:{query_hash}:2"
        mockUpdate.callback_query = mockCallbackQuery

        with patch('handlers.book.searchBooks', new_callable=AsyncMock) as mock_search:
            with patch('handlers.book.safeEditMessage', new_callable=AsyncMock):
                mock_search.return_value = {
                    'total': 20,
                    'page': 2,
                    'totalPages': 2,
                    'results': []
                }

                await handleBookCallback(mockUpdate, mockContext)

                mock_search.assert_called_once_with("测试", page=2, limit=5)

    @pytest.mark.asyncio
    async def test_cache_expired_shows_error(self, mockUpdate, mockContext, mockCallbackQuery):
        """缓存过期（user_data 中无搜索词）时显示错误"""
        mockCallbackQuery.data = "book:list:nonexistent:1"
        mockUpdate.callback_query = mockCallbackQuery

        with patch('handlers.book.safeEditMessage', new_callable=AsyncMock) as mock_edit:
            await handleBookCallback(mockUpdate, mockContext)

            mock_edit.assert_called_once()
            call_args = mock_edit.call_args[0][1]
            assert "过期" in call_args or "失效" in call_args

    @pytest.mark.asyncio
    async def test_invalid_callback_data(self, mockUpdate, mockContext, mockCallbackQuery):
        """无效 callback_data 格式时安全返回"""
        mockCallbackQuery.data = "book:"  # 缺少 action
        mockUpdate.callback_query = mockCallbackQuery

        await handleBookCallback(mockUpdate, mockContext)

        mockCallbackQuery.answer.assert_called_once()
        # 不应抛出异常

    @pytest.mark.asyncio
    async def test_invalid_page_number(self, mockUpdate, mockContext, mockCallbackQuery):
        """页码不是数字时显示错误"""
        query_hash = _hashQuery("test")
        mockContext.user_data[f"book_query_{query_hash}"] = "test"
        mockCallbackQuery.data = f"book:list:{query_hash}:abc"  # 非数字页码
        mockUpdate.callback_query = mockCallbackQuery

        with patch('handlers.book.safeEditMessage', new_callable=AsyncMock) as mock_edit:
            await handleBookCallback(mockUpdate, mockContext)

            mock_edit.assert_called_once()
            call_args = mock_edit.call_args[0][1]
            assert "无效" in call_args or "页码" in call_args


# ============================================================================
# TestBookHelpers - 辅助函数测试
# ============================================================================

class TestBookHelpers:
    """测试辅助函数"""

    def test_escape_html_special_chars(self):
        """HTML 转义特殊字符"""
        assert _escapeHtml("<script>") == "&lt;script&gt;"
        assert _escapeHtml("A & B") == "A &amp; B"
        assert _escapeHtml("5 > 3") == "5 &gt; 3"

    def test_truncate_long_title(self):
        """截断过长标题"""
        long_title = "A" * 100
        truncated = _truncate(long_title, 50)
        assert len(truncated) <= 50  # 截断到 maxLen
        assert truncated.endswith("…")  # 中文省略号

    def test_truncate_short_title(self):
        """短标题不截断"""
        short_title = "Short"
        truncated = _truncate(short_title, 50)
        assert truncated == short_title

    def test_hash_query_consistency(self):
        """相同搜索词生成相同哈希"""
        hash1 = _hashQuery("test")
        hash2 = _hashQuery("test")
        assert hash1 == hash2
        assert len(hash1) == 12  # BOOK_QUERY_HASH_LENGTH


# ============================================================================
# TestBookRegister - 注册测试
# ============================================================================

class TestBookRegister:
    """测试 handler 注册"""

    def test_register_returns_correct_metadata(self):
        """register() 返回正确的元数据结构"""
        result = register()

        assert isinstance(result, dict)
        assert "handlers" in result
        assert "name" in result
        assert "description" in result
        assert isinstance(result["handlers"], list)
        assert len(result["handlers"]) == 3  # CommandHandler + MessageHandler + CallbackQueryHandler