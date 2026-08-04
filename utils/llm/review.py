"""
utils/llm/review.py

LLM 审核共享操作。

三个审核入口（Telegram 按钮、chatScreen TUI、独立 CLI）
的 send/retry/cancel/feedback 逻辑统一在此，各自只负责 UI 交互和结果展示。

支持两类审核项：
    - kind == "reply"：回复审核（支持编辑、重试、补充反馈重试）
    - kind == "memory"：LLM 自主记忆操作审核
"""

from config import LLM_REVIEW_FEEDBACK_MAX_LENGTH

from utils.llm.client import generateReply
from utils.llm.config import getMemoryAutoApprove
from utils.llm.memory.action import (
    MemoryAction,
    buildMemoryActionReviewPayload,
    executeAction,
    formatActionDetail,
    parseMemoryActions,
    LLM_MEMORY_MAX_ACTIONS,
    validateAction,
)
from utils.llm.state import addMemoryReviewItem
from utils.core.logger import logAction, LogLevel, LogChildType, logSystemEvent
from utils.telegramHelpers import sendLLMReply




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
    for act in actions:
        addMemoryReviewItem(
            action=act.toDict(),
            chatID=chatID,
            originalMsg=originalMsg,
            opsID=opsID,
            userID=userID,
        )


async def dispatchMemoryActionsToConsole(actions, *, chatID, originalMsg, opsID, userID, logLabel):
    """
    memoryDispatcher 默认实现：async 包装转发到 sync queueMemoryActionsToConsole。

    契约：retry/feedback 路径有意不读 memoryAutoApprove（与首生成路径不同——首生成经
    dispatchMemoryActions 的 respectAutoApprove=True 读 memoryAutoApprove）——retry 产出
    的记忆操作本就要 ops 看过新回复才能定夺，故始终走审核。logLabel 仅用于汇总日志。
    """
    queueMemoryActionsToConsole(
        actions, chatID=chatID, originalMsg=originalMsg, opsID=opsID, userID=userID,
    )
    if actions:
        await logSystemEvent(
            f"LLM {logLabel} 生成记忆操作",
            f"{len(actions)} 个操作已加入 console 审核队列",
            LogLevel.INFO,
        )



async def dispatchMemoryActions(
    actions: list,
    *,
    autoMode: str,
    opsList: list,
    chatID,
    originalMsg: str,
    userID,
    respectAutoApprove: bool = True,
    sendTGMemoryReview=None,
) -> None:
    """
    按 autoMode 分流校验通过的记忆操作（首生成 + retry/feedback 共用）。

    与 dispatchMemoryActionsToConsole 的关系：本函数是 autoMode 总分流入口；
    console 分支直接调 addMemoryReviewItem，TG 分支委托调用方注入的 sendTGMemoryReview 回调
    （review.py 不依赖 handlers/PTB，TG 的 int() 类型转换在回调内完成）。

    respectAutoApprove：
        - True（首生成）：读 memoryAutoApprove，开启时直接 executeAction 自动执行
        - False（retry/feedback）：有意不读——产出的操作需 ops 看过新回复才能定夺

    opsList：审核人列表；首生成传完整列表（空则丢弃），retry/feedback 传 [opsID]。
    console 分支强制 str()，TG 分支的 int() 在 sendTGMemoryReview 回调内。
    """
    if not actions:
        return

    if respectAutoApprove and getMemoryAutoApprove():
        for act in actions:
            success = await executeAction(act)
            status = "成功" if success else "失败"
            await logAction(
                "System", f"LLM 记忆操作自动执行 ({status})",
                formatActionDetail(act),
                LogLevel.INFO, LogChildType.WITH_ONE_CHILD,
            )
        return

    if not opsList:
        await logSystemEvent(
            "LLM 记忆操作无审核人",
            f"有 {len(actions)} 个操作被丢弃（无 LLM ops）",
            LogLevel.WARNING,
        )
        return

    opsID = opsList[0]
    for act in actions:
        actDict = await buildMemoryActionReviewPayload(act)
        if autoMode == "console":
            addMemoryReviewItem(
                action=actDict,
                chatID=str(chatID),
                originalMsg=originalMsg,
                opsID=str(opsID),
                userID=userID,
            )
        else:
            if sendTGMemoryReview is None:
                raise RuntimeError(
                    "dispatchMemoryActions: TG 分支需要 sendTGMemoryReview 回调"
                )
            await sendTGMemoryReview(actDict)




# ---------------------------------------------------------------------------
# 审核动作
# ---------------------------------------------------------------------------

