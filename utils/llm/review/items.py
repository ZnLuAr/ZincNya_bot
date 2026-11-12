"""
utils/llm/review/items.py

审核项的创建与存取。审核项有两类：回复审核（支持编辑、重试、补充反馈重试）与
LLM 记忆操作审核。它们按审核入口存在两个地方：

    - 控制台 / chatScreen：进程内队列（容器在 utils/llm/state.py 的 getReviewQueue），
      审核项带 kind 字段（"reply" / "memory"）
    - Telegram：bot_data，按审核卡的 message ID 存取，两类审核项用不同的 key 前缀；
      重启后丢失是可接受的
"""

import time

from config import LLM_REVIEW_TTL_SECONDS

from utils.llm.memory.types import MemoryQuery
from utils.llm.models import DisplayBlocks, TaskRequest
from utils.llm.state import getReviewQueue


_HINT_LEN = 16  # chatScreen 状态栏预览截断长度

# bot_data 的 key 前缀：回复审核、记忆审核，以及从审核卡 message ID 反查审核项的索引
_REPLY_KEY_PREFIX = "llm_review_"
_MEMORY_KEY_PREFIX = "llm_memreview_"
_EDIT_INDEX_PREFIX = "llm_editidx_"




# ---------------------------------------------------------------------------
# 控制台队列里的审核项
# ---------------------------------------------------------------------------

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
            preview = escaped[:_HINT_LEN] + "…" if len(escaped) > _HINT_LEN else escaped
        elif memoryID is not None:
            preview = f"#{memoryID}"
        else:
            preview = ""
        return f"当前操作的是：[记忆:{actionType}] {preview}" if preview else f"当前操作的是：[记忆:{actionType}]"

    reply = item.get("reply") or ""
    # 转义换行符，避免底边栏被截断
    escaped = reply.replace('\n', '\\n')
    preview = escaped[:_HINT_LEN] + "…" if len(escaped) > _HINT_LEN else escaped
    return f"当前操作的是：[回复] {preview}" if preview else "当前操作的是：[回复]"


def peekReviewHint() -> str | None:
    """
    窥视队首审核项，返回类型+内容预览字符串。
    队列为空时返回 None。不消费队列项。
    """
    # asyncio.Queue 内部使用 collections.deque
    if not getReviewQueue()._queue:
        return None
    return _formatReviewHint(getReviewQueue()._queue[0])


def makeReplyReviewItem(
    *,
    chatID: str,
    messageID: int,
    originalMsg: str,
    reply: str,
    opsID: str,
    userID: str | int | None = None,
    includeContext: bool = False,
    displayBlocks: DisplayBlocks | None = None,
    memoryQuery: MemoryQuery | None = None,
    request: TaskRequest | None = None,
) -> dict:
    """构造回复审核快照；request 是重试时重新运行任务用的原始请求。"""
    return {
        "kind": "reply",
        "chatID": chatID,
        "messageID": messageID,
        "originalMsg": originalMsg,
        "reply": reply,
        "opsID": opsID,
        "userID": str(userID) if userID is not None else None,
        "includeContext": includeContext,
        "displayBlocks": displayBlocks,
        "memoryQuery": memoryQuery,
        "request": request,
    }


def addReviewItem(
    chatID: str,
    messageID: int,
    originalMsg: str,
    reply: str,
    opsID: str,
    userID: str | int | None = None,
    includeContext: bool = False,
    displayBlocks: DisplayBlocks | None = None,
    memoryQuery: MemoryQuery | None = None,
    request: TaskRequest | None = None,
):
    """
    将待审核消息加入队列

    参数:
        chatID: 原始聊天 ID
        messageID: 原始消息 ID
        originalMsg: 用户发送的原始消息
        reply: LLM 生成的回复
        opsID: 审核通知发送给哪个 ops
        displayBlocks: 审核卡结构化展示块（None 时渲染层走退化路径）
    """
    getReviewQueue().put_nowait(makeReplyReviewItem(
        chatID=chatID,
        messageID=messageID,
        originalMsg=originalMsg,
        reply=reply,
        opsID=opsID,
        userID=userID,
        includeContext=includeContext,
        displayBlocks=displayBlocks,
        memoryQuery=memoryQuery,
        request=request,
    ))


def makeMemoryReviewItem(
    *,
    action: dict,
    chatID: str,
    originalMsg: str,
    opsID: str,
    userID: str | int | None = None,
    displayBlocks: DisplayBlocks | None = None,
) -> dict:
    """构造 memory 审核项；``action`` 可包含批准时使用的目标状态快照。"""
    return {
        "kind": "memory",
        "action": action,
        "chatID": chatID,
        "originalMsg": originalMsg,
        "opsID": opsID,
        "userID": str(userID) if userID is not None else None,
        "displayBlocks": displayBlocks,
    }


def addMemoryReviewItem(
    *,
    action: dict,
    chatID: str,
    originalMsg: str,
    opsID: str,
    userID: str | int | None = None,
    displayBlocks: DisplayBlocks | None = None,
):
    """
    将 LLM 记忆操作加入审核队列。

    参数:
        action: MemoryAction 的 dict 形式
        chatID: 原始聊天 ID
        originalMsg: 触发该操作的用户消息
        opsID: 审核通知发送给哪个 ops
        userID: 触发用户 ID
        displayBlocks: 审核卡结构化展示块（console/chatScreen 路径恒为 None，见 capability/memory.py 的 submitMemoryAction）
    """
    getReviewQueue().put_nowait(makeMemoryReviewItem(
        action=action,
        chatID=chatID,
        originalMsg=originalMsg,
        opsID=opsID,
        userID=userID,
        displayBlocks=displayBlocks,
    ))




