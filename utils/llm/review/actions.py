"""
utils/llm/review/actions.py

审核动作：批准（发送回复或执行记忆操作）、重试、补充反馈重试、取消、编辑。

console 与 chatScreen 两端直接使用；Telegram 按钮的重试也走这里，批准 / 取消
在 handlers/llmReview.py 里连同卡片回显一起处理。

重试会重新运行一遍任务（task.collectOutput），所以本文件依赖任务入口；items / cards /
console 不依赖它，发言、记忆能力送审时用的是这三个文件。
"""

from utils.core.logger import logAction, LogLevel, LogChildType
from utils.llm.memory.action import (
    MemoryAction,
    MemoryActionContext,
    buildMemoryActionReviewPayload,
    executeAction,
)
from utils.llm.task import collectOutput
from utils.telegramHelpers import sendLLMReply

from .console import dispatchMemoryActionsToConsole


_LOG_FEEDBACK_LEN = 100  # 日志中反馈补充文本的预览长度
_LOG_EDIT_LEN = 200  # 日志中编辑后文本的预览长度




async def _retryReplyReview(
    item: dict,
    *,
    feedback: str = "",
    memoryDispatcher=dispatchMemoryActionsToConsole,
    logLabel: str = "console retry",
) -> dict:
    """用原始请求重新运行任务，只收集结果；反馈只用于本次任务，不写回请求。"""
    request = item.get("request")
    if request is None:
        raise ValueError("审核项缺少原始生成请求，无法重试")
    collected = await collectOutput(request, feedback=feedback, logLabel=logLabel)
    if request.includeContext:
        await memoryDispatcher(
            collected.memoryActions,
            chatID=request.chatID, originalMsg=item["originalMsg"],
            opsID=item["opsID"], userID=request.userID, logLabel=logLabel,
        )

    feedback = feedback.strip()
    title = "LLM 审核：补充反馈重试" if feedback else "LLM 审核：重新生成"
    detail = f"补充：{feedback[:_LOG_FEEDBACK_LEN]}" if feedback else f"原文：{item['originalMsg']}"
    await logAction("System", title, detail, LogLevel.INFO, LogChildType.WITH_CHILD)
    await logAction(
        "System", "", f"生成的消息：{collected.reply}",
        LogLevel.INFO, LogChildType.LAST_CHILD,
    )
    return {
        **item,
        "reply": collected.reply,
        "memoryQuery": request.memoryQuery,
    }


async def reviewRetryWithFeedback(
    item: dict,
    feedback: str,
    *,
    memoryDispatcher=dispatchMemoryActionsToConsole,
    logLabel: str = "feedback retry",
) -> dict:
    """以一次性补充反馈重新生成回复；不改变原始消息或后续重试的输入。"""
    if item.get("kind", "reply") == "memory":
        raise ValueError("记忆审核项暂不支持重试")
    return await _retryReplyReview(
        item, feedback=feedback, memoryDispatcher=memoryDispatcher, logLabel=logLabel,
    )


async def _approveMemoryReview(item: dict) -> bool:
    """使用审核快照批准 memory 操作；冲突时刷新目标供再次确认。"""
    actionData = item["action"]
    action = MemoryAction.fromDict(actionData)
    # 审核卡保存的 chat/user 来自生成请求，是批准时仍可信的授权上下文；
    # 不能改用 actionData 中由模型生成、或卡片展示层重新拼出的 scope ID。
    actionContext = MemoryActionContext(
        chatID=item.get("chatID"),
        userID=item.get("userID"),
    )
    success = await executeAction(
        action,
        humanApproved=True,
        expectedState=actionData.get("targetState"),
        actionContext=actionContext,
    )
    if not success:
        await refreshMemoryReviewItem(item)
    status = "成功" if success else "失败"
    detail = f"scope={action.scopeType}:{action.scopeID}"
    if action.memoryID is not None:
        detail += f", id=#{action.memoryID}"
    await logAction(
        "System", f"LLM 审核：记忆操作 {action.action} {status}",
        detail,
        LogLevel.INFO, LogChildType.WITH_ONE_CHILD,
    )
    return success


