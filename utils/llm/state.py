"""
utils/llm/state.py

LLM 运行时状态管理：
    - 待审核消息队列容器（console / chatScreen 共用；item 契约与入队操作在
      utils/llm/review.py——队列项是审核域契约，生产端与消费端同文件演进）
    - 每用户速率限制
    - 消息防抖缓冲（短时间内的原始消息按顺序暂存，窗口结束后一次取出）
    - 全局 one-shot context 标记（memory -once；下一批同时启用 memory/history）
"""

import time
import asyncio

from config import LLM_RATE_LIMIT_SECONDS, LLM_PENDING_MSG_LIMIT

from utils.llm.models import MessageInput




# 待审核消息队列（auto -console 模式时使用；item 契约在 utils/llm/review.py）
_llmReviewQueue: asyncio.Queue = asyncio.Queue()

# 每用户最后调用时间 {userID: timestamp}
_lastCallTime: dict[str, float] = {}

# 消息防抖状态（聚合短时间内分多次发送的消息）
_pendingMessages: dict[str, list[MessageInput]] = {}
_pendingTasks: dict[str, asyncio.Task] = {}   # debounceKey -> 当前防抖 Task

# 全局 one-shot context 标记：下一批 LLM 调用强制带 memory/history，触发后自动清除
_contextOnce: bool = False




def getReviewQueue() -> asyncio.Queue:
    """获取审核队列"""
    return _llmReviewQueue


def isRateLimited(userID: str | int) -> bool:
    """
    检查用户是否在速率限制冷却中

    调用时会自动清理过期的记录，防止内存泄漏。
    """
    userID = str(userID)
    cutoff = time.time() - LLM_RATE_LIMIT_SECONDS

    # 清理超过冷却时间的过期记录
    expired = [k for k, v in _lastCallTime.items() if v <= cutoff]
    for k in expired:
        del _lastCallTime[k]

    return _lastCallTime.get(userID, 0) > cutoff




def setContextOnce():
    """让下一次防抖批次同时加载 memory 与 history context。"""
    global _contextOnce
    _contextOnce = True


def consumeContextOnce() -> bool:
    """消费全局 one-shot context 标记，返回是否已设置并同时清除"""
    global _contextOnce
    if _contextOnce:
        _contextOnce = False
        return True
    return False


def isContextOnceSet() -> bool:
    """查询 one-shot context 标记是否已设置（只读，不消费）"""
    return _contextOnce




# ============================================================================
# 防抖聚合工具
# ============================================================================

def addRateLimit(userID: str | int):
    """记录用户的调用时间（触发速率限制）"""
    _lastCallTime[str(userID)] = time.time()


def makeDebounceKey(chatID: str | int, userID: str | int) -> str:
    """构造防抖键：同一用户在不同 chat 中分别聚合"""
    return f"{chatID}:{userID}"


def appendPendingMessage(
    debounceKey: str,
    message: MessageInput,
) -> bool:
    """将原样消息追加到防抖缓冲；返回 False 表示已达上限。"""
    buf = _pendingMessages.setdefault(debounceKey, [])
    if len(buf) >= LLM_PENDING_MSG_LIMIT:
        return False
    buf.append(message)
    return True


def popPendingMessages(debounceKey: str) -> tuple[MessageInput, ...]:
    """取出并清空该防抖键对应的待聚合消息列表"""
    return tuple(_pendingMessages.pop(debounceKey, []))


def getPendingTask(debounceKey: str) -> asyncio.Task | None:
    """获取当前防抖任务"""
    return _pendingTasks.get(debounceKey)


def setPendingTask(debounceKey: str, task: asyncio.Task):
    """设置防抖任务"""
    _pendingTasks[debounceKey] = task


def clearPendingTask(debounceKey: str, task: asyncio.Task | None = None):
    """清除防抖任务记录（若传入任务，仅在匹配时清除，避免误删替换后的新任务）"""
    if task is None or _pendingTasks.get(debounceKey) is task:
        _pendingTasks.pop(debounceKey, None)
