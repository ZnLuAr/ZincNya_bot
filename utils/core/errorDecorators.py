"""
utils/core/errorDecorators.py

统一的错误处理装饰器

提供：
- @handleErrors - 通用错误处理装饰器
- @handleTelegramErrors - Telegram handler 专用装饰器
- @handleAsyncErrors - 异步函数错误处理装饰器
- ErrorContext - 错误上下文管理器

使用示例：
    @handleErrors(errorType="BookSearch", logToFile=True)
    async def searchBooks(query: str):
        # 函数内的异常会被自动捕获和记录
        ...

    @handleTelegramErrors
    async def onCommand(update: Update, context: ContextTypes.DEFAULT_TYPE):
        # Telegram handler 专用，自动提取用户信息
        ...

    with ErrorContext("DatabaseOperation"):
        # 代码块内的异常会被捕获
        ...
"""




import functools
import traceback
from datetime import datetime
from typing import Optional, Callable, Any

from utils.errorHandler import getErrorHandler




class ErrorContext:
    """
    错误上下文管理器

    使用示例：
        with ErrorContext("DatabaseOperation", logToFile=True):
            # 可能抛出异常的代码
            conn.execute(...)
    """


    def __init__(
        self,
        operation: str,
        logToFile: bool = True,
        silent: bool = False,
    ):
        """
        参数:
            operation: 操作描述
            logToFile: 是否写入错误日志文件
            silent: 是否静默（不在终端显示）
        """
        self.operation = operation
        self.logToFile = logToFile
        self.silent = silent
        self.exception: Optional[Exception] = None


    def __enter__(self):
        return self


    def __exit__(self, excType, excValue, excTraceback):
        if excType is None:
            return False

        # 记录异常
        self.exception = excValue
        handler = getErrorHandler()

        if not self.silent:
            errorType = excType.__name__
            message = str(excValue)
            context = f"Operation: {self.operation}"

            handler.logError(
                errorType=errorType,
                message=message,
                exception=excValue,
                context=context
            )

        # 返回 True 表示异常已处理，不再向上传播
        return True




def handleErrors(
    errorType: str = "General",
    logToFile: bool = True,
    silent: bool = False,
    defaultReturn: Any = None,
    reraise: bool = False
):
    """
    通用错误处理装饰器

    参数:
        errorType: 错误类型标识（用于日志分类）
        logToFile: 是否写入错误日志文件
        silent: 是否静默（不在终端显示）
        defaultReturn: 发生错误时的默认返回值
        reraise: 是否在记录后重新抛出异常

    使用示例：
        @handleErrors(errorType="BookSearch", defaultReturn=[])
        async def searchBooks(query: str):
            ...
    """

    def decorator(func: Callable):
        @functools.wraps(func)
        async def asyncWrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                if not silent:
                    handler = getErrorHandler()
                    handler.logError(
                        errorType=errorType,
                        message=str(e),
                        exception=e,
                        context=f"Function: {func.__name__}"
                    )

                if reraise:
                    raise

                return defaultReturn

        @functools.wraps(func)
        def syncWrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                if not silent:
                    handler = getErrorHandler()
                    handler.logError(
                        errorType=errorType,
                        message=str(e),
                        exception=e,
                        context=f"Function: {func.__name__}"
                    )

                if reraise:
                    raise

                return defaultReturn

        # 根据函数类型返回对应的 wrapper
        import asyncio
        if asyncio.iscoroutinefunction(func):
            return asyncWrapper
        else:
            return syncWrapper

    return decorator




def handleTelegramErrors(func: Callable = None, *, errorReply: str = None):
    """
    Telegram handler 专用错误处理装饰器

    自动提取 update 和 context，记录用户信息。
    可选择在出错时回复用户友好的错误消息。

    使用示例：
        # 静默处理（仅记录日志）
        @handleTelegramErrors
        async def onCommand(update: Update, context: ContextTypes.DEFAULT_TYPE):
            ...

        # 出错时回复用户
        @handleTelegramErrors(errorReply="呜……出错了喵……")
        async def onCommand(update: Update, context: ContextTypes.DEFAULT_TYPE):
            ...
    """
    def decorator(fn: Callable):
        @functools.wraps(fn)
        async def wrapper(update, context, *args, **kwargs):
            try:
                return await fn(update, context, *args, **kwargs)
            except Exception as e:
                handler = getErrorHandler()

                # 提取用户信息
                user = None
                if update and update.effective_user:
                    user = update.effective_user.username or update.effective_user.first_name

                # 提取命令/消息信息
                message = ""
                if update and update.message:
                    message = update.message.text or "[非文本消息]"
                elif update and update.callback_query:
                    message = f"[回调查询: {update.callback_query.data}]"

                handler.logError(
                    errorType="TelegramHandler",
                    message=str(e),
                    exception=e,
                    context=f"User: {user}, Message: {message[:50]}"
                )

                # 回复用户友好的错误消息
                if errorReply:
                    try:
                        if update and update.effective_message:
                            await update.effective_message.reply_text(errorReply)
                    except Exception:
                        pass  # 回复失败不再抛出

                return None

        return wrapper

    # 支持 @handleTelegramErrors 和 @handleTelegramErrors(...) 两种用法
    if func is not None:
        return decorator(func)
    return decorator




def suppressErrors(defaultReturn: Any = None):
    """
    静默错误装饰器 - 完全忽略异常，不记录日志

    仅用于非关键操作（如统计、缓存更新等）

    使用示例：
        @suppressErrors(defaultReturn=0)
        def updateStatistics():
            ...
    """
    def decorator(func: Callable):
        @functools.wraps(func)
        async def asyncWrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except Exception:
                return defaultReturn

        @functools.wraps(func)
        def syncWrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception:
                return defaultReturn

        import asyncio
        if asyncio.iscoroutinefunction(func):
            return asyncWrapper
        else:
            return syncWrapper

    return decorator
