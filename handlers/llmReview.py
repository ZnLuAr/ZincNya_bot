"""
handlers/llmReview.py

Telegram 端 LLM 审核回调处理器（PTB 交互层）

职责：
    - 处理审核按钮回调（发送 / 重试 / 取消）
    - 处理 ops 对审核消息的 :edit 编辑
    - 处理 ops 对审核消息的 :fb 补充反馈重试

审核卡由发言、记忆能力（utils/llm/capability/）发出；卡片的文字与键盘、bot_data 里
审核项的存取、重试等审核动作在 utils/llm/review/。

支持两类审核项：
    - llm_review_*：回复审核（发送 / 重试 / 取消 / :edit 编辑 / :fb 补充反馈）
    - llm_memreview_*：LLM 记忆操作审核（批准 / 取消 / :edit 编辑 add/update）
"""

from telegram import Update
from telegram.ext import ContextTypes, CallbackQueryHandler

from config import Permission, TG_MESSAGE_MAX_LEN

from utils.core.errorDecorators import handleTelegramErrors
from utils.llm.capability import submitMemoryAction
from utils.llm.models import DisplayBlocks
from utils.llm.memory.action import MemoryAction, MemoryActionContext, executeAction
from utils.llm.review.actions import refreshMemoryReviewItem, reviewRetry, reviewRetryWithFeedback
from utils.llm.review.cards import (
    formatReviewText,
    renderMemoryReviewCard,
    renderOriginalMsgBlock,
    renderReviewCard,
    truncateCardText,
)
from utils.llm.review.items import (
    cleanupExpiredReviews,
    deleteReviewEntry,
    editIndexKey,
    isMemoryReviewKey,
    memoryReviewKey,
    replyReviewKey,
)
from utils.core.logger import logAction, LogLevel, LogChildType, logSystemEvent
from utils.operators import hasPermission
from utils.telegramHelpers import escapeHtml, safeEditMessage, sendLLMReply


_TG_MAX_LEN = TG_MESSAGE_MAX_LEN  # Telegram 消息上限（平台硬约束，复用 config）
_REPLY_PREVIEW_LEN = 1800  # send 回显中 reply 预览长度（与卡片同量级）

# 结果回显 / 日志的截断长度（按显示需要设定，非业务阈值）
_RESULT_CONTENT_LEN = 300  # 批准结果回显中 content 的截断长度
_LOG_LEN = 100  # 日志中截断展示的字符数




