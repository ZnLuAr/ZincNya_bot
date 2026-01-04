"""
utils/logger.py

树状日志记录系统，为 ZincNya Bot 提供统一的日志输出与文件记录能力。

该模块的核心设计是以"树状结构"展示日志的层级关系，使得控制台输出更加直观易读。
模块由两部分组成：TreeLogger 类（核心实现）与向后兼容的函数接口。


================================================================================
核心类：TreeLogger

TreeLogger 是单例模式的日志记录器，负责管理日志文件的创建、写入，以及控制台的格式化输出。

主要功能：
    1. 初始化日志系统（initialize）
        - 自动按日期创建日志文件，每天的第 N 次启动会生成 log_YYYY-MM-DD_NN.log
        - 在控制台和文件中记录启动时间戳

    2. 记录日志（log）
        - 接受用户对象、操作描述、操作结果、子节点类型
        - 根据子节点类型格式化为树状结构输出
        - 异步写入文件，避免阻塞主线程

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

在旧版本，树状输出的格式是由字符串决定的，这样很容易发生拼写错误。
现在把它改成了枚举。

================================================================================
向后兼容接口

模块提供了两个函数接口，保持与旧代码的兼容性：

    initLogger()
        - 初始化日志系统，在 bot.py 启动时调用

    logAction(user, action, result, child, writeInLog=True)
        - 记录操作日志
        - child 参数支持旧的字符串格式（如 "withChild"）或新的枚举
        - 旧字符串会自动转换为对应的 LogChildType 枚举


================================================================================
使用示例

# 方式 1：使用旧的字符串参数（向后兼容）
await logAction(
    update.effective_user,
    "使用 /findsticker 寻找表情包",
    "OK喵",
    "withChild"
)

# 方式 2：使用新的枚举类型（推荐）
from utils.logger import logAction, LogChildType

await logAction(
    update.effective_user,
    "使用 /findsticker 寻找表情包",
    "OK喵",
    LogChildType.WITH_CHILD
)


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


================================================================================
向后兼容映射表

旧字符串参数会自动映射为新枚举：
    "False"               -> LogChildType.NONE
    "withChild"           -> LogChildType.WITH_CHILD
    "withOneChild"        -> LogChildType.WITH_ONE_CHILD
    "childWithChild"      -> LogChildType.CHILD_WITH_CHILD
    "lastChildWithChild"  -> LogChildType.LAST_CHILD_WITH_CHILD
    "lastChild"           -> LogChildType.LAST_CHILD
    "True"                -> LogChildType.ONLY_RESULT

这确保了现有代码无需修改即可继续正常工作。
"""




import os
from datetime import datetime
from enum import Enum
import asyncio
from config import LOG_DIR, LOG_FILE_TEMPLATE





