"""
utils/logger.py

树状日志记录系统，为 ZincNya Bot 提供统一的日志输出与文件记录能力。

该模块的核心设计是以"树状结构"展示日志的层级关系，使得控制台输出更加直观易读。
模块由两部分组成：TreeLogger 类（核心实现）与函数接口。


================================================================================
核心类：TreeLogger

TreeLogger 是单例模式的日志记录器，负责管理日志文件的创建、写入，以及控制台的格式化输出。

主要功能：
    1. 初始化日志系统（initialize）
        - 每天一个日志文件：log_YYYY-MM-DD.log
        - 不同启动之间用分隔线区分
        - 空启动（没有任何操作）不会写入日志

    2. 记录日志（log）
        - 接受用户对象、操作描述、操作结果、子节点类型
        - 根据子节点类型格式化为树状结构输出
        - 异步写入文件，避免阻塞主线程
        - 首次写入时自动添加启动分隔符

    3. 提取用户名（_extractUserName）
        - 支持多种输入类型：Telegram User 对象、字符串、字典、None
        - 优先级：username > first_name > "Unknown"


================================================================================
枚举类：LogChildType

定义了日志的 7 种树状输出格式：

    NONE                  - 无子节点，打印完后换行
    WITH_CHILD            - 有子节点，不换行
    WITH_ONE_CHILD        - 有一个子节点后换行
    CHILD_WITH_CHILD      - 子节点还有子节点
    LAST_CHILD_WITH_CHILD - 最后的子节点还有子节点
    LAST_CHILD            - 最后的子节点
    ONLY_RESULT           - 仅结果（二级输出）


================================================================================
函数接口

    initLogger()
        - 初始化日志系统，在 bot.py 启动时调用

    logAction(user, event, details, level, childType, writeInLog=True)
        - 记录用户操作日志

    logSystemEvent(event, details, level, childType, ...)
        - 记录系统内部事件（数据库、文件 I/O、API 等）


================================================================================
树状输出示例

使用 WITH_CHILD + LAST_CHILD 时的输出效果：

[14:23:45] @ZincPhos：使用 /findsticker 寻找表情包
               └─┤ OK喵
                   └─┤ 找到表情包 cute_cats

使用 CHILD_WITH_CHILD 时的输出效果：

[14:23:45] @ZincPhos：使用 /findsticker 寻找表情包
               └─┤ OK喵
                   └─┤ 开始下载
                         └─┤ 成功 42 张，失败 0 张
"""




import os
from datetime import datetime
from enum import Enum
from typing import Optional
import asyncio
from config import LOG_DIR




class LogLevel(str, Enum):
    """日志级别"""
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class LogChildType(str , Enum):
    """日志子节点类型"""
    NONE = "none"                                               # 无子节点，打印空行
    WITH_CHILD = "with_child"                                   # 有子节点
    WITH_ONE_CHILD = "with_one_child"                           # 有一个子节点后换行
    CHILD_WITH_CHILD = "child_with_child"                       # 子节点还有子节点
    LAST_CHILD_WITH_CHILD = "last_child_with_child"             # 最后的子节点还有子节点
    LAST_CHILD = "last_child"                                   # 最后的子节点
    ONLY_RESULT = "only_result"                                 # 仅结果