async def handleEditReply(message, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    处理 ops 对审核消息的 :edit 编辑
    返回 True 表示消息已处理，调用方应 return

    安全约束：
        - 只有拥有 Permission.LLM 的 ops 本人才能编辑审核消息
        - 无权用户或点错消息时静默忽略，不打断正常群聊/审核流程
    """
    if not (message.reply_to_message and message.text and message.text.startswith(":edit ")):
        return False

    senderID = str(message.from_user.id) if message.from_user else None
    if not senderID or not hasPermission(senderID, Permission.LLM):
        return True

    replyToID = message.reply_to_message.message_id
    reviewKey = (context.bot_data or {}).get(editIndexKey(replyToID))
    if not reviewKey:
        return True

    reviewData = context.bot_data.get(reviewKey)
    if not reviewData or str(reviewData.get("opsID")) != senderID:
        return True

    isMemoryReview = isMemoryReviewKey(reviewKey)

    newText = message.text[6:].strip()
    if newText:
        chatIDEdit = reviewData["chatID"]
        if isMemoryReview:
            # 记忆审核编辑，只允许 add/update
            actionData = reviewData.get("action", {})
            if actionData.get("action") not in ("add", "update"):
                return True
            context.bot_data[reviewKey]["action"]["content"] = newText
            # hint 是从原正文派生的检索元数据；人工改写正文后旧 hint 不再可信。
            context.bot_data[reviewKey]["action"]["retrievalHint"] = ""
            textEdit, markupEdit = renderMemoryReviewCard(
                context.bot_data[reviewKey]["action"], reviewData["originalMsg"], chatIDEdit,
                displayBlocks=reviewData.get("displayBlocks"),
            )
        else:
            context.bot_data[reviewKey]["reply"] = newText
            textEdit, markupEdit = renderReviewCard(
                reviewData["originalMsg"], newText, chatIDEdit,
                displayBlocks=reviewData.get("displayBlocks"),
            )
        await context.bot.edit_message_text(
            chat_id=senderID,
            message_id=replyToID,
            text=textEdit,
            reply_markup=markupEdit,
            parse_mode="HTML",
        )
        await message.delete()
    return True




def _makeTelegramDispatcher(context, *, autoMode: str, displayBlocks: DisplayBlocks | None = None):
    """
    构造 Telegram 端的 memoryDispatcher：把审核重试产生的记忆操作逐个交给
    submitMemoryAction，由当前审核人审核（console 模式进控制台队列，其余发审核卡）。

    重试路径固定 respectAutoApprove=False：不读 memoryAutoApprove，始终走审核；
    首次生成才读这个开关。displayBlocks 随审核卡透传（None 走退化渲染）。
    """
    async def _dispatcher(actions, *, chatID, originalMsg, opsID, userID, logLabel):
        for action in actions:
            await submitMemoryAction(
                action,
                bot=context.bot,
                botData=context.bot_data,
                autoMode=autoMode,
                opsList=[opsID],
                chatID=chatID,
                userID=userID,
                originalMsg=originalMsg,
                displayBlocks=displayBlocks,
                respectAutoApprove=False,
            )
        if actions:
            tail = "已加入 console 审核队列" if autoMode == "console" else "已发送 Telegram 审核"
            await logSystemEvent(
                f"LLM {logLabel} 生成记忆操作",
                f"{len(actions)} 个操作{tail}",
                LogLevel.INFO,
            )
    return _dispatcher




@handleTelegramErrors(errorReply="……诶、操作好像出了点问题……")
async def handleReviewCallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理回复审核按钮点击（发送 / 重试 / 取消）。无权用户点击时静默忽略。"""
    query = update.callback_query
    clickerID = str(query.from_user.id) if query.from_user else None
    if not clickerID:
        return

    cleanupExpiredReviews(context.bot_data)

    parts = query.data.split(":")
    if len(parts) != 4 or query.message is None:
        await query.answer()
        if query.message is not None:
            await safeEditMessage(query.message, "[无效的操作喵]", parse_mode="HTML")
        return
    action, chatID = parts[2:]
    chatID = int(chatID)
    msgID = str(query.message.message_id)

    key = replyReviewKey(chatID, msgID)
    reviewData = context.bot_data.get(key)
    if not reviewData:
        await query.answer()
        await safeEditMessage(query.message, "[消息已过期喵]", parse_mode="HTML")
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
            "[消息已发送]\n\n"
            f"{renderOriginalMsgBlock(reviewData['originalMsg'], reviewData.get('displayBlocks'))}\n\n"
            f"<b>回复</b>\n"
            f"<pre>{escapeHtml(truncateCardText(reviewData['reply'], _REPLY_PREVIEW_LEN))}</pre>"
        )
        await safeEditMessage(query.message, truncateCardText(sentText, _TG_MAX_LEN), parse_mode="HTML")

        deleteReviewEntry(context.bot_data, key)
        await logAction("System", f"LLM 生成内容审核通过：{chatID}", f"原文：{reviewData['originalMsg']}", LogLevel.INFO, LogChildType.WITH_CHILD)
        await logAction("System", "", f"生成的消息：{reviewData['reply']}", LogLevel.INFO, LogChildType.LAST_CHILD)

    elif action == "retry":
        # 中间态：禁用按钮，提示正在重新生成（与 :fb 路径同款交互——生成期间不可重复点击）
        generatingText = formatReviewText(
            reviewData["originalMsg"], reviewData["reply"], reviewData.get("displayBlocks"),
        ) + "\n\n🔄 正在重新生成..."
        await safeEditMessage(query.message, generatingText, parse_mode="HTML")

        try:
            newItem = await reviewRetry(
                reviewData,
                memoryDispatcher=_makeTelegramDispatcher(
                    context, autoMode=reviewData.get("autoMode", "console"),
                    displayBlocks=reviewData.get("displayBlocks"),
                ),
                logLabel="retry",
            )
        except Exception as e:
            # 失败在原卡上恢复并标注（与 :fb 路径同款），不再另发独立消息
            prefix = f"⚠️ {escapeHtml(str(e))}" if isinstance(e, ValueError) else f"❌ 重试失败喵：{escapeHtml(str(e))}"
            recoverText, recoverMarkup = renderReviewCard(
                reviewData["originalMsg"], reviewData["reply"], chatID, suffix=f"\n\n{prefix}",
                displayBlocks=reviewData.get("displayBlocks"),
            )
            await safeEditMessage(query.message, recoverText, reply_markup=recoverMarkup, parse_mode="HTML")
            if not isinstance(e, ValueError):
                await logSystemEvent(
                    "Telegram 审核重试失败",
                    f"chatID={chatID}, error={e}",
                    LogLevel.ERROR,
                    exception=e,
                )
            return

        # 成功但生成为空：不放行空的 reply（保留旧回复供发送/再试），恢复卡片并提示
        # （与 :fb 路径同款）
        if not newItem["reply"].strip():
            emptyText, emptyMarkup = renderReviewCard(
                reviewData["originalMsg"], reviewData["reply"], chatID,
                suffix="\n\n⚠️ 重新生成是空的喵，再试一次吧",
                displayBlocks=reviewData.get("displayBlocks"),
            )
            await safeEditMessage(query.message, emptyText, reply_markup=emptyMarkup, parse_mode="HTML")
            await logSystemEvent(
                "LLM 审核重试生成为空",
                f"chatID={chatID}",
                LogLevel.WARNING,
            )
            return

        textRetry, markupRetry = renderReviewCard(
            reviewData["originalMsg"], newItem["reply"], chatID,
            displayBlocks=reviewData.get("displayBlocks"),
        )
        await safeEditMessage(query.message, textRetry, reply_markup=markupRetry, parse_mode="HTML")
        context.bot_data[key]["reply"] = newItem["reply"]
        context.bot_data[key]["memoryQuery"] = newItem.get("memoryQuery")
        await logAction("System", f"LLM 生成内容审核重试：{chatID}", f"原文：{reviewData['originalMsg']}", LogLevel.INFO, LogChildType.WITH_CHILD)
        await logAction("System", "", f"生成的消息：{newItem['reply']}", LogLevel.INFO, LogChildType.LAST_CHILD)

    elif action == "cancel":
        await safeEditMessage(query.message, "[已取消]", parse_mode="HTML")
        deleteReviewEntry(context.bot_data, key)
        await logAction("System", f"LLM 生成内容审核取消：{chatID}", f"原文：{reviewData['originalMsg']}", LogLevel.INFO, LogChildType.WITH_ONE_CHILD)