async def refreshMemoryReviewItem(item: dict) -> bool:
    """以当前数据库目标刷新 action 与 ``targetState``；不可用时返回 False。"""
    actionData = item.get("action")
    if not isinstance(actionData, dict):
        return False

    try:
        action = MemoryAction.fromDict(actionData)
    except (KeyError, TypeError, ValueError):
        return False
    if action.memoryID is None:
        return False

    refreshedAction = await buildMemoryActionReviewPayload(action)
    if not refreshedAction.get("targetState"):
        return False
    item["action"] = refreshedAction
    return True


def _updateReviewItemText(item: dict, editedText: str) -> dict:
    """返回编辑后的审核项；memory 正文变化时同步清空旧 hint。"""
    kind = item.get("kind", "reply")
    if kind == "memory":
        return {
            **item,
            "action": {
                **item["action"],
                "content": editedText,
                "retrievalHint": "",
            },
        }
    return {**item, "reply": editedText}


async def reviewSend(bot, item: dict) -> bool:
    """
    发送审核项的 reply 至目标聊天，或执行记忆操作。

    服务 console / chatScreen 两端（TG 按钮走 handlers/llmReview.py 自己的 send 分支，
    不经此函数）。

    参数:
        bot: Telegram Bot 实例
        item: 审核项

    返回:
        True 表示回复已发送或 memory 已执行；False 表示 memory 目标状态冲突，
        item 已刷新并应重新入队等待人工确认
    """
    kind = item.get("kind", "reply")

    if kind == "memory":
        return await _approveMemoryReview(item)

    # kind == "reply"
    await sendLLMReply(
        bot=bot,
        chatID=item["chatID"],
        reply=item["reply"],
        replyToMessageID=item.get("messageID"),
    )
    await logAction(
        "System", "LLM 审核：发送",
        f"原文：{item['originalMsg']}", LogLevel.INFO, LogChildType.WITH_ONE_CHILD,
    )
    return True


async def reviewRetry(
    item: dict,
    *,
    memoryDispatcher=dispatchMemoryActionsToConsole,
    logLabel: str = "console retry",
) -> dict:
    """
    重新生成审核项的 reply，返回更新后的审核项。
    仅支持 kind == "reply"。

    参数:
        item: 审核项
        memoryDispatcher: 记忆操作分发回调（默认入 console 队列；TG 端注入 autoMode 分流闭包）
        logLabel: collectOutput 与 dispatcher 共用的日志标签

    返回:
        更新后的审核项（reply 已替换）
    """
    if item.get("kind", "reply") == "memory":
        raise ValueError("记忆审核项暂不支持重试")

    return await _retryReplyReview(item, memoryDispatcher=memoryDispatcher, logLabel=logLabel)


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
            "System", "LLM 审核：记忆操作取消",
            f"{action.get('action', '?')} | "
            f"scope={action.get('scopeType', '?')}:{action.get('scopeID', '')}",
            LogLevel.INFO, LogChildType.WITH_ONE_CHILD,
        )
        return

    await logAction(
        "System", "LLM 审核：取消",
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
        actionData = item.get("action", {})
        actionType = actionData.get("action", "?")
        scopeType = actionData.get("scopeType", "?")
        scopeID = actionData.get("scopeID", "")
        memoryID = actionData.get("memoryID")
        detail = f"{actionType} | scope={scopeType}:{scopeID}"
        if memoryID is not None:
            detail += f" | id=#{memoryID}"
        await logAction(
            "System",
            "LLM 审核：记忆操作编辑完成",
            detail,
            LogLevel.INFO, LogChildType.WITH_ONE_CHILD,
        )
        return editedItem

    # kind == "reply"
    await logAction(
        "System",
        "LLM 审核：编辑完成",
        f"原文：{item['originalMsg']}", LogLevel.INFO, LogChildType.WITH_CHILD,
    )
    await logAction(
        "System", "",
        f"编辑后：{editedText[:_LOG_EDIT_LEN]}", LogLevel.INFO, LogChildType.LAST_CHILD,
    )
    return editedItem
