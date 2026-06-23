"""
handlers/llmReview.py

Telegram 端 LLM 审核回调处理器

职责：
    - 向 ops 发送审核消息（带 inline keyboard）
    - 处理审核按钮回调（发送 / 重试 / 取消）
    - 处理 ops 对审核消息的 :edit 编辑
    - 处理 ops 对审核消息的 :fb 补充反馈重试
    - 管理 bot_data 中的审核状态（含过期清理）

支持两类审核项：
    - llm_review_*：回复审核（发送 / 重试 / 取消 / :edit 编辑 / :fb 补充反馈）
    - llm_memreview_*：LLM 记忆操作审核（批准 / 取消 / :edit 编辑 add/update）
"""

import time

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CallbackQueryHandler

from config import Permission, LLM_REVIEW_TTL_SECONDS

from utils.core.errorDecorators import handleTelegramErrors
from utils.llm import generateReply
from utils.llm.memory.action import MemoryAction, executeAction
from utils.llm.review import extractMemoryActionFields, extractValidatedMemoryActions, queueMemoryActionsToConsole
from utils.core.logger import logAction, LogLevel, LogChildType, logSystemEvent
from utils.operators import hasPermission
from utils.telegramHelpers import sendLLMReply


_TG_MAX_LEN = 4096
_REPLY_PREVIEW_LEN = 1800  # 审核消息中 reply 预留长度（正文 + 框架文字后仍留余量）




def _replyReviewKey(chatID, reviewMsgID) -> str:
    return f"llm_review_{chatID}_{reviewMsgID}"


def _memoryReviewKey(chatID, reviewMsgID) -> str:
    return f"llm_memreview_{chatID}_{reviewMsgID}"


def _editIndexKey(reviewMsgID) -> str:
    return f"llm_editidx_{reviewMsgID}"


def _reviewMsgIDFromKey(key: str) -> str:
    return key.rsplit("_", 1)[-1]


def _putReplyReview(bot_data: dict, *, chatID, reviewMsgID, reply, originalMsg, opsID, triggerMsgID, userID, includeContext, urlContexts=None, autoMode=None) -> str:
    key = _replyReviewKey(chatID, reviewMsgID)
    bot_data[key] = {
        "reply": reply,
        "originalMsg": originalMsg,
        "chatID": chatID,
        "opsID": opsID,
        "triggerMsgID": triggerMsgID,
        "userID": userID,
        "includeContext": includeContext,
        "urlContexts": urlContexts or [],
        "autoMode": autoMode,
        "createdAt": time.time(),
    }
    bot_data[_editIndexKey(reviewMsgID)] = key
    return key


def _putMemoryReview(bot_data: dict, *, chatID, reviewMsgID, action, originalMsg, opsID, userID) -> str:
    key = _memoryReviewKey(chatID, reviewMsgID)
    bot_data[key] = {
        "action": action,
        "originalMsg": originalMsg,
        "chatID": chatID,
        "opsID": opsID,
        "userID": userID,
        "createdAt": time.time(),
    }
    bot_data[_editIndexKey(reviewMsgID)] = key
    return key


def _deleteReviewEntry(bot_data: dict, key: str):
    """删除审核条目及其反向索引"""
    bot_data.pop(key, None)
    bot_data.pop(_editIndexKey(_reviewMsgIDFromKey(key)), None)


def _truncate(text: str, limit: int) -> str:
    """超出 limit 时截断并附加省略提示，保证返回长度不超过 limit"""
    if len(text) <= limit:
        return text

    suffix = "……[内容过长，已截断]"
    if limit <= len(suffix):
        return suffix[:limit]
    return text[:limit - len(suffix)] + suffix


def _formatReviewText(originalMsg: str, reply: str) -> str:
    """构造审核消息文本（自动截断超长内容）"""
    reply = _truncate(reply, _REPLY_PREVIEW_LEN)
    text = (
        f"[待审核]\n\n"
        f"原始消息：\n{originalMsg}\n\n"
        f"---\n"
        f"{reply}\n"
        f"---\n\n"
        f"💡 回复此消息并以 :edit 开头修改内容喵"
    )
    return _truncate(text, _TG_MAX_LEN)