@handleTelegramErrors(errorReply="……诶、操作好像出了点问题喵……")
async def handleMemoryReviewCallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理记忆审核按钮；批准使用目标快照，冲突时刷新卡片并要求再次确认。"""
    query = update.callback_query
    clickerID = str(query.from_user.id) if query.from_user else None
    if not clickerID:
        return

    cleanupExpiredReviews(context.bot_data)

    parts = query.data.split(":")
    if len(parts) != 4 or query.message is None:
        await query.answer()
        if query.message is not None:
            await safeEditMessage(query.message, "[无效的操作喵]", parse_mode="HTML")
        return
    action, chatID = parts[2:]
    msgID = str(query.message.message_id)

    key = memoryReviewKey(chatID, msgID)
    reviewData = context.bot_data.get(key)
    if not reviewData:
        await query.answer()
        await safeEditMessage(query.message, "[记忆审核已过期喵]", parse_mode="HTML")
        return

    opsID = str(reviewData["opsID"])
    if clickerID != opsID or not hasPermission(clickerID, Permission.LLM):
        return

    await query.answer()

    actionData = reviewData["action"]

    if action == "approve":
        memAction = MemoryAction.fromDict(actionData)
        # 回调里的 actionData 可被模型影响，不能用其中的 scope_id 做授权；
        # 审核项保存的原始 chat/user 才是这次请求的可信身份边界。
        actionContext = MemoryActionContext(
            chatID=reviewData.get("chatID"),
            userID=reviewData.get("userID"),
        )
        success = await executeAction(
            memAction,
            humanApproved=True,
            expectedState=actionData.get("targetState"),
            actionContext=actionContext,
        )
        if not success:
            refreshed = await refreshMemoryReviewItem(reviewData)
            warning = (
                "目标状态已变化，需要核对当前内容后再次批准"
                if refreshed else
                "操作不能成功执行，目标可能已删除或不再允许修改"
            )
            retryText, retryMarkup = renderMemoryReviewCard(
                reviewData["action"],
                reviewData["originalMsg"],
                chatID,
                suffix=f"\n\n⚠️ {warning}",
                displayBlocks=reviewData.get("displayBlocks"),
            )
            await safeEditMessage(
                query.message,
                retryText,
                reply_markup=retryMarkup,
                parse_mode="HTML",
            )
            await logAction(
                "System",
                "LLM 记忆操作需要重新确认",
                f"action={actionData.get('action')}, scope={actionData.get('scopeType')}:{actionData.get('scopeID', '')}, id={actionData.get('memoryID')}",
                LogLevel.WARNING,
                LogChildType.WITH_ONE_CHILD,
            )
            return

        resultText = (
            "[记忆操作已批准 · 成功]\n\n"
            f"<b>操作</b>：{escapeHtml(actionData.get('action', '?').upper())}\n"
            f"<b>范围</b>：{escapeHtml(actionData.get('scopeType', '?'))}:{escapeHtml(actionData.get('scopeID', 'global'))}\n"
        )
        await safeEditMessage(query.message, truncateCardText(resultText, _TG_MAX_LEN), parse_mode="HTML")

        deleteReviewEntry(context.bot_data, key)
        await logAction(
            "System",
            "LLM 记忆操作审核通过 (成功)",
            f"action={actionData.get('action')}, scope={actionData.get('scopeType')}:{actionData.get('scopeID', '')}, id={actionData.get('memoryID')}",
            LogLevel.INFO, LogChildType.WITH_ONE_CHILD,
        )

    elif action == "cancel":
        await safeEditMessage(query.message, "[记忆操作已取消]", parse_mode="HTML")
        deleteReviewEntry(context.bot_data, key)
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

    委托 utils.llm.review.actions.reviewRetryWithFeedback 完成生成与记忆操作分发，
    本函数只负责 TG 卡片交互（正在生成中间态、成功/失败回填、:fb 消息清理）。
    反馈超长由 reviewRetryWithFeedback 抛 ValueError（友好提示），在此恢复卡片。

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
    reviewKey = (context.bot_data or {}).get(editIndexKey(replyToID))
    if not reviewKey:
        return True

    reviewData = context.bot_data.get(reviewKey)
    if not reviewData or str(reviewData.get("opsID")) != senderID:
        return True

    # 只支持回复审核（不支持记忆审核）
    if isMemoryReviewKey(reviewKey):
        return True

    feedback = message.text[4:].strip()
    if not feedback:
        return True

    chatIDRetry = reviewData["chatID"]
    originalMsg = reviewData["originalMsg"]
    currentReply = reviewData["reply"]

    # 中间态：禁用按钮，提示正在根据补充信息重新生成（纯文本，无 markup）
    generatingText = (
        formatReviewText(originalMsg, currentReply, reviewData.get("displayBlocks"))
        + "\n\n🔄 正在根据补充信息重新生成..."
    )
    await context.bot.edit_message_text(
        chat_id=senderID,
        message_id=replyToID,
        text=generatingText,
        reply_markup=None,
        parse_mode="HTML",
    )

    try:
        newItem = await reviewRetryWithFeedback(
            reviewData,
            feedback,
            memoryDispatcher=_makeTelegramDispatcher(
                context, autoMode=reviewData.get("autoMode", "console"),
                displayBlocks=reviewData.get("displayBlocks"),
            ),
            logLabel="feedback retry",
        )
    except Exception as e:
        # 恢复审核卡片（带按钮）；ValueError（反馈过长等用户可纠正错误）用友好 ⚠️，其余用 ❌
        prefix = f"⚠️ {escapeHtml(str(e))}" if isinstance(e, ValueError) else f"❌ 生成失败喵：{escapeHtml(str(e))}"
        recoverText, recoverMarkup = renderReviewCard(
            originalMsg, currentReply, chatIDRetry, suffix=f"\n\n{prefix}",
            displayBlocks=reviewData.get("displayBlocks"),
        )
        await context.bot.edit_message_text(
            chat_id=senderID,
            message_id=replyToID,
            text=recoverText,
            reply_markup=recoverMarkup,
            parse_mode="HTML",
        )
        if not isinstance(e, ValueError):
            await logSystemEvent(
                "Telegram 补充反馈重试失败",
                f"error={e}",
                LogLevel.ERROR,
                exception=e,
            )
        return True

    # 成功但生成为空：不写回空 reply（保留旧回复供发送/再试），恢复卡片并提示
    if not newItem["reply"].strip():
        emptyText, emptyMarkup = renderReviewCard(
            originalMsg, currentReply, chatIDRetry,
            suffix="\n\n⚠️ 重新生成是空的喵，再试一次吧",
            displayBlocks=reviewData.get("displayBlocks"),
        )
        await context.bot.edit_message_text(
            chat_id=senderID,
            message_id=replyToID,
            text=emptyText,
            reply_markup=emptyMarkup,
            parse_mode="HTML",
        )
        await message.delete()
        await logSystemEvent(
            "LLM 补充反馈重试生成为空",
            f"chatID={chatIDRetry}, feedback={feedback[:_LOG_LEN]}",
            LogLevel.WARNING,
        )
        return True

    textFb, markupFb = renderReviewCard(
        originalMsg, newItem["reply"], chatIDRetry,
        displayBlocks=reviewData.get("displayBlocks"),
    )
    await context.bot.edit_message_text(
        chat_id=senderID,
        message_id=replyToID,
        text=textFb,
        reply_markup=markupFb,
        parse_mode="HTML",
    )
    context.bot_data[reviewKey]["reply"] = newItem["reply"]
    context.bot_data[reviewKey]["memoryQuery"] = newItem.get("memoryQuery")
    await message.delete()

    await logAction(
        "System",
        "LLM Telegram 审核：补充反馈重试",
        f"反馈：{feedback[:_LOG_LEN]}",
        LogLevel.INFO,
        LogChildType.WITH_ONE_CHILD,
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
