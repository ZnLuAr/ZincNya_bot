"""
utils/errorHandler.py

统一错误处理模块，用于捕获和格式化各类异常。

核心设计：
    - 终端只显示简洁的一行错误摘要
    - 完整 traceback 写入独立的错误日志文件（每天一个）
    - 相同错误自动聚合计数，避免刷屏


================================================================================
主要功能
================================================================================

initErrorHandler(app)
    初始化错误处理系统，注册各类错误处理器：
        - Telegram Bot 错误处理器（handleTelegramError）
        - asyncio 异常处理器（setupAsyncioHandler）
        - 全局未捕获异常处理器（handleUncaughtException）
        - Python logging 拦截器（setupLoggingInterceptor）

logError(errorType, message, exception, context)
    记录错误到日志文件，并在终端显示简洁摘要

setupAsyncioErrorHandler(loop)
    设置 asyncio 事件循环的异常处理器


================================================================================
错误处理流程
================================================================================

错误来源有四种：

1. Telegram Bot 错误（通过 app.add_error_handler 注册）
   - 由 python-telegram-bot 库在处理 update 时捕获
   - 包括消息处理、命令处理等过程中的异常

2. asyncio 异常（通过 loop.set_exception_handler 注册）
   - 捕获异步任务中未处理的异常
   - 包括 Task 异常、Future 异常等

3. 全局未捕获异常（通过 sys.excepthook 注册）
   - 捕获主线程中未被 try-except 处理的异常
   - 作为最后的兜底机制

4. logging 拦截（通过自定义 logging.Handler）
   - 拦截 telegram、httpx、httpcore 库的 ERROR 级别日志
   - 这些库会在网络错误时输出完整 traceback 到 logging
   - 拦截后转换为简洁格式，避免刷屏


================================================================================
支持的错误类型
================================================================================

httpx 网络错误（使用 isinstance 判断）：
    - ConnectError      网络连接失败
    - ConnectTimeout    连接超时
    - ReadTimeout       读取超时
    - WriteTimeout      写入超时
    - PoolTimeout       连接池超时
    - ProxyError        代理服务器错误

telegram API 错误（使用 isinstance 判断）：
    - NetworkError      网络错误（基类）
    - TimedOut          请求超时
    - Forbidden         无权限（被屏蔽、被踢出群组等）
    - BadRequest        请求参数错误
    - RetryAfter        触发频率限制
    - Conflict          多实例冲突
    - InvalidToken      Token 无效
    - TelegramError     其他 Telegram 错误（基类）


================================================================================
错误日志格式
================================================================================

日志文件：log/error_YYYY-MM-DD.log

每条记录格式：
    ══════════════════════════════════════════════════════════════════
    [14:23:45] NetworkError
    Server disconnected without sending a response.
    Context: Logger: telegram.ext._utils.networkloop
    ──────────────────────────────────────────────────────────────────
    Traceback (most recent call last):
      ...完整的堆栈信息...
    ══════════════════════════════════════════════════════════════════


================================================================================
终端显示格式
================================================================================

首次出现：
    刚刚发生了 NetworkError: Server disconnected without sending a response. 喵

重复出现（自动聚合）：
    记录了 NetworkError: Server disconnected... 喵 (今日第 3 次喵)

"""




import os
import sys
import logging
import traceback
from datetime import datetime
from typing import Optional
from collections import defaultdict

from config import LOG_DIR

# 导入异常类型用于 isinstance 判断
from telegram.error import (
    TelegramError,
    NetworkError,
    TimedOut,
    Forbidden,
    BadRequest,
    RetryAfter,
    Conflict,
    InvalidToken,
)
from httpx import (
    ConnectError,
    ConnectTimeout,
    ReadTimeout,
    WriteTimeout,
    PoolTimeout,
    ProxyError,
)