class TreeLogger:
    """树状日志记录器"""

    def __init__(self):
        self._logPath = None
        self._startupTime = None          # 启动时间
        self._startupWritten = False      # 本次启动是否已写入日志
        self._hasContent = False          # 本次启动是否有实际内容


    def initialize(self):
        """
        初始化日志系统（主程序启动时调用）

        注意：不再立即写入启动信息，而是延迟到首次有实际日志时才写入。
        这样可以避免空启动（没有任何操作）污染日志文件。
        """
        self._logPath = self._getTodayLogPath()
        self._startupTime = datetime.now()
        self._startupWritten = False
        self._hasContent = False

        print(f"\n\n锌酱、现在就要创建日志了喵—— ✍\n   · {self._logPath}，\nこれからですよにゃー\n")


    def _getTodayLogPath(self):
        """生成当天的日志文件路径（每天一个文件）"""
        os.makedirs(LOG_DIR , exist_ok=True)
        today = datetime.now().strftime("%Y-%m-%d")
        return os.path.join(LOG_DIR , f"log_{today}.log")


    def _writeStartupHeader(self):
        """写入启动分隔符和启动信息（仅在首次有实际日志时调用）"""
        if self._startupWritten:
            return

        self._startupWritten = True
        timestamp = self._startupTime.strftime("%H:%M:%S")

        # 检查文件是否已存在且有内容（决定是否需要分隔符）
        needSeparator = os.path.exists(self._logPath) and os.path.getsize(self._logPath) > 0

        with open(self._logPath, "a", encoding="utf-8") as f:
            if needSeparator:
                f.write(f"\n\n{'─' * 60}\n\n")
            f.write(f"[{timestamp}] ZincNya Bot—— 喵的一声，就启动啦——\n")


    async def log(
        self,
        user,
        event: str,           # 原 action
        details: str,         # 原 result
        level: LogLevel,      # 新增
        childType: LogChildType,
        writeInLog=True,
        exception: Optional[Exception] = None  # 新增，用于错误日志
    ):
        """
        记录日志

        Args:
            user: 用户对象或用户名字符串
            event: 事件描述（发生了什么）
            details: 补充信息（具体细节）
            level: 日志级别（LogLevel.INFO/WARNING/ERROR）
            childType: 子节点类型（LogChildType 枚举或字符串）
            writeInLog: 是否写入日志文件
            exception: 异常对象（用于错误日志双写）
        """
        if not event and not details:
            return

        # 当不存在日志时，初始化
        if self._logPath is None:
            self.initialize()

        timestamp = datetime.now().strftime("%H:%M:%S")
        userName = self._extractUserName(user)

        # 如果有异常，追加异常信息到 details
        if exception:
            exceptionInfo = f"{type(exception).__name__}: {str(exception)}"
            if details:
                details = f"{details} ({exceptionInfo})"
            else:
                details = exceptionInfo

        # 格式化控制台输出（保持树状结构）
        consoleText = self._formatConsoleText(timestamp, userName, event, details, level, childType)

        # 格式化日志文件输出（单行格式）
        logLine = self._formatLogLine(timestamp, userName, event, details, level)

        if writeInLog:
            try:
                await asyncio.to_thread(self._writeLogSync, consoleText, logLine)

                # 如果是 ERROR 级别且有异常对象，双写到错误日志
                if level == LogLevel.ERROR and exception is not None:
                    from utils.errorHandler import logError
                    logError(
                        errorType=type(exception).__name__,
                        message=str(exception),
                        exception=exception,
                        context=f"User: {userName}, Event: {event}"
                    )
            except Exception as e:
                print(f"[{timestamp}] [{level.value}] @锌酱：咦？日志写入失败了喵……？\n             └─┤ 报错在这里——{e}")
        else:
            print(consoleText)


    def _formatConsoleText(self, timestamp, userName, event, details, level: LogLevel, childType: LogChildType):
        """根据子节点类型格式化控制台输出"""
        match childType:
            case LogChildType.NONE:
                return f"[{timestamp}] [{level.value}] @{userName}: {event}\n                   └─┤ {details}\n\n"

            case LogChildType.WITH_CHILD:
                return f"[{timestamp}] [{level.value}] @{userName}: {event}\n                   └─┤ {details}"

            case LogChildType.WITH_ONE_CHILD:
                return f"[{timestamp}] [{level.value}] @{userName}: {event}\n                   └─┤ {details}\n\n"

            case LogChildType.CHILD_WITH_CHILD:
                return f"                   └─┤ {event}\n                         └─┤ {details}"

            case LogChildType.LAST_CHILD_WITH_CHILD:
                return f"                   └─┤ {event}\n                         └─┤ {details}\n\n"

            case LogChildType.LAST_CHILD:
                return f"                   └─┤ {details}\n\n"

            case LogChildType.ONLY_RESULT:
                return f"                   └─┤ {details}"


    def _formatLogLine(self, timestamp, userName, event, details, level: LogLevel):
        """生成单行日志格式"""
        if event and details:
            return f"[{timestamp}] [{level.value}] @{userName}: {event} → {details}\n"
        elif event:
            return f"[{timestamp}] [{level.value}] @{userName}: {event}\n"
        elif details:
            return f"[{timestamp}] [{level.value}] @{userName}: → {details}\n"
        else:
            return ""




    def _writeLogSync(self , consoleText , logLine):
        """同步写入日志"""
        # 首次写入时，先写入启动分隔符和启动信息
        self._writeStartupHeader()

        with open(self._logPath, "a", encoding="utf-8") as f:
            f.write(logLine)
        print(consoleText)


    def _extractUserName(self , user):
        """提取用户名（支持多种输入类型）"""
        if user is None:
            return "System"  # 修改：原来返回 "Bot"，现在统一为 "System"
        if isinstance(user , str):
            return user.strip() or "Unknown"
        if isinstance(user , dict):
            return user.get("username") or user.get("first_name") or "Unknown"

        userName = getattr(user , "username" , None)
        if userName:
            return userName

        first = getattr(user , "first_name" , None)
        if first:
            return first

        return "Unknown"





# 创建全局单例
_logger = TreeLogger()


# 向后兼容的函数接口
def initLogger():
    """初始化日志系统（主程序启动时调用）"""
    _logger.initialize()




async def logAction(user, event, details, level, childType, writeInLog=True):
    """
    记录操作日志（函数接口）

    Args:
        user: 用户对象或用户名
        event: 事件描述（发生了什么）
        details: 补充信息（具体细节）
        level: 日志级别（LogLevel.INFO/WARNING/ERROR）
        childType: 子节点类型（支持旧字符串或新枚举）
        writeInLog: 是否写入日志文件
    """
    await _logger.log(user, event, details, level, childType, writeInLog)




async def logSystemEvent(
    event: str,
    details: str = "",
    level: LogLevel = LogLevel.INFO,
    childType: LogChildType = LogChildType.NONE,
    writeToFile: bool = True,
    exception: Optional[Exception] = None
):
    """
    记录系统内部事件

    专门用于内部系统事件（数据库操作、文件 I/O、API 调用等），
    自动使用 user="System"，支持树状结构输出。

    Args:
        event: 事件描述（发生了什么）
        details: 补充信息（具体细节）
        level: 日志级别（LogLevel.INFO/WARNING/ERROR）
        childType: 树状结构类型（默认 NONE 为单行输出）
        writeToFile: 是否写入日志文件
        exception: 异常对象（用于错误日志双写）

    示例：
        await logSystemEvent("聊天记录归档成功", f"Chat {chatID}: {count} 条")
        await logSystemEvent("归档失败", f"Chat {chatID}", LogLevel.ERROR, exception=e)
        await logSystemEvent("开始归档", "", LogLevel.INFO, LogChildType.WITH_CHILD)
    """
    await _logger.log(
        user="System",
        event=event,
        details=details,
        level=level,
        childType=childType,
        writeInLog=writeToFile,
        exception=exception
    )