class LogChildType(str, Enum):
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


    def initialize(self):
        """初始化日志系统（主程序启动时调用）"""
        self._logPath = self._getTodayLogPath()

        with open(self._logPath , "a" , encoding="utf-8") as f:
            f.write(f"[{datetime.now().strftime('%H:%M:%S')}] ZincNya Bot—— 喵的一声，就启动啦——\n")

        print(f"\n\n锌酱、现在就要创建日志了喵——✍\n   · {self._logPath}，\nこれからですよにゃー\n")


    def _getTodayLogPath(self):
        """生成当天的日志文件路径，并计算本次开机为本日第几次开机"""
        os.makedirs(LOG_DIR , exist_ok=True)
        today = datetime.now().strftime("%Y-%m-%d")

        # 找出今天已经有多少个日志
        existingLogs = [
            f for f in os.listdir(LOG_DIR)
            if f.startswith(f"log_{today}")
        ]

        index = len(existingLogs) + 1
        logFileName = LOG_FILE_TEMPLATE.format(date=today , index=index)

        return os.path.join(LOG_DIR , logFileName)


    async def log(self , user , action , result , childType: LogChildType|str , writeInLog=True):
        """
        记录日志

        Args:
            user: 用户对象或用户名字符串
            action: 操作描述
            result: 操作结果
            childType: 子节点类型（LogChildType 枚举或字符串）
            writeInLog: 是否写入日志文件
        """
        if not action and not result:
            return

        # 当不存在日志时，初始化
        if self._logPath is None:
            self.initialize()

        # 兼容旧的字符串参数
        if isinstance(childType, str):
            childType = self._convertLegacyChildType(childType)

        timestamp = datetime.now().strftime("%H:%M:%S")
        userName = self._extractUserName(user)

        # 格式化控制台输出
        consoleText = self._formatConsoleText(timestamp, userName, action, result, childType)

        # 格式化日志文件输出
        logLine = f"[{timestamp}] @{userName}：{action}\n[{timestamp}] @锌酱：Result：{result}\n"

        if writeInLog:
            try:
                await asyncio.to_thread(self._writeLogSync, consoleText, logLine)
            except Exception as e:
                print(f"[{timestamp}] @锌酱：咦？日志写入失败了喵……？\n             └─┤ 报错在这里——{e}")
        else:
            print(consoleText)


    def _formatConsoleText(self, timestamp, userName, action, result, childType: LogChildType):
        """根据子节点类型格式化控制台输出"""
        match childType:
            case LogChildType.NONE:
                return f"[{timestamp}] @{userName}：{action}\n               └─┤ {result}\n\n"

            case LogChildType.WITH_CHILD:
                return f"[{timestamp}] @{userName}：{action}\n               └─┤ {result}"

            case LogChildType.WITH_ONE_CHILD:
                return f"[{timestamp}] @{userName}：{action}\n               └─┤ {result}\n\n"

            case LogChildType.CHILD_WITH_CHILD:
                return f"                   └─┤ {action}\n                         └─┤ {result}"

            case LogChildType.LAST_CHILD_WITH_CHILD:
                return f"                   └─┤ {action}\n                         └─┤ {result}\n\n"

            case LogChildType.LAST_CHILD:
                return f"                   └─┤ {result}\n\n"

            case LogChildType.ONLY_RESULT:
                return f"                   └─┤ {result}"


    def _convertLegacyChildType(self, oldType: str) -> LogChildType:
        """将旧的字符串类型转换为新的枚举类型（向后兼容）"""
        mapping = {
            "False": LogChildType.NONE,
            "withChild": LogChildType.WITH_CHILD,
            "withOneChild": LogChildType.WITH_ONE_CHILD,
            "childWithChild": LogChildType.CHILD_WITH_CHILD,
            "lastChildWithChild": LogChildType.LAST_CHILD_WITH_CHILD,
            "lastChild": LogChildType.LAST_CHILD,
            "True": LogChildType.ONLY_RESULT,
        }
        return mapping.get(oldType, LogChildType.NONE)


    def _writeLogSync(self, consoleText, logLine):
        """同步写入日志"""
        with open(self._logPath, "a", encoding="utf-8") as f:
            f.write(logLine)
        print(consoleText)


    def _extractUserName(self, user):
        """提取用户名（支持多种输入类型）"""
        if user is None:
            return "Bot"
        if isinstance(user, str):
            return user.strip() or "Unknown"
        if isinstance(user, dict):
            return user.get("username") or user.get("first_name") or "Unknown"

        userName = getattr(user, "username", None)
        if userName:
            return userName

        first = getattr(user, "first_name", None)
        if first:
            return first

        return "Unknown"





# 创建全局单例
_logger = TreeLogger()


# 向后兼容的函数接口
def initLogger():
    """初始化日志系统（主程序启动时调用）"""
    _logger.initialize()




async def logAction(user, action, result, child, writeInLog=True):
    """
    记录操作日志（向后兼容的函数接口）

    Args:
        user: 用户对象或用户名
        action: 操作描述
        result: 操作结果
        child: 子节点类型（支持旧字符串或新枚举）
        writeInLog: 是否写入日志文件
    """
    await _logger.log(user, action, result, child, writeInLog)