async def _retryReplyReview(
    item: dict,
    *,
    memoryDispatcher=dispatchMemoryActionsToConsole,
    logLabel: str = "console retry",
) -> dict:
    newReply = await generateReply(
        item["originalMsg"],
        item["chatID"],
        includeContext=bool(item.get("includeContext")),
        userID=item.get("userID"),
        urlContexts=item.get("urlContexts"),
    )

    # 清理 <MEMORY_ACTION> 块、校验并按 memoryDispatcher 分发（默认入 console 队列）
    failed = 0
    if item.get("includeContext"):
        newReply, validated, failed = await extractValidatedMemoryActions(newReply, logLabel=logLabel)
        await memoryDispatcher(
            validated,
            chatID=item["chatID"],
            originalMsg=item["originalMsg"],
            opsID=item["opsID"],
            userID=item.get("userID"),
            logLabel=logLabel,
        )

    await logAction(
        "System", "LLM 控制台审核：重新生成",
        f"原文：{item['originalMsg']}", LogLevel.INFO, LogChildType.WITH_CHILD,
    )
    await logAction(
        "System", "",
        f"生成的消息：{newReply}", LogLevel.INFO, LogChildType.LAST_CHILD,
    )
    return {**item, "reply": newReply, "memoryFailedCount": failed}


async def reviewRetryWithFeedback(
    item: dict,
    feedback: str,
    *,
    memoryDispatcher=dispatchMemoryActionsToConsole,
    logLabel: str = "feedback retry",
) -> dict:
    """
    Ops 补充反馈后打回去重试生成。

    将 ops 的补充要求追加到 originalMsg 后，作为 [背景信息补充：...] 块。
    LLM 会将其理解为可信的背景信息。

    参数:
        item: 原始审核项
        feedback: ops 输入的补充要求（超 LLM_REVIEW_FEEDBACK_MAX_LENGTH 抛 ValueError）

    返回:
        更新后的审核项（reply 已替换，含 memoryFailedCount）

    抛出:
        ValueError: feedback 超过 LLM_REVIEW_FEEDBACK_MAX_LENGTH（调用方负责提示用户精简）
    """
    trimmed = feedback.strip()
    # 长度检查置于 enhancedMsg 构造之前：超长直接拒绝，不白调用 generateReply
    if len(trimmed) > LLM_REVIEW_FEEDBACK_MAX_LENGTH:
        raise ValueError(
            f"反馈过长喵（{len(trimmed)}/{LLM_REVIEW_FEEDBACK_MAX_LENGTH} 字），请精简后重试"
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

    # 清理 <MEMORY_ACTION> 块、校验并按 memoryDispatcher 分发（默认入 console 队列）
    failed = 0
    if item.get("includeContext"):
        newReply, validated, failed = await extractValidatedMemoryActions(
            newReply, logLabel=logLabel,
        )
        await memoryDispatcher(
            validated,
            chatID=item["chatID"],
            originalMsg=item["originalMsg"],
            opsID=item["opsID"],
            userID=item.get("userID"),
            logLabel=logLabel,
        )

    await logAction(
        "System",
        "LLM 控制台审核：补充反馈重试",
        f"补充：{trimmed[:100]}", LogLevel.INFO, LogChildType.WITH_CHILD,
    )
    await logAction(
        "System", "",
        f"生成的消息：{newReply}", LogLevel.INFO, LogChildType.LAST_CHILD,
    )
    return {**item, "reply": newReply, "memoryFailedCount": failed}


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
    await sendLLMReply(
        bot=bot,
        chatID=item["chatID"],
        reply=item["reply"],
        replyToMessageID=item.get("messageID"),
    )
    await logAction(
        "System", "LLM 控制台审核：发送",
        f"原文：{item['originalMsg']}", LogLevel.INFO, LogChildType.WITH_ONE_CHILD,
    )


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
        logLabel: extractValidatedMemoryActions 与 dispatcher 共用的日志标签

    返回:
        更新后的审核项（reply 已替换，含 memoryFailedCount）
    """
    if item.get("kind", "reply") == "memory":
        raise ValueError("memory 审核项暂不支持重试")

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
            "System",
            "LLM 控制台审核：记忆操作编辑完成",
            f"编辑后：{editedText[:200]}", LogLevel.INFO, LogChildType.WITH_ONE_CHILD,
        )
        return editedItem

    # kind == "reply"
    await logAction(
        "System",
        "LLM 控制台审核：编辑完成",
        f"原文：{item['originalMsg']}", LogLevel.INFO, LogChildType.WITH_CHILD,
    )
    await logAction(
        "System", "",
        f"编辑后：{editedText[:200]}", LogLevel.INFO, LogChildType.LAST_CHILD,
    )
    return editedItem
