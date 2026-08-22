"""
utils/llm/state.py

LLM 运行时状态管理：
    - 待审核消息队列容器（console / chatScreen 共用；item 契约与入队操作在
      utils/llm/review.py——队列项是审核域契约，生产端与消费端同文件演进）
    - 每用户速率限制
    - 消息防抖缓冲（聚合短时间内分多次发送的消息，按 dict 记录
      text / includeContext / images / urlIntentText / urlCandidateText；
      collectDebouncedBatch 聚合为 DebouncedBatch）
    - 全局 one-shot context 标记（memory -once）
"""

import time
import asyncio
from dataclasses import dataclass, field

from config import LLM_RATE_LIMIT_SECONDS, LLM_PENDING_MSG_LIMIT




# 待审核消息队列（auto -console 模式时使用；item 契约在 utils/llm/review.py）
_llmReviewQueue: asyncio.Queue = asyncio.Queue()

# 每用户最后调用时间 {userID: timestamp}
_lastCallTime: dict[str, float] = {}

# 消息防抖状态（聚合短时间内分多次发送的消息）
_pendingMessages: dict[str, list[dict]] = {}   # debounceKey -> [{"text": str, "includeContext": bool, "images": list, "urlIntentText": str, "urlCandidateText": str, "replyLine": str, "currentText": str}]
_pendingTasks: dict[str, asyncio.Task] = {}   # debounceKey -> 当前防抖 Task

# 全局 one-shot context 标记：下一次 LLM 调用强制带记忆，触发后自动清除
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




# ============================================================================
# 防抖聚合工具
# ============================================================================

@dataclass(frozen=True, kw_only=True)
class DebouncedBatch:
    """
    防抖聚合批次的纯数据载体，不涉及 Python-Telegram-Bot。

    collectDebouncedBatch 把同一 (chatID, userID) 防抖键下的全部 pending 消息
    聚合成一个批次（唯一生产点）；_runLLMPipeline 消费。批次内的多条用户消息
    在这里合为一体：文本换行拼接、图片串联、上下文标记取或。

    字段（prompt 线 / 展示线在此并行共存）:
        combinedText:     prompt 主文本。各消息 pureText（已含 reply marker、
                          图片 notes）换行拼接，直接作为 generateReply 的 userMessage
        includeContext:   批次内任一消息 True 或 one-shot 标记（consumeContextOnce）
                          取或——一条开上下文，整批生效
        images:           各消息图片列表串联，元素 {"data": b64, "mimeType": str}
        urlIntentText:    各消息 urlIntentText 换行拼接（意图判定，reply 不参与）
        urlCandidateText: 各消息 urlCandidateText 换行拼接（URL 提取候选）
        displayPairs:     展示线。各消息 {"replyLine", "currentText"} 的列表，
                          双空条目已过滤——审核卡按此逐条渲染引用/当前配对
    """
    combinedText: str
    includeContext: bool
    images: list[dict]
    urlIntentText: str
    urlCandidateText: str
    displayPairs: list[dict] = field(default_factory=list)


def addRateLimit(userID: str | int):
    """记录用户的调用时间（触发速率限制）"""
    _lastCallTime[str(userID)] = time.time()


def makeDebounceKey(chatID: str | int, userID: str | int) -> str:
    """构造防抖键：同一用户在不同 chat 中分别聚合"""
    return f"{chatID}:{userID}"


def appendPendingMessage(
    debounceKey: str,
    text: str,
    includeContext: bool = False,
    images: list[dict] | None = None,
    urlIntentText: str | None = None,
    urlCandidateText: str | None = None,
    replyLine: str | None = None,
    currentText: str | None = None,
) -> bool:
    """
    将消息追加到防抖缓冲区。

    参数:
        images: 图片列表 [{"data": b64_str, "mimeType": "..."}, ...]
        urlIntentText: 当前用户消息文本，用于判断 URL 读取意图
        urlCandidateText: 当前用户消息 + 被回复消息文本，用于提取 URL
        replyLine: 引用行（"<@发送者> 文本"，无引用为空串）——审核卡结构化展示用，prompt 侧不用
        currentText: 当前消息纯文本（含图片 notes）——同上

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
        "replyLine": replyLine or "",
        "currentText": currentText or "",
    })
    return True


def popPendingMessages(debounceKey: str) -> list[dict]:
    """取出并清空该防抖键对应的待聚合消息列表"""
    return _pendingMessages.pop(debounceKey, [])


def collectDebouncedBatch(debounceKey: str) -> DebouncedBatch | None:
    """
    收集防抖批次消息，空缓冲返回 None

    includeContext 在「任一消息为 True」和「one-shot context 标记（consumeContextOnce）」之间取或。
    """
    parts = popPendingMessages(debounceKey)
    if not parts:
        return None

    combinedText = "\n".join(p["text"] for p in parts if p["text"])
    hadOnce = consumeContextOnce()
    includeContext = any(p["includeContext"] for p in parts) or hadOnce

    allImages: list[dict] = []
    for p in parts:
        allImages.extend(p["images"])

    combinedURLIntentText = "\n".join(p["urlIntentText"] for p in parts if p["urlIntentText"])
    combinedURLCandidateText = "\n".join(p["urlCandidateText"] for p in parts if p["urlCandidateText"])

    # 引用/当前结构化配对（双空过滤——旧缓冲条目/外部调用无新键时跳过）
    displayPairs = [
        {"replyLine": p.get("replyLine", ""), "currentText": p.get("currentText", "")}
        for p in parts
        if p.get("replyLine") or p.get("currentText")
    ]

    return DebouncedBatch(
        combinedText=combinedText,
        includeContext=includeContext,
        images=allImages,
        urlIntentText=combinedURLIntentText,
        urlCandidateText=combinedURLCandidateText,
        displayPairs=displayPairs,
    )


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