def _buildReviewKeyboard(chatID, msgID) -> InlineKeyboardMarkup:
    """构造审核 inline keyboard"""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ 发送", callback_data=f"llm:review:send:{chatID}:{msgID}"),
            InlineKeyboardButton("🔄 重试", callback_data=f"llm:review:retry:{chatID}:{msgID}"),
            InlineKeyboardButton("❌ 取消", callback_data=f"llm:review:cancel:{chatID}:{msgID}"),
        ],
    ])




# ---------------------------------------------------------------------------
# 记忆审核格式化与键盘
# ---------------------------------------------------------------------------

def _formatMemoryReviewText(action: dict, originalMsg: str) -> str:
    """构造记忆审核消息文本"""
    f = extractMemoryActionFields(action)

    lines = [
        f"[记忆操作待审核] {f['actionType'].upper()}",
        f"范围: {f['scopeType']}:{f['scopeID'] or 'global'}",
    ]
    if f["memoryID"] is not None:
        lines.append(f"目标 ID: #{f['memoryID']}")
    if f["content"]:
        displayContent = _truncate(f["content"], 500)
        lines.append(f"内容: {displayContent}")
    if f["tags"]:
        lines.append(f"标签: {', '.join(f['tags'])}")
    if f["priority"]:
        lines.append(f"优先级: {f['priority']}")
    if f["reason"]:
        lines.append(f"理由: {f['reason']}")
    lines.append(f"\n触发消息: {originalMsg}")

    if f["actionType"] in ("add", "update"):
        lines.append("\n💡 回复此消息并以 :edit 开头修改记忆内容")

    text = "\n".join(lines)
    return _truncate(text, _TG_MAX_LEN)


def _buildMemoryReviewKeyboard(chatID, msgID) -> InlineKeyboardMarkup:
    """构造记忆审核 inline keyboard"""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ 批准", callback_data=f"llm:memreview:approve:{chatID}:{msgID}"),
            InlineKeyboardButton("❌ 取消", callback_data=f"llm:memreview:cancel:{chatID}:{msgID}"),
        ],
    ])




