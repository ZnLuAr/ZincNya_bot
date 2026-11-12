"""
utils/llm/review/console.py

控制台 / chatScreen 一侧的审核：审核项的展示文字与可用操作提示，以及重试产生的
记忆操作入队。Telegram 一侧的展示见 cards.py。
"""

from utils.core.logger import logSystemEvent, LogLevel
from utils.llm.memory.action import buildMemoryActionReviewPayload

from .items import addMemoryReviewItem, extractMemoryActionFields


_DISPLAY_LIMIT = 200  # 展示用文本截断长度（memory 内容 / reply）




# ---------------------------------------------------------------------------
# 展示
# ---------------------------------------------------------------------------

def formatReviewItemText(item: dict) -> str:
    """将审核项格式化为可读文本，用于 console / chatScreen 展示。"""
    kind = item.get("kind", "reply")

    if kind == "memory":
        f = extractMemoryActionFields(item.get("action", {}))

        lines = [
            f"[记忆操作审核] {f['actionType'].upper()}",
            f"  范围: {f['scopeType']}:{f['scopeID'] or 'global'}",
        ]
        if f["memoryID"] is not None:
            lines.append(f"  目标 ID: #{f['memoryID']}")
        if f["originalContent"]:
            displayContent = (
                f["originalContent"]
                if len(f["originalContent"]) <= _DISPLAY_LIMIT
                else f["originalContent"][:_DISPLAY_LIMIT] + "..."
            )
            lines.append(f"  当前内容: {displayContent}")
        if f["content"]:
            displayContent = (
                f["content"]
                if len(f["content"]) <= _DISPLAY_LIMIT
                else f["content"][:_DISPLAY_LIMIT] + "..."
            )
            label = "新内容" if f["originalContent"] else "内容"
            lines.append(f"  {label}: {displayContent}")
        if f["tags"]:
            lines.append(f"  标签: {', '.join(f['tags'])}")
        if f["priority"] is not None:
            lines.append(f"  优先级: {f['priority']}")
        if f["originalMode"] is not None:
            lines.append(f"  当前模式: {f['originalMode']}")
        if f["mode"] is not None:
            label = "新模式" if f["originalMode"] is not None else "模式"
            lines.append(f"  {label}: {f['mode']}")
        if f["retrievalHint"] is not None:
            lines.append(f"  检索说明: {f['retrievalHint'] or '(清空)'}")
        if f["reason"]:
            lines.append(f"  理由: {f['reason']}")
        lines.append(f"  触发消息: {item.get('originalMsg', '?')}")
        return "\n".join(lines)

    # kind == "reply"
    reply = item.get("reply", "")
    displayReply = reply if len(reply) <= _DISPLAY_LIMIT else reply[:_DISPLAY_LIMIT] + "..."
    return (
        f"[回复审核]\n"
        f"  原始消息: {item.get('originalMsg', '?')}\n"
        f"  回复内容: {displayReply}"
    )


def getReviewItemActions(item: dict) -> str:
    """对控制台审核，返回审核项的可用操作提示"""
    kind = item.get("kind", "reply")

    if kind == "memory":
        actionType = item.get("action", {}).get("action", "")
        if actionType in {"add", "update"}:
            return "[A]pprove / [E]dit / [C]ancel"
        return "[A]pprove / [C]ancel"

    # kind == "reply"
    return "[A]ccept / [E]dit / [R]etry / [F]eedback / [C]ancel"




# ---------------------------------------------------------------------------
# 重试产生的记忆操作
# ---------------------------------------------------------------------------

async def dispatchMemoryActionsToConsole(actions, *, chatID, originalMsg, opsID, userID, logLabel):
    """
    actions.py 里 memoryDispatcher 的默认实现：构造审核快照后加入 console 队列。

    重试产生的记忆操作不读 memoryAutoApprove，始终进入审核——ops 要看过新回复才能定夺。
    首次生成走 capability/memory.py 的 submitMemoryAction，那里才读 memoryAutoApprove。
    logLabel 仅用于汇总日志。
    """
    for act in actions:
        actDict = await buildMemoryActionReviewPayload(act)
        addMemoryReviewItem(
            action=actDict,
            chatID=chatID,
            originalMsg=originalMsg,
            opsID=opsID,
            userID=userID,
        )
    if actions:
        await logSystemEvent(
            f"LLM {logLabel} 生成记忆操作",
            f"{len(actions)} 个操作已加入 console 审核队列",
            LogLevel.INFO,
        )
