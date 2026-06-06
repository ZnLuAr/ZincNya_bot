"""
utils/llm/state.py

LLM 运行时状态管理：
    - 多类型审核队列（reply + memory，console / chatScreen 共用；
      reply item 会携带 urlContexts 供 retry 复用）
    - 队首预览（peekReviewHint，供 chatScreen 状态栏显示）
    - 每用户速率限制
    - 消息防抖缓冲（聚合短时间内分多次发送的消息，按 dict 记录
      text / includeContext / images / urlIntentText / urlCandidateText）
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
_pendingMessages: dict[str, list[dict]] = {}   # debounceKey -> [{"text": str, "includeContext": bool, "images": list, "urlIntentText": str, "urlCandidateText": str}]
_pendingTasks: dict[str, asyncio.Task] = {}   # debounceKey -> 当前防抖 Task

# 全局 one-shot context 标记：下一次 LLM 调用强制带记忆，触发后自动清除
_contextOnce: bool = False




def getReviewQueue() -> asyncio.Queue:
    """获取审核队列"""
    return _llmReviewQueue


def _formatReviewHint(item: dict) -> str:
    kind = item.get("kind", "reply")
    if kind == "memory":
        action = item.get("action", {})
        actionType = action.get("action", "?")
        content = action.get("content") or action.get("originalContent") or ""
        memoryID = action.get("memoryID")
        if content:
            # 转义换行符，避免底边栏被截断
            escaped = content.replace('\n', '\\n')
            preview = escaped[:16] + "…" if len(escaped) > 16 else escaped
        elif memoryID is not None:
            preview = f"#{memoryID}"
        else:
            preview = ""
        return f"当前操作的是：[记忆:{actionType}] {preview}" if preview else f"当前操作的是：[记忆:{actionType}]"

    reply = item.get("reply") or ""
    # 转义换行符，避免底边栏被截断
    escaped = reply.replace('\n', '\\n')
    preview = escaped[:16] + "…" if len(escaped) > 16 else escaped
    return f"当前操作的是：[回复] {preview}" if preview else "当前操作的是：[回复]"


def peekReviewHint() -> str | None:
    """
    窥视队首审核项，返回类型+内容预览字符串。
    队列为空时返回 None。不消费队列项。
    """
    # asyncio.Queue 内部使用 collections.deque
    if not _llmReviewQueue._queue:
        return None
    return _formatReviewHint(_llmReviewQueue._queue[0])




def makeReplyReviewItem(
    *,
    chatID: str,
    messageID: int,
    originalMsg: str,
    reply: str,
    opsID: str,
    userID: str | int | None = None,
    includeContext: bool = False,
    urlContexts: list[dict] | None = None,
) -> dict:
    return {
        "kind": "reply",
        "chatID": chatID,
        "messageID": messageID,
        "originalMsg": originalMsg,
        "reply": reply,
        "opsID": opsID,
        "userID": str(userID) if userID is not None else None,
        "includeContext": includeContext,
        "urlContexts": urlContexts or [],
    }


def addReviewItem(
    chatID: str,
    messageID: int,
    originalMsg: str,
    reply: str,
    opsID: str,
    userID: str | int | None = None,
    includeContext: bool = False,
    urlContexts: list[dict] | None = None,
):
    """
    将待审核消息加入队列

    参数:
        chatID: 原始聊天 ID
        messageID: 原始消息 ID
        originalMsg: 用户发送的原始消息
        reply: LLM 生成的回复
        opsID: 审核通知发送给哪个 ops
        urlContexts: URL 抓取结果列表
    """
    _llmReviewQueue.put_nowait(makeReplyReviewItem(
        chatID=chatID,
        messageID=messageID,
        originalMsg=originalMsg,
        reply=reply,
        opsID=opsID,
        userID=userID,
        includeContext=includeContext,
        urlContexts=urlContexts,
    ))


def makeMemoryReviewItem(
    *,
    action: dict,
    chatID: str,
    originalMsg: str,
    opsID: str,
    userID: str | int | None = None,
) -> dict:
    return {
        "kind": "memory",
        "action": action,
        "chatID": chatID,
        "originalMsg": originalMsg,
        "opsID": opsID,
        "userID": str(userID) if userID is not None else None,
    }


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
    _llmReviewQueue.put_nowait(makeMemoryReviewItem(
        action=action,
        chatID=chatID,
        originalMsg=originalMsg,
        opsID=opsID,
        userID=userID,
    ))




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


def appendPendingMessage(
    debounceKey: str,
    text: str,
    includeContext: bool = False,
    images: list[dict] | None = None,
    urlIntentText: str | None = None,
    urlCandidateText: str | None = None,
) -> bool:
    """
    将消息追加到防抖缓冲区。

    参数:
        images: 图片列表 [{"data": b64_str, "mimeType": "..."}, ...]
        urlIntentText: 当前用户消息文本，用于判断 URL 读取意图
        urlCandidateText: 当前用户消息 + 被回复消息文本，用于提取 URL

    返回 False 表示已达上限，消息被丢弃。
    """
    buf = _pendingMessages.setdefault(debounceKey, [])
    if len(buf) >= LLM_PENDING_MSG_LIMIT:
        return False
    buf.append({
        "text": text,
        "includeContext": includeContext,
        "images": images or [],
        "urlIntentText": urlIntentText or "",
        "urlCandidateText": urlCandidateText or "",
    })
    return True


def popPendingMessages(debounceKey: str) -> list[dict]:
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
