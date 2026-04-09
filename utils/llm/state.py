"""
utils/llm/state.py

LLM 运行时状态管理：
    - 多类型审核队列（reply + memory，console / chatScreen 共用）
    - 队首预览（peekReviewHint，供 chatScreen 状态栏显示）
    - 每用户速率限制
    - 消息防抖缓冲（聚合短时间内分多次发送的消息）
    - 全局 one-shot context 标记（memory -once）
"""




import time
import asyncio

from config import LLM_RATE_LIMIT_SECONDS, LLM_PENDING_MSG_LIMIT




# 待审核消息队列（auto -console 模式时使用）
_llmReviewQueue: asyncio.Queue = asyncio.Queue()

# 每用户最后调用时间 {userID: timestamp}
_lastCallTime: dict[str, float] = {}

# 消息防抖状态（聚合短时间内分多次发送的消息）
_pendingMessages: dict[str, list[tuple[str, bool]]] = {}   # debounceKey -> [(text, includeContext)]
_pendingTasks: dict[str, asyncio.Task] = {}   # debounceKey -> 当前防抖 Task

# 全局 one-shot context 标记：下一次 LLM 调用强制带记忆，触发后自动清除
_contextOnce: bool = False




def getReviewQueue() -> asyncio.Queue:
    """获取审核队列"""
    return _llmReviewQueue


def peekReviewHint() -> str | None:
    """
    窥视队首审核项，返回类型+内容预览字符串。
    队列为空时返回 None。不消费队列项。
    """
    # asyncio.Queue 内部使用 collections.deque
    if not _llmReviewQueue._queue:
        return None
    item = _llmReviewQueue._queue[0]
    kind = item.get("kind", "reply")
    if kind == "memory":
        action = item.get("action", {})
        actionType = action.get("action", "?")
        content = action.get("content") or action.get("originalContent") or ""
        memoryID = action.get("memoryID")
        if content:
            preview = content[:16] + "…" if len(content) > 16 else content
        elif memoryID is not None:
            preview = f"#{memoryID}"
        else:
            preview = ""
        return f"当前操作的是：[记忆:{actionType}] {preview}" if preview else f"当前操作的是：[记忆:{actionType}]"
    else:
        reply = item.get("reply") or ""
        preview = reply[:16] + "…" if len(reply) > 16 else reply
        return f"当前操作的是：[回复] {preview}" if preview else "当前操作的是：[回复]"




def addReviewItem(
    chatID: str,
    messageID: int,
    originalMsg: str,
    reply: str,
    opsID: str,
    userID: str | int | None = None,
    includeContext: bool = False,
):
    """
    将待审核消息加入队列

    参数:
        chatID: 原始聊天 ID
        messageID: 原始消息 ID
        originalMsg: 用户发送的原始消息
        reply: LLM 生成的回复
        opsID: 审核通知发送给哪个 ops
    """
    _llmReviewQueue.put_nowait({
        "kind": "reply",
        "chatID": chatID,
        "messageID": messageID,
        "originalMsg": originalMsg,
        "reply": reply,
        "opsID": opsID,
        "userID": str(userID) if userID is not None else None,
        "includeContext": includeContext,
    })


def addMemoryReviewItem(
    *,
    action: dict,
    chatID: str,
    originalMsg: str,
    opsID: str,
    userID: str | int | None = None,
):
    """
    将 LLM 记忆操作加入审核队列。

    参数:
        action: MemoryAction 的 dict 形式
        chatID: 原始聊天 ID
        originalMsg: 触发该操作的用户消息
        opsID: 审核通知发送给哪个 ops
        userID: 触发用户 ID
    """
    _llmReviewQueue.put_nowait({
        "kind": "memory",
        "action": action,
        "chatID": chatID,
        "originalMsg": originalMsg,
        "opsID": opsID,
        "userID": str(userID) if userID is not None else None,
    })




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




def addRateLimit(userID: str | int):
    """记录用户的调用时间（触发速率限制）"""
    _lastCallTime[str(userID)] = time.time()




def makeDebounceKey(chatID: str | int, userID: str | int) -> str:
    """构造防抖键：同一用户在不同 chat 中分别聚合。"""
    return f"{chatID}:{userID}"


def appendPendingMessage(debounceKey: str, text: str, includeContext: bool = False) -> bool:
    """
    将消息追加到防抖缓冲区。

    返回 False 表示已达上限，消息被丢弃。
    """
    buf = _pendingMessages.setdefault(debounceKey, [])
    if len(buf) >= LLM_PENDING_MSG_LIMIT:
        return False
    buf.append((text, includeContext))
    return True


def popPendingMessages(debounceKey: str) -> list[tuple[str, bool]]:
    """取出并清空该防抖键对应的待聚合消息列表"""
    return _pendingMessages.pop(debounceKey, [])


def getPendingTask(debounceKey: str) -> asyncio.Task | None:
    """获取当前防抖 Task"""
    return _pendingTasks.get(debounceKey)


def setPendingTask(debounceKey: str, task: asyncio.Task):
    """设置防抖 Task"""
    _pendingTasks[debounceKey] = task


def clearPendingTask(debounceKey: str, task: asyncio.Task | None = None):
    """清除防抖 Task 记录（若传入 task，仅在匹配时清除，避免误删替换后的新 Task）"""
    if task is None or _pendingTasks.get(debounceKey) is task:
        _pendingTasks.pop(debounceKey, None)




def setContextOnce():
    """设置全局 one-shot context 标记"""
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