class ErrorHandler:
    """统一错误处理器"""

    def __init__(self):
        self.errorCounts = defaultdict(int)  # 错误计数（按类型+消息）
        self.lastErrorDate = None            # 上次错误的日期（用于重置计数）


    def initialize(self, app=None):
        """
        初始化错误处理系统

        Args:
            app: Telegram Application 对象（可选）
        """
        # 注册全局未捕获异常处理器
        sys.excepthook = self.handleUncaughtException

        # 注册 Telegram Bot 错误处理器
        if app:
            app.add_error_handler(self.handleTelegramError)

        # 配置 logging 拦截器，捕获 httpx/telegram 库的网络错误
        self.setupLoggingInterceptor()

        print("错误处理系统已初始化喵——")


    def getErrorLogPath(self) -> str:
        """获取今日错误日志文件路径"""
        os.makedirs(LOG_DIR, exist_ok=True)
        today = datetime.now().strftime("%Y-%m-%d")
        return os.path.join(LOG_DIR , f"error_{today}.log")


    def resetDailyCountsIfNeeded(self):
        """如果日期变化，重置错误计数"""
        today = datetime.now().date()
        if self.lastErrorDate != today:
            self.errorCounts.clear()
            self.lastErrorDate = today


    def getErrorKey(self , errorType: str , message: str) -> str:
        """生成错误的唯一标识（用于计数）"""
        # 截取消息前 50 个字符作为 key，避免微小差异导致无法聚合
        shortMessage = message[:50] if message else ""
        return f"{errorType}:{shortMessage}"


    def logError(
        self,
        errorType: str,
        message: str,
        exception: Optional[BaseException] = None,
        context: Optional[str] = None
    ):
        """
        记录错误

        Args:
            errorType: 错误类型名称（如 "NetworkError"）
            message: 错误消息
            exception: 原始异常对象（用于获取 traceback）
            context: 额外的上下文信息
        """
        self.resetDailyCountsIfNeeded()

        timestamp = datetime.now().strftime("%H:%M:%S")
        errorKey = self.getErrorKey(errorType, message)
        self.errorCounts[errorKey] += 1
        count = self.errorCounts[errorKey]

        # 终端显示简洁摘要
        shortMessage = message[:60] + "..." if len(message) > 60 else message
        if count > 1:
            print(f"记录了 {errorType}: {shortMessage} 喵 (今日第 {count} 次喵)")
        else:
            print(f"刚刚发生了 {errorType}: {shortMessage} 喵")

        # 写入错误日志文件
        self.writeToLogFile(timestamp , errorType , message , exception , context)


    def writeToLogFile(
        self,
        timestamp: str,
        errorType: str,
        message: str,
        exception: Optional[BaseException],
        context: Optional[str]
    ):
        # 相应地，有：
        # def _formatConsoleText(self , timestamp , userName , action , result , childType: LogChildType)

        """写入错误日志文件"""
        logPath = self.getErrorLogPath()

        lines = [
            "",
            "═" * 70,
            f"[{timestamp}] {errorType}",
            message,
        ]

        if context:
            lines.append(f"Context: {context}")

        if exception:
            lines.append("─" * 70)
            # 获取完整的 traceback
            tbLines = traceback.format_exception(type(exception) , exception , exception.__traceback__)
            lines.extend([line.rstrip() for line in tbLines])

        lines.append("═" * 70)

        try:
            with open(logPath , "a" , encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except Exception as e:
            # 日志写入失败时，至少在终端显示
            print(f"    └─ 错误日志写入失败喵: {e}")


    def handleUncaughtException(self , excType , excValue , excTraceback):
        """处理全局未捕获异常"""
        # 忽略 KeyboardInterrupt
        if issubclass(excType , KeyboardInterrupt):
            sys.__excepthook__(excType , excValue , excTraceback)
            return

        self.logError(
            errorType=excType.__name__,
            message=str(excValue),
            exception=excValue,
            context="Uncaught Exception"
        )


    async def handleTelegramError(self , update , context):
        """处理 Telegram Bot 错误"""
        error = context.error
        errorType = type(error).__name__
        message = str(error)

        # 提取更多上下文信息
        contextInfo = None
        if update:
            if update.effective_user:
                contextInfo = f"User: {update.effective_user.id}"
            elif update.effective_chat:
                contextInfo = f"Chat: {update.effective_chat.id}"

        self.logError(
            errorType=errorType,
            message=message,
            exception=error,
            context=contextInfo
        )


    def setupAsyncioHandler(self, loop):
        """
        设置 asyncio 异常处理器

        Args:
            loop: asyncio 事件循环
        """
        def handleAsyncioException(loop, context):
            exception = context.get("exception")
            message = context.get("message" , "Unknown async error")

            if exception:
                self.logError(
                    errorType=type(exception).__name__,
                    message=str(exception),
                    exception=exception,
                    context=f"Asyncio: {message}"
                )
            else:
                self.logError(
                    errorType="AsyncioError",
                    message=message,
                    context="Asyncio Loop"
                )

        loop.set_exception_handler(handleAsyncioException)


    def setupLoggingInterceptor(self):
        """
        配置 logging 拦截器

        python-telegram-bot 和 httpx 使用 Python 标准 logging 模块输出错误。
        这里创建一个自定义 handler 来拦截这些日志，转换为简洁格式输出。
        """

        class TelegramLoggingHandler(logging.Handler):
            """自定义 logging handler，拦截 telegram/httpx 的错误日志"""

            def __init__(self, errorHandler):
                super().__init__()
                self._errorHandler = errorHandler

            def emit(self, record):
                # 只处理 ERROR 及以上级别
                if record.levelno < logging.ERROR:
                    return

                # 提取错误信息
                message = record.getMessage()

                # 如果有异常信息，使用 isinstance 进行类型判断
                if record.exc_info:
                    _, excValue, _ = record.exc_info
                    if excValue:
                        errorType = type(excValue).__name__
                        excMessage = str(excValue)

                        # 使用 isinstance 进行类型判断，更可靠
                        # httpx 超时类错误
                        if isinstance(excValue, (ConnectTimeout, ReadTimeout, WriteTimeout, PoolTimeout)):
                            message = excMessage or "请求超时"
                        # httpx 连接类错误
                        elif isinstance(excValue, ConnectError):
                            message = excMessage or "网络连接失败"
                        elif isinstance(excValue, ProxyError):
                            message = excMessage or "代理服务器错误"
                        # telegram 超时错误
                        elif isinstance(excValue, TimedOut):
                            message = excMessage or "请求超时"
                        # telegram 网络错误
                        elif isinstance(excValue, NetworkError):
                            message = excMessage or "网络错误"
                        # telegram API 错误
                        elif isinstance(excValue, Forbidden):
                            message = excMessage or "没有权限喵（可能被用户屏蔽了）"
                        elif isinstance(excValue, BadRequest):
                            message = excMessage or "请求参数错误"
                        elif isinstance(excValue, RetryAfter):
                            message = excMessage or "操作过于频繁，触发频率限制"
                        elif isinstance(excValue, Conflict):
                            message = excMessage or "同时存在多个 bot 实例，出现冲突"
                        elif isinstance(excValue, InvalidToken):
                            message = excMessage or "Bot Token 无效"
                        # 其他 telegram 错误
                        elif isinstance(excValue, TelegramError):
                            message = excMessage or "Telegram API 错误"
                        else:
                            message = excMessage

                        self._errorHandler.logError(
                            errorType=errorType,
                            message=message,
                            exception=excValue,
                            context=f"Logger: {record.name}"
                        )
                        return

                # 没有异常信息时，使用日志记录器名称作为类型
                errorType = record.name.split(".")[-1] if record.name else "LogError"
                self._errorHandler.logError(
                    errorType=errorType,
                    message=message,
                    context=f"Logger: {record.name}"
                )

        # 创建自定义 handler
        handler = TelegramLoggingHandler(self)
        handler.setLevel(logging.ERROR)

        # 拦截 telegram 和 httpx 相关的日志
        for loggerName in ["telegram", "httpx", "httpcore"]:
            logger = logging.getLogger(loggerName)
            logger.addHandler(handler)
            # 阻止日志向上传播到 root logger（避免重复输出）
            logger.propagate = False
            # 设置日志级别，确保能捕获 ERROR
            if logger.level == logging.NOTSET or logger.level > logging.ERROR:
                logger.setLevel(logging.ERROR)




# 创建全局单例
errorHandler = ErrorHandler()


# 暴露接口
def initErrorHandler(app=None):
    """初始化错误处理系统"""
    errorHandler.initialize(app)


def logError(errorType: str , message: str , exception=None , context=None):
    """记录错误"""
    errorHandler.logError(errorType , message , exception , context)


def setupAsyncioErrorHandler(loop):
    """设置 asyncio 异常处理器"""
    errorHandler.setupAsyncioHandler(loop)
