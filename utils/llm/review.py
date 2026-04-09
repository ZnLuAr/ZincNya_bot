"""
utils/llm/review.py

LLM 审核共享操作。

三个审核入口（Telegram 按钮、chatScreen TUI、独立 CLI）
的 send/retry/cancel 逻辑统一在此，各自只负责 UI 交互和结果展示。

支持两类审核项：
    - kind == "reply"：回复审核
    - kind == "memory"：LLM 自主记忆操作审核
"""

from utils.llm.client import generateReply
from utils.llm.memory.action import MemoryAction, executeAction
from utils.logger import logAction, LogLevel, LogChildType


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
# 格式化辅助函数
# ---------------------------------------------------------------------------

def formatReviewItemText(item: dict) -> str:
    """将审核项格式化为可读文本，用于 console / chatScreen 展示。"""
    kind = item.get("kind", "reply")

    if kind == "memory":
        action = item.get("action", {})
        actionType = action.get("action", "?")
        scopeType = action.get("scopeType", "?")
        scopeID = action.get("scopeID", "")
        content = action.get("content") or action.get("originalContent") or ""
        tags = action.get("tags") or []
        priority = action.get("priority", 0)
        reason = action.get("reason", "")
        memoryID = action.get("memoryID")

        lines = [
            f"[记忆操作审核] {actionType.upper()}",
            f"  范围: {scopeType}:{scopeID or 'global'}",
        ]
        if memoryID is not None:
            lines.append(f"  目标 ID: #{memoryID}")
        if content:
            displayContent = content if len(content) <= 200 else content[:200] + "..."
            lines.append(f"  内容: {displayContent}")
        if tags:
            lines.append(f"  标签: {', '.join(tags)}")
        if priority:
            lines.append(f"  优先级: {priority}")
        if reason:
            lines.append(f"  理由: {reason}")
        lines.append(f"  触发消息: {item.get('originalMsg', '?')}")
        return "\n".join(lines)

    # kind == "reply"
    reply = item.get("reply", "")
    displayReply = reply if len(reply) <= 200 else reply[:200] + "..."
    return (
        f"[回复审核]\n"
        f"  原始消息: {item.get('originalMsg', '?')}\n"
        f"  回复内容: {displayReply}"
    )


def getReviewItemActions(item: dict) -> str:
    """返回审核项的可用操作提示。"""
    kind = item.get("kind", "reply")

    if kind == "memory":
        actionType = item.get("action", {}).get("action", "")
        if actionType in {"add", "update"}:
            return "[A]pprove / [E]dit / [C]ancel"
        return "[A]pprove / [C]ancel"

    # kind == "reply"
    return "[A]ccept / [E]dit / [R]etry / [C]ancel"


# ---------------------------------------------------------------------------
# 审核动作
# ---------------------------------------------------------------------------

async def reviewSend(bot, item: dict) -> None:
    """
    发送审核项的 reply 至目标聊天，或执行记忆操作。

    参数:
        bot: Telegram Bot 实例
        item: 审核项
    """
    kind = item.get("kind", "reply")

    if kind == "memory":
        actionData = item["action"]
        action = MemoryAction(
            action=actionData["action"],
            scopeType=actionData["scopeType"],
            scopeID=actionData.get("scopeID", ""),
            content=actionData.get("content"),
            tags=actionData.get("tags"),
            priority=actionData.get("priority"),
            memoryID=actionData.get("memoryID"),
            reason=actionData.get("reason", ""),
        )
        success = await executeAction(action)
        status = "成功" if success else "失败"
        detail = f"scope={action.scopeType}:{action.scopeID}"
        if action.memoryID is not None:
            detail += f", id=#{action.memoryID}"
        if action.content:
            detail += f", content={action.content[:80]}"
        await logAction(
            "System", f"LLM 控制台审核：记忆操作 {action.action} {status}",
            detail,
            LogLevel.INFO, LogChildType.WITH_ONE_CHILD,
        )
        return

    # kind == "reply"
    await bot.send_message(
        chat_id=item["chatID"],
        text=item["reply"],
        reply_to_message_id=item.get("messageID"),
    )
    await logAction(
        "System", "LLM 控制台审核：发送",
        f"原文：{item['originalMsg']}", LogLevel.INFO, LogChildType.WITH_ONE_CHILD,
    )


async def reviewRetry(item: dict) -> dict:
    """
    重新生成审核项的 reply，返回更新后的审核项。
    仅支持 kind == "reply"。

    参数:
        item: 审核项

    返回:
        更新后的审核项（reply 已替换）
    """
    if item.get("kind", "reply") == "memory":
        raise ValueError("memory 审核项暂不支持重试")

    newReply = await generateReply(
        item["originalMsg"],
        item["chatID"],
        includeContext=bool(item.get("includeContext")),
        userID=item.get("userID"),
    )
    await logAction(
        "System", "LLM 控制台审核：重新生成",
        f"原文：{item['originalMsg']}", LogLevel.INFO, LogChildType.WITH_CHILD,
    )
    await logAction(
        "System", "",
        f"生成的消息：{newReply}", LogLevel.INFO, LogChildType.LAST_CHILD,
    )
    return {**item, "reply": newReply}


async def reviewCancel(item: dict) -> None:
    """
    取消审核项，记录日志。

    参数:
        item: 审核项
    """
    kind = item.get("kind", "reply")
    if kind == "memory":
        action = item.get("action", {})
        await logAction(
            "System", "LLM 控制台审核：记忆操作取消",
            f"{action.get('action', '?')} | "
            f"scope={action.get('scopeType', '?')}:{action.get('scopeID', '')}",
            LogLevel.INFO, LogChildType.WITH_ONE_CHILD,
        )
        return

    await logAction(
        "System", "LLM 控制台审核：取消",
        f"原文：{item['originalMsg']}", LogLevel.INFO, LogChildType.WITH_ONE_CHILD,
    )


async def reviewEditSubmit(item: dict, editedText: str) -> dict:
    """
    提交编辑后的审核项，记录日志，返回更新后的审核项。
    若编辑内容为空则返回原始 item（不修改）。

    参数:
        item: 原始审核项
        editedText: 编辑后的文本

    返回:
        更新后的审核项
    """
    if not editedText.strip():
        return item

    kind = item.get("kind", "reply")

    if kind == "memory":
        editedItem = {**item}
        editedItem["action"] = {**item["action"], "content": editedText}
        await logAction(
            "System", "LLM 控制台审核：记忆操作编辑完成",
            f"编辑后：{editedText[:200]}", LogLevel.INFO, LogChildType.WITH_ONE_CHILD,
        )
        return editedItem

    # kind == "reply"
    editedItem = {**item, "reply": editedText}
    await logAction(
        "System", "LLM 控制台审核：编辑完成",
        f"原文：{item['originalMsg']}", LogLevel.INFO, LogChildType.WITH_CHILD,
    )
    await logAction(
        "System", "",
        f"编辑后：{editedText[:200]}", LogLevel.INFO, LogChildType.LAST_CHILD,
    )
    return editedItem