"""
tests/utils/core/test_errorDecorators.py

错误处理装饰器（utils/core/errorDecorators.py）行为矩阵：
    - handleErrors：sync/async 双 wrapper、defaultReturn、reraise、ApplicationHandlerStop 放行
    - handleTelegramErrors：裸装饰/带参两种用法、errorReply 回复、用户信息提取
    - suppressErrors：完全静默 + Stop 放行
    - ErrorContext：上下文管理器吞异常

已知边界（不测）：装饰器不覆盖 create_task 内部异常——这是文档化约定，
不是装饰器可测行为。
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.ext import ApplicationHandlerStop

from utils.core.errorDecorators import (
    ErrorContext,
    handleErrors,
    handleTelegramErrors,
    suppressErrors,
)



def _raising(exc=RuntimeError("boom")):
    return exc



class TestHandleErrors:
    @patch("utils.core.errorDecorators.getErrorHandler")
    async def test_async_exception_returns_default(self, mockHandler):
        @handleErrors(errorType="Test", defaultReturn="fallback")
        async def fn():
            raise _raising()
        assert await fn() == "fallback"
        mockHandler.return_value.logError.assert_called_once()

    @patch("utils.core.errorDecorators.getErrorHandler")
    async def test_async_normal_passthrough(self, mockHandler):
        @handleErrors()
        async def fn():
            return 42
        assert await fn() == 42
        mockHandler.return_value.logError.assert_not_called()

    @patch("utils.core.errorDecorators.getErrorHandler")
    def test_sync_exception_returns_default(self, mockHandler):
        @handleErrors(errorType="Test", defaultReturn=[])
        def fn():
            raise _raising()
        assert fn() == []
        mockHandler.return_value.logError.assert_called_once()

    @patch("utils.core.errorDecorators.getErrorHandler")
    async def test_reraise(self, mockHandler):
        @handleErrors(reraise=True)
        async def fn():
            raise _raising()
        with pytest.raises(RuntimeError, match="boom"):
            await fn()

    async def test_handler_stop_not_swallowed(self):
        """ApplicationHandlerStop 是 PTB 控制流，必须放行"""
        @handleErrors()
        async def fn():
            raise ApplicationHandlerStop()
        with pytest.raises(ApplicationHandlerStop):
            await fn()

    @patch("utils.core.errorDecorators.getErrorHandler")
    async def test_silent_skips_logging(self, mockHandler):
        @handleErrors(silent=True)
        async def fn():
            raise _raising()
        await fn()
        mockHandler.return_value.logError.assert_not_called()

    @patch("utils.core.errorDecorators.getErrorHandler")
    async def test_log_context_contains_function_name(self, mockHandler):
        @handleErrors()
        async def myLabeledFn():
            raise _raising()
        await myLabeledFn()
        ctx = mockHandler.return_value.logError.call_args.kwargs["context"]
        assert "myLabeledFn" in ctx



class TestHandleTelegramErrors:
    def _update(self, text="hello", hasUser=True):
        update = MagicMock()
        update.message.text = text
        if hasUser:
            update.effective_user.username = "tester"
        else:
            update.effective_user = None
        return update

    @patch("utils.core.errorDecorators.getErrorHandler")
    async def test_bare_decorator_usage(self, mockHandler):
        """@handleTelegramErrors（裸）—— func 非 None 直接装饰"""
        @handleTelegramErrors
        async def fn(update, context):
            raise _raising()
        result = await fn(self._update(), MagicMock())
        assert result is None
        mockHandler.return_value.logError.assert_called_once()

    @patch("utils.core.errorDecorators.getErrorHandler")
    async def test_error_reply_sent(self, mockHandler):
        update = self._update()
        update.effective_message.reply_text = AsyncMock()

        @handleTelegramErrors(errorReply="出错啦")
        async def fn(update, context):
            raise _raising()
        await fn(update, MagicMock())
        update.effective_message.reply_text.assert_awaited_once_with("出错啦")

    @patch("utils.core.errorDecorators.getErrorHandler")
    async def test_error_reply_failure_swallowed(self, mockHandler):
        """回复失败（如用户 block）不再抛出"""
        update = self._update()
        update.effective_message.reply_text = AsyncMock(side_effect=Exception("blocked"))

        @handleTelegramErrors(errorReply="x")
        async def fn(update, context):
            raise _raising()
        await fn(update, MagicMock())   # 不抛即通过

    @patch("utils.core.errorDecorators.getErrorHandler")
    async def test_user_info_in_context(self, mockHandler):
        @handleTelegramErrors
        async def fn(update, context):
            raise _raising()
        await fn(self._update(text="/cmd 参数"), MagicMock())
        ctx = mockHandler.return_value.logError.call_args.kwargs["context"]
        assert "tester" in ctx and "/cmd 参数" in ctx

    @patch("utils.core.errorDecorators.getErrorHandler")
    async def test_callback_query_context(self, mockHandler):
        update = MagicMock()
        update.message = None
        update.effective_user.username = "tester"
        update.callback_query.data = "book:next:1"

        @handleTelegramErrors
        async def fn(update, context):
            raise _raising()
        await fn(update, MagicMock())
        ctx = mockHandler.return_value.logError.call_args.kwargs["context"]
        assert "回调查询" in ctx and "book:next:1" in ctx

    async def test_handler_stop_passthrough(self):
        @handleTelegramErrors
        async def fn(update, context):
            raise ApplicationHandlerStop()
        with pytest.raises(ApplicationHandlerStop):
            await fn(self._update(), MagicMock())



class TestSuppressErrors:
    async def test_async_silent_default(self):
        @suppressErrors(defaultReturn=0)
        async def fn():
            raise _raising()
        assert await fn() == 0

    def test_sync_silent_default(self):
        @suppressErrors(defaultReturn=None)
        def fn():
            raise _raising()
        assert fn() is None

    async def test_stop_passthrough(self):
        @suppressErrors()
        async def fn():
            raise ApplicationHandlerStop()
        with pytest.raises(ApplicationHandlerStop):
            await fn()



class TestErrorContext:
    @patch("utils.core.errorDecorators.getErrorHandler")
    def test_swallows_exception(self, mockHandler):
        with ErrorContext("DbOp") as ctx:
            raise _raising()
        assert isinstance(ctx.exception, RuntimeError)
        mockHandler.return_value.logError.assert_called_once()

    def test_no_exception_no_log(self):
        with patch("utils.core.errorDecorators.getErrorHandler") as mockHandler:
            with ErrorContext("DbOp"):
                pass
        mockHandler.return_value.logError.assert_not_called()

    @patch("utils.core.errorDecorators.getErrorHandler")
    def test_silent_mode(self, mockHandler):
        with ErrorContext("DbOp", silent=True):
            raise _raising()
        mockHandler.return_value.logError.assert_not_called()
