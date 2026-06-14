"""
utils/llm/review.py

LLM 审核共享操作。

三个审核入口（Telegram 按钮、chatScreen TUI、独立 CLI）
的 send/retry/cancel/feedback 逻辑统一在此，各自只负责 UI 交互和结果展示。

支持两类审核项：
    - kind == "reply"：回复审核（支持编辑、重试、补充反馈重试）
    - kind == "memory"：LLM 自主记忆操作审核
"""

from utils.llm.client import generateReply
from utils.llm.memory.action import (
    MemoryAction,
    executeAction,
    parseMemoryActions,
    LLM_MEMORY_MAX_ACTIONS,
    validateAction,
)
from utils.core.logger import logAction, LogLevel, LogChildType, logSystemEvent




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
        "content": action.get("content") or action.get("originalContent") or "",
        "tags": action.get("tags") or [],
        "priority": action.get("priority", 0),
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
# 格式化辅助函数
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
        if f["content"]:
            displayContent = f["content"] if len(f["content"]) <= 200 else f["content"][:200] + "..."
            lines.append(f"  内容: {displayContent}")
        if f["tags"]:
            lines.append(f"  标签: {', '.join(f['tags'])}")
        if f["priority"]:
            lines.append(f"  优先级: {f['priority']}")
        if f["reason"]:
            lines.append(f"  理由: {f['reason']}")
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
    return "[A]ccept / [E]dit / [R]etry / [F]eedback / [C]ancel"




# ---------------------------------------------------------------------------
# 记忆操作解析 / 校验 / 分发（审核层共享）
# ---------------------------------------------------------------------------

async def extractValidatedMemoryActions(reply: str, *, logLabel: str) -> tuple[str, list, int]:
    """
    清理 reply 中的 <MEMORY_ACTION> 块、截断超限操作、逐个校验。

    这是 action.py 原语（parse/validate）之上的审核层编排：
    截断阈值与"丢弃失败项并计数"的语义都为审核流程服务，故放在 review 层。

    参数:
        reply: LLM 原始回复（可能含 <MEMORY_ACTION> 块）
        logLabel: 日志来源标签，如 'retry' / 'feedback retry' / 'console retry'

    返回:
        清理后的 reply, 校验通过的 action 列表, 校验失败数
    """
    cleanedReply, actions = parseMemoryActions(reply)

    # 截断超限操作
    if len(actions) > LLM_MEMORY_MAX_ACTIONS:
        await logSystemEvent(
            f"LLM 记忆操作数量超限（{logLabel}）",
            f"请求 {len(actions)} 个，上限 {LLM_MEMORY_MAX_ACTIONS}，截断",
            LogLevel.WARNING,
        )
        actions = actions[:LLM_MEMORY_MAX_ACTIONS]

    # 逐个校验，失败项丢弃并计数
    validated = []
    failed = 0
    for act in actions:
        err = await validateAction(act)
        if err:
            failed += 1
            await logSystemEvent(
                f"LLM 记忆操作校验失败（{logLabel}）",
                f"{act.action} | {err}",
                LogLevel.WARNING,
            )
        else:
            validated.append(act)

    return cleanedReply, validated, failed


def queueMemoryActionsToConsole(actions: list, *, chatID, originalMsg, opsID, userID) -> None:
    """将校验通过的记忆操作加入 console 审核队列。"""
    from utils.llm.state import addMemoryReviewItem

    for act in actions:
        addMemoryReviewItem(
            action=act.toDict(),
            chatID=chatID,
            originalMsg=originalMsg,
            opsID=opsID,
            userID=userID,
        )




# ---------------------------------------------------------------------------
# 审核动作
# ---------------------------------------------------------------------------

