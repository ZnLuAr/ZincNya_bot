"""
utils/errorHandler.py

统一错误处理模块，用于捕获和格式化各类异常。

核心设计：
    - 终端只显示简洁的一行错误摘要
    - 完整 traceback 写入独立的错误日志文件（每天一个）


================================================================================
主要功能
================================================================================

initErrorHandler(app)
    初始化错误处理系统，注册各类错误处理器：
        - Telegram Bot 错误处理器
        - asyncio 异常处理器
        - 全局未捕获异常处理器

logError(errorType, message, exception)
    记录错误到日志文件，并在终端显示简洁摘要


================================================================================
错误日志格式
================================================================================

日志文件：log/error_YYYY-MM-DD.log

每条记录格式：
    ══════════════════════════════════════════════════════════════════
    [14:23:45] NetworkError
    Server disconnected without sending a response.
    ----------------------------------------------------------------------
    Traceback (most recent call last):
      ...完整的堆栈信息...
    ══════════════════════════════════════════════════════════════════


================================================================================
终端显示格式
================================================================================

简洁模式（默认）：
    ⚠️ NetworkError: Server disconnected without sending a response.

相同错误聚合：
    ⚠️ NetworkError: Server disconnected... (今日第 3 次)

"""




import os
import sys
import traceback
from datetime import datetime
from typing import Optional
from collections import defaultdict

from config import LOG_DIR




class ErrorHandler:
    """统一错误处理器"""

    def __init__(self):
        self._errorCounts = defaultdict(int)  # 错误计数（按类型+消息）
        self._lastErrorDate = None            # 上次错误的日期（用于重置计数）


    def initialize(self, app=None):
        """
        初始化错误处理系统

        Args:
            app: Telegram Application 对象（可选）
        """
        # 注册全局未捕获异常处理器
        sys.excepthook = self._handleUncaughtException

        # 注册 Telegram Bot 错误处理器
        if app:
            app.add_error_handler(self._handleTelegramError)

        print("错误处理系统已初始化喵——")


    def _getErrorLogPath(self) -> str:
        """获取今日错误日志文件路径"""
        os.makedirs(LOG_DIR, exist_ok=True)
        today = datetime.now().strftime("%Y-%m-%d")
        return os.path.join(LOG_DIR , f"error_{today}.log")


    def _resetDailyCountsIfNeeded(self):
        """如果日期变化，重置错误计数"""
        today = datetime.now().date()
        if self._lastErrorDate != today:
            self._errorCounts.clear()
            self._lastErrorDate = today


    def _getErrorKey(self , errorType: str , message: str) -> str:
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
        self._resetDailyCountsIfNeeded()

        timestamp = datetime.now().strftime("%H:%M:%S")
        errorKey = self._getErrorKey(errorType, message)
        self._errorCounts[errorKey] += 1
        count = self._errorCounts[errorKey]

        # 终端显示简洁摘要
        shortMessage = message[:60] + "..." if len(message) > 60 else message
        if count > 1:
            print(f"记录了 {errorType}: {shortMessage} 喵 (今日第 {count} 次喵)")
        else:
            print(f"刚刚发生了 {errorType}: {shortMessage} 喵")

        # 写入错误日志文件
        self._writeToLogFile(timestamp , errorType , message , exception , context)


    def _writeToLogFile(
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
        logPath = self._getErrorLogPath()

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


    def _handleUncaughtException(self , excType , excValue , excTraceback):
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


    async def _handleTelegramError(self , update , context):
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




# 创建全局单例
_errorHandler = ErrorHandler()


# 暴露接口
def initErrorHandler(app=None):
    """初始化错误处理系统"""
    _errorHandler.initialize(app)


def logError(errorType: str , message: str , exception=None , context=None):
    """记录错误"""
    _errorHandler.logError(errorType , message , exception , context)


def setupAsyncioErrorHandler(loop):
    """设置 asyncio 异常处理器"""
    _errorHandler.setupAsyncioHandler(loop)