async def handleEditReply(message, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    处理 ops 对审核消息的 :edit 编辑
    返回 True 表示消息已处理，调用方应 return

    安全约束：
        - 只有拥有 Permission.LLM 的 ops 本人才能编辑自己的审核消息
        - 无权用户或点错消息时静默忽略，不打断正常群聊/审核流程
    """
    if not (message.reply_to_message and message.text and message.text.startswith(":edit ")):
        return False

    senderID = str(message.from_user.id) if message.from_user else None
    if not senderID or not hasPermission(senderID, Permission.LLM):
        return True

    replyToID = message.reply_to_message.message_id
    reviewKey = (context.bot_data or {}).get(_editIndexKey(replyToID))
    if not reviewKey:
        return True

    reviewData = context.bot_data.get(reviewKey)
    if not reviewData or str(reviewData.get("opsID")) != senderID:
        return True

    isMemoryReview = reviewKey.startswith("llm_memreview_")

    newText = message.text[6:].strip()
    if newText:
        if isMemoryReview:
            # 记忆审核编辑，只允许 add/update
            actionData = reviewData.get("action", {})
            if actionData.get("action") not in ("add", "update"):
                return True
            context.bot_data[reviewKey]["action"]["content"] = newText
            chatIDEdit = reviewData["chatID"]
            msgIDEdit = _reviewMsgIDFromKey(reviewKey)
            await context.bot.edit_message_text(
                chat_id=senderID,
                message_id=replyToID,
                text=_formatMemoryReviewText(context.bot_data[reviewKey]["action"], reviewData["originalMsg"]),
                reply_markup=_buildMemoryReviewKeyboard(chatIDEdit, msgIDEdit),
            )
        else:
            context.bot_data[reviewKey]["reply"] = newText
            chatIDEdit = reviewData["chatID"]
            msgIDEdit = _reviewMsgIDFromKey(reviewKey)
            await context.bot.edit_message_text(
                chat_id=senderID,
                message_id=replyToID,
                text=_formatReviewText(reviewData["originalMsg"], newText),
                reply_markup=_buildReviewKeyboard(chatIDEdit, msgIDEdit),
            )
        await message.delete()
    return True




async def sendReviewMessage(
    bot,
    opsID: int,
    originalMsg: str,
    reply: str,
    chatID: int,
    context: ContextTypes.DEFAULT_TYPE,
    triggerMsgID: int | None = None,
    userID: int | None = None,
    includeContext: bool = False,
    urlContexts: list[dict] | None = None,
    autoMode: str | None = None,
):
    """
    发送 Telegram 审核消息（带 inline keyboard），并存储审核状态到 bot_data
    """
    # 先发一条无按钮消息拿到 message_id，再用该 id 构造含正确 callback_data 的 keyboard
    sent = await bot.send_message(
        chat_id=opsID,
        text=_formatReviewText(originalMsg, reply),
    )
    reviewMsgID = sent.message_id
    await bot.edit_message_reply_markup(
        chat_id=opsID,
        message_id=reviewMsgID,
        reply_markup=_buildReviewKeyboard(chatID, reviewMsgID),
    )

    # 存储审核状态（重启后失效可接受）
    _putReplyReview(
        context.bot_data,
        chatID=chatID,
        reviewMsgID=reviewMsgID,
        reply=reply,
        originalMsg=originalMsg,
        opsID=opsID,
        triggerMsgID=triggerMsgID,
        userID=userID,
        includeContext=includeContext,
        urlContexts=urlContexts,
        autoMode=autoMode,
    )




async def sendMemoryReviewMessage(
    bot,
    opsID: int,
    action: dict,
    originalMsg: str,
    chatID: int | str,
    context: ContextTypes.DEFAULT_TYPE,
    userID: int | str | None = None,
):
    """
    发送 Telegram 记忆操作审核消息（带 inline keyboard），并存储审核状态到 bot_data

    参数:
        action: MemoryAction 的 dict 形式
        originalMsg: 触发该操作的用户消息
        chatID: 原始聊天 ID
    """
    sent = await bot.send_message(
        chat_id=opsID,
        text=_formatMemoryReviewText(action, originalMsg),
    )
    reviewMsgID = sent.message_id
    await bot.edit_message_reply_markup(
        chat_id=opsID,
        message_id=reviewMsgID,
        reply_markup=_buildMemoryReviewKeyboard(chatID, reviewMsgID),
    )

    _putMemoryReview(
        context.bot_data,
        chatID=chatID,
        reviewMsgID=reviewMsgID,
        action=action,
        originalMsg=originalMsg,
        opsID=opsID,
        userID=userID,
    )




def _cleanupExpiredReviews(bot_data: dict):
    """清理 bot_data 中已过期的审核条目（包括回复审核、记忆审核及其反向索引）"""
    cutoff = time.time() - LLM_REVIEW_TTL_SECONDS
    expired = [
        k for k, v in bot_data.items()
        if (k.startswith("llm_review_") or k.startswith("llm_memreview_"))
        and isinstance(v, dict) and v.get("createdAt", 0) < cutoff
    ]
    for k in expired:
        _deleteReviewEntry(bot_data, k)




async def _dispatchMemoryActionsTelegram(
    actions: list,
    *,
    chatID,
    originalMsg,
    opsID,
    userID,
    autoMode: str,
    context: ContextTypes.DEFAULT_TYPE,
    logLabel: str,
) -> None:
    """
    根据 autoMode 分流校验通过的记忆操作：
        - "console"：加入控制台审核队列（chatID/opsID 转为 str，与 console 约定一致）
        - 其他（off 等）：逐个发送 Telegram 记忆审核消息
    """
    if not actions:
        return

    if autoMode == "console":
        queueMemoryActionsToConsole(
            actions,
            chatID=str(chatID),
            originalMsg=originalMsg,
            opsID=str(opsID),
            userID=userID,
        )
        await logSystemEvent(
            f"LLM {logLabel} 生成记忆操作",
            f"{len(actions)} 个操作已加入 console 审核队列",
            LogLevel.INFO,
        )
    else:
        from handlers.llm import _buildMemoryActionReviewPayload

        for act in actions:
            actDict = await _buildMemoryActionReviewPayload(act)
            await sendMemoryReviewMessage(
                bot=context.bot,
                opsID=opsID,
                action=actDict,
                originalMsg=originalMsg,
                chatID=chatID,
                context=context,
                userID=userID,
            )
        await logSystemEvent(
            f"LLM {logLabel} 生成记忆操作",
            f"{len(actions)} 个操作已发送 Telegram 审核",
            LogLevel.INFO,
        )




@handleTelegramErrors(errorReply="……诶、操作好像出了点问题喵……")
async def handleReviewCallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理审核按钮点击无权用户点击时静默忽略"""
    query = update.callback_query
    clickerID = str(query.from_user.id) if query.from_user else None
    if not clickerID:
        return

    _cleanupExpiredReviews(context.bot_data)

    parts = query.data.split(":")
    if len(parts) != 5:
        await query.answer()
        await query.edit_message_text("[无效的操作喵]")
        return
    action, chatID, msgID = parts[2:]
    chatID = int(chatID)

    key = _replyReviewKey(chatID, msgID)
    reviewData = context.bot_data.get(key)
    if not reviewData:
        await query.answer()
        await query.edit_message_text("[消息已过期喵]")
        return

    opsID = str(reviewData["opsID"])
    if clickerID != opsID or not hasPermission(clickerID, Permission.LLM):
        return

    await query.answer()

    if action == "send":
        await sendLLMReply(
            bot=context.bot,
            chatID=chatID,
            reply=reviewData["reply"],
            replyToMessageID=reviewData.get("triggerMsgID"),
            maxLength=_TG_MAX_LEN,
        )

        sentText = (
            "[消息已发送喵]\n\n"
            "原始消息：\n"
            f"{reviewData['originalMsg']}\n\n"
            "---\n"
            f"{reviewData['reply']}\n"
        )
        await query.edit_message_text(_truncate(sentText, _TG_MAX_LEN))

        _deleteReviewEntry(context.bot_data, key)
        await logAction("System", f"LLM 生成内容审核通过：{chatID}", f"原文：{reviewData['originalMsg']}", LogLevel.INFO, LogChildType.WITH_CHILD)
        await logAction("System", "", f"生成的消息：{reviewData['reply']}", LogLevel.INFO, LogChildType.LAST_CHILD)

    elif action == "retry":
        try:
            newReply = await generateReply(
                reviewData["originalMsg"],
                str(chatID),
                includeContext=bool(reviewData.get("includeContext")),
                userID=reviewData.get("userID"),
                urlContexts=reviewData.get("urlContexts"),
            )
        except Exception as e:
            await context.bot.send_message(chat_id=opsID, text=f"重试失败：{e}")
            return

        # 清理 <MEMORY_ACTION> 块、校验并按 autoMode 分流
        failedCount = 0
        if reviewData.get("includeContext"):
            newReply, validatedActions, failedCount = await extractValidatedMemoryActions(
                newReply, logLabel="retry",
            )
            await _dispatchMemoryActionsTelegram(
                validatedActions,
                chatID=chatID,
                originalMsg=reviewData["originalMsg"],
                opsID=opsID,
                userID=reviewData.get("userID"),
                autoMode=reviewData.get("autoMode", "console"),
                context=context,
                logLabel="retry",
            )

        # 如果有校验失败，在审核消息附加警告
        warningText = ""
        if failedCount > 0:
            warningText = f"\n\n⚠️ {failedCount} 个记忆操作校验失败，已丢弃"

        await query.edit_message_text(
            text=_formatReviewText(reviewData["originalMsg"], newReply) + warningText,
            reply_markup=_buildReviewKeyboard(chatID, msgID),
        )
        context.bot_data[key]["reply"] = newReply
        await logAction("System", f"LLM 生成内容审核重试：{chatID}", f"原文：{reviewData['originalMsg']}", LogLevel.INFO, LogChildType.WITH_CHILD)
        await logAction("System", "", f"生成的消息：{newReply}", LogLevel.INFO, LogChildType.LAST_CHILD)

    elif action == "cancel":
        await query.edit_message_text("[已取消]")
        _deleteReviewEntry(context.bot_data, key)
        await logAction("System", f"LLM 生成内容审核取消：{chatID}", f"原文：{reviewData['originalMsg']}", LogLevel.INFO, LogChildType.WITH_ONE_CHILD)




@handleTelegramErrors(errorReply="……诶、操作好像出了点问题喵……")
async def handleMemoryReviewCallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理记忆审核按钮点击无权用户点击时静默忽略"""
    query = update.callback_query
    clickerID = str(query.from_user.id) if query.from_user else None
    if not clickerID:
        return

    _cleanupExpiredReviews(context.bot_data)

    parts = query.data.split(":")
    if len(parts) != 5:
        await query.answer()
        await query.edit_message_text("[无效的操作喵]")
        return
    action, chatID, msgID = parts[2:]

    key = _memoryReviewKey(chatID, msgID)
    reviewData = context.bot_data.get(key)
    if not reviewData:
        await query.answer()
        await query.edit_message_text("[记忆审核已过期喵]")
        return

    opsID = str(reviewData["opsID"])
    if clickerID != opsID or not hasPermission(clickerID, Permission.LLM):
        return

    await query.answer()

    actionData = reviewData["action"]

    if action == "approve":
        memAction = MemoryAction.fromDict(actionData)
        success = await executeAction(memAction)
        status = "成功" if success else "失败"

        resultText = (
            f"[记忆操作已批准 - {status}]\n\n"
            f"操作: {actionData.get('action', '?').upper()}\n"
            f"范围: {actionData.get('scopeType', '?')}:{actionData.get('scopeID', 'global')}\n"
        )
        if actionData.get("content"):
            resultText += f"内容: {_truncate(actionData['content'], 300)}\n"
        await query.edit_message_text(_truncate(resultText, _TG_MAX_LEN))

        _deleteReviewEntry(context.bot_data, key)
        await logAction(
            "System",
            f"LLM 记忆操作审核通过 ({status})",
            f"action={actionData.get('action')}, scope={actionData.get('scopeType')}:{actionData.get('scopeID', '')}, content={str(actionData.get('content', ''))[:100]}",
            LogLevel.INFO, LogChildType.WITH_ONE_CHILD,
        )

    elif action == "cancel":
        await query.edit_message_text("[记忆操作已取消]")
        _deleteReviewEntry(context.bot_data, key)
        await logAction(
            "System",
            f"LLM 记忆操作审核取消",
            f"action={actionData.get('action')}, scope={actionData.get('scopeType')}:{actionData.get('scopeID', '')}",
            LogLevel.INFO, LogChildType.WITH_ONE_CHILD,
        )




async def handleFeedbackRetry(message, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    处理 ops 对审核消息的 :fb 补充反馈重试
    返回 True 表示消息已处理，调用方应 return

    安全约束：
        - 只有拥有 Permission.LLM 的 ops 本人才能补充反馈
        - 无权用户或点错消息时静默忽略，不打断正常群聊/审核流程
        - 只支持回复审核（不支持记忆审核）
    """
    if not (message.reply_to_message and message.text and message.text.startswith(":fb ")):
        return False

    senderID = str(message.from_user.id) if message.from_user else None
    if not senderID or not hasPermission(senderID, Permission.LLM):
        return True

    replyToID = message.reply_to_message.message_id
    reviewKey = (context.bot_data or {}).get(_editIndexKey(replyToID))
    if not reviewKey:
        return True

    reviewData = context.bot_data.get(reviewKey)
    if not reviewData or str(reviewData.get("opsID")) != senderID:
        return True

    # 只支持回复审核（不支持记忆审核）
    if reviewKey.startswith("llm_memreview_"):
        return True

    feedback = message.text[4:].strip()
    if not feedback:
        return True

    # 限制反馈长度
    MAX_FEEDBACK_LENGTH = 200
    if len(feedback) > MAX_FEEDBACK_LENGTH:
        feedback = feedback[:MAX_FEEDBACK_LENGTH]

    # 更新审核消息显示"正在重新生成"
    chatIDRetry = reviewData["chatID"]
    msgIDRetry = _reviewMsgIDFromKey(reviewKey)
    await context.bot.edit_message_text(
        chat_id=senderID,
        message_id=replyToID,
        text=_formatReviewText(reviewData["originalMsg"], reviewData["reply"]) + "\n\n🔄 正在根据补充信息重新生成...",
        reply_markup=None,  # 禁用按钮，防止重复操作
    )

    try:
        # 生成增强消息
        enhancedMsg = f"{reviewData['originalMsg']}\n\n[背景信息补充：{feedback}]"

        newReply = await generateReply(
            enhancedMsg,
            str(reviewData["chatID"]),
            includeContext=bool(reviewData.get("includeContext")),
            userID=reviewData.get("userID"),
            urlContexts=reviewData.get("urlContexts"),
        )

        # 清理记忆块、校验并按 autoMode 分发（与 retry 路径相同）
        failedCount = 0
        if reviewData.get("includeContext"):
            newReply, validated, failedCount = await extractValidatedMemoryActions(
                newReply, logLabel="feedback retry",
            )
            await _dispatchMemoryActionsTelegram(
                validated,
                chatID=reviewData["chatID"],
                originalMsg=reviewData["originalMsg"],
                opsID=reviewData["opsID"],
                userID=reviewData.get("userID"),
                autoMode=reviewData.get("autoMode", "console"),
                context=context,
                logLabel="feedback retry",
            )

        # 如果有校验失败，附加警告
        warningText = ""
        if failedCount > 0:
            warningText = f"\n\n⚠️ {failedCount} 个记忆操作校验失败，已丢弃"

        # 更新 bot_data 和审核消息
        context.bot_data[reviewKey]["reply"] = newReply
        await context.bot.edit_message_text(
            chat_id=senderID,
            message_id=replyToID,
            text=_formatReviewText(reviewData["originalMsg"], newReply) + warningText,
            reply_markup=_buildReviewKeyboard(chatIDRetry, msgIDRetry),
        )

        # 删除 ops 的 :fb 消息
        await message.delete()

        await logAction(
            "System",
            f"LLM Telegram 审核：补充反馈重试",
            f"反馈：{feedback[:100]}",
            LogLevel.INFO,
            LogChildType.WITH_ONE_CHILD,
        )

    except Exception as e:
        # 恢复审核消息（移除"正在生成"提示）
        await context.bot.edit_message_text(
            chat_id=senderID,
            message_id=replyToID,
            text=_formatReviewText(reviewData["originalMsg"], reviewData["reply"]) + f"\n\n❌ 生成失败喵：{e}",
            reply_markup=_buildReviewKeyboard(chatIDRetry, msgIDRetry),
        )
        await logSystemEvent(
            "Telegram 补充反馈重试失败",
            f"error={e}",
            LogLevel.ERROR,
            exception=e,
        )

    return True




def register():
    return {
        "handlers": [
            CallbackQueryHandler(handleReviewCallback, pattern=r"^llm:review:"),
            CallbackQueryHandler(handleMemoryReviewCallback, pattern=r"^llm:memreview:"),
        ],
        "name": "LLM Telegram 审核",
        "description": "Telegram 端 LLM 审核按钮回调（回复与记忆操作）",
        "auth": False,
    }