async def _retryReplyReview(item: dict) -> dict:
    newReply = await generateReply(
        item["originalMsg"],
        item["chatID"],
        includeContext=bool(item.get("includeContext")),
        userID=item.get("userID"),
        urlContexts=item.get("urlContexts"),
    )

    # 清理 <MEMORY_ACTION> 块、校验并加入 Console 审核队列
    if item.get("includeContext"):
        newReply, validated, _ = await extractValidatedMemoryActions(newReply, logLabel="console retry")
        queueMemoryActionsToConsole(
            validated,
            chatID=item["chatID"],
            originalMsg=item["originalMsg"],
            opsID=item["opsID"],
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


async def reviewRetryWithFeedback(item: dict, feedback: str) -> dict:
    """
    Ops 补充反馈后重试生成。

    将 ops 的补充要求追加到 originalMsg 后，作为 [背景信息补充：...] 块。
    LLM 会将其理解为可信的背景信息。

    参数:
        item: 原始审核项
        feedback: ops 输入的补充要求（限制 200 字符）

    返回:
        更新后的审核项（reply 已替换）
    """
    # 限制长度
    MAX_FEEDBACK_LENGTH = 200
    trimmed = feedback.strip()
    if len(trimmed) > MAX_FEEDBACK_LENGTH:
        trimmed = trimmed[:MAX_FEEDBACK_LENGTH]
        await logSystemEvent(
            "Ops 反馈过长，已截断",
            f"原长度 {len(feedback)}，截断至 {MAX_FEEDBACK_LENGTH}",
            LogLevel.WARNING,
        )

    # 格式化并追加。
    # trimmed 的分隔符中和由下游统一处理：
    # enhancedMsg 作为 userMessage 流经 generateReply → buildConversationContext，
    # 在进 <CURRENT_USER_MESSAGE> 前被 neutralizePromptDelimiters 整体中和，
    # 故此处不再各自转义（见 utils/llm/promptSafety.py）。
    enhancedMsg = f"{item['originalMsg']}\n\n[背景信息补充：{trimmed}]"

    newReply = await generateReply(
        enhancedMsg,
        item["chatID"],
        includeContext=bool(item.get("includeContext")),
        userID=item.get("userID"),
        urlContexts=item.get("urlContexts"),
    )

    # 清理 <MEMORY_ACTION> 块、校验并加入 Console 审核队列
    if item.get("includeContext"):
        newReply, validated, _ = await extractValidatedMemoryActions(
            newReply, logLabel="feedback retry",
        )
        queueMemoryActionsToConsole(
            validated,
            chatID=item["chatID"],
            originalMsg=item["originalMsg"],
            opsID=item["opsID"],
            userID=item.get("userID"),
        )

    await logAction(
        "System", "LLM 控制台审核：补充反馈重试",
        f"补充：{trimmed[:100]}", LogLevel.INFO, LogChildType.WITH_CHILD,
    )
    await logAction(
        "System", "",
        f"生成的消息：{newReply}", LogLevel.INFO, LogChildType.LAST_CHILD,
    )
    return {**item, "reply": newReply}


async def _approveMemoryReview(item: dict) -> bool:
    actionData = item["action"]
    action = MemoryAction.fromDict(actionData)
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
    return success


def _updateReviewItemText(item: dict, editedText: str) -> dict:
    kind = item.get("kind", "reply")
    if kind == "memory":
        return {**item, "action": {**item["action"], "content": editedText}}
    return {**item, "reply": editedText}


async def reviewSend(bot, item: dict) -> None:
    """
    发送审核项的 reply 至目标聊天，或执行记忆操作。

    参数:
        bot: Telegram Bot 实例
        item: 审核项
    """
    kind = item.get("kind", "reply")

    if kind == "memory":
        await _approveMemoryReview(item)
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

    return await _retryReplyReview(item)


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
    editedItem = _updateReviewItemText(item, editedText)

    if kind == "memory":
        await logAction(
            "System", "LLM 控制台审核：记忆操作编辑完成",
            f"编辑后：{editedText[:200]}", LogLevel.INFO, LogChildType.WITH_ONE_CHILD,
        )
        return editedItem

    # kind == "reply"
    await logAction(
        "System", "LLM 控制台审核：编辑完成",
        f"原文：{item['originalMsg']}", LogLevel.INFO, LogChildType.WITH_CHILD,
    )
    await logAction(
        "System", "",
        f"编辑后：{editedText[:200]}", LogLevel.INFO, LogChildType.LAST_CHILD,
    )
    return editedItem