# ---------------------------------------------------------------------------
# 共享字段提取
# ---------------------------------------------------------------------------

def extractMemoryActionFields(action: dict) -> dict:
    """
    从 action dict 中提取标准化展示字段。
    处理 None 安全（content/originalContent fallback），供格式化函数共用。
    """
    return {
        "actionType": action.get("action", "?"),
        "scopeType": action.get("scopeType", "?"),
        "scopeID": action.get("scopeID", ""),
        "content": action.get("content"),
        "originalContent": action.get("originalContent") or "",
        "tags": action.get("tags") or [],
        "priority": action.get("priority"),
        "mode": action.get("mode"),
        "originalMode": action.get("originalMode"),
        "retrievalHint": action.get("retrievalHint"),
        "reason": action.get("reason", ""),
        "memoryID": action.get("memoryID"),
    }




# ---------------------------------------------------------------------------
# 能力判断辅助函数
# ---------------------------------------------------------------------------

def canEditReviewItem(item: dict) -> bool:
    """判断审核项是否可编辑。"""
    kind = item.get("kind", "reply")
    if kind == "reply":
        return True
    if kind == "memory":
        actionType = item.get("action", {}).get("action", "")
        return actionType in {"add", "update"}
    return False


def canRetryReviewItem(item: dict) -> bool:
    """判断审核项是否可重试。"""
    return item.get("kind", "reply") == "reply"




# ---------------------------------------------------------------------------
# bot_data 里的审核项（Telegram 审核卡）
# ---------------------------------------------------------------------------

def replyReviewKey(chatID, reviewMsgID) -> str:
    return f"{_REPLY_KEY_PREFIX}{chatID}_{reviewMsgID}"


def memoryReviewKey(chatID, reviewMsgID) -> str:
    return f"{_MEMORY_KEY_PREFIX}{chatID}_{reviewMsgID}"


def editIndexKey(reviewMsgID) -> str:
    return f"{_EDIT_INDEX_PREFIX}{reviewMsgID}"


def isMemoryReviewKey(key: str) -> bool:
    return key.startswith(_MEMORY_KEY_PREFIX)


def _reviewMsgIDFromKey(key: str) -> str:
    return key.rsplit("_", 1)[-1]


def putReplyReview(
    botData: dict,
    *,
    chatID,
    reviewMsgID,
    reply,
    originalMsg,
    opsID,
    triggerMsgID,
    userID,
    includeContext,
    autoMode=None,
    displayBlocks: DisplayBlocks | None = None,
    memoryQuery=None,
    request: TaskRequest | None = None,
) -> str:
    """保存 Telegram 回复审核项及 message ID 反向索引。"""
    # key 指向 reply 审核项，审核卡片的 message_id 由 callback 反查
    key = replyReviewKey(chatID, reviewMsgID)
    botData[key] = {
        "reply": reply,
        "originalMsg": originalMsg,
        "chatID": chatID,
        "opsID": opsID,
        "triggerMsgID": triggerMsgID,
        "userID": userID,
        "includeContext": includeContext,
        "autoMode": autoMode,
        "displayBlocks": displayBlocks,
        "memoryQuery": memoryQuery,
        "request": request,
        "createdAt": time.time(),
    }
    botData[editIndexKey(reviewMsgID)] = key
    return key


def putMemoryReview(
    botData: dict,
    *,
    chatID,
    reviewMsgID,
    action,
    originalMsg,
    opsID,
    userID,
    displayBlocks: DisplayBlocks | None = None,
) -> str:
    """保存 Telegram memory 审核项及其独立的反向索引。"""
    # memory 审核与 reply 审核使用独立 key 前缀，避免同一聊天中两个审核卡片
    # 的编辑/批准回调互相覆盖；action 内含 targetState 供批准时做乐观锁校验。
    key = memoryReviewKey(chatID, reviewMsgID)
    botData[key] = {
        "action": action,
        "originalMsg": originalMsg,
        "chatID": chatID,
        "opsID": opsID,
        "userID": userID,
        "displayBlocks": displayBlocks,
        "createdAt": time.time(),
    }
    botData[editIndexKey(reviewMsgID)] = key
    return key


def deleteReviewEntry(botData: dict, key: str):
    """删除审核条目及其反向索引"""
    botData.pop(key, None)
    botData.pop(editIndexKey(_reviewMsgIDFromKey(key)), None)


def cleanupExpiredReviews(botData: dict):
    """清理 bot_data 中已过期的审核条目（包括回复审核、记忆审核及其反向索引）"""
    cutoff = time.time() - LLM_REVIEW_TTL_SECONDS
    expired = [
        k for k, v in botData.items()
        if (k.startswith(_REPLY_KEY_PREFIX) or k.startswith(_MEMORY_KEY_PREFIX))
        and isinstance(v, dict) and v.get("createdAt", 0) < cutoff
    ]
    for k in expired:
        deleteReviewEntry(botData, k)
