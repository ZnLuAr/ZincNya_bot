"""
handlers/llmReview.py

Telegram 端 LLM 审核回调处理器（PTB 交互层）

职责：
    - 向 ops 发送审核消息（卡片文本与键盘的渲染在 utils/llm/review.py，本文件只做发送与状态存储）
    - 处理审核按钮回调（发送 / 重试 / 取消）
    - 处理 ops 对审核消息的 :edit 编辑
    - 处理 ops 对审核消息的 :fb 补充反馈重试
    - 管理 bot_data 中的审核状态（含过期清理）

支持两类审核项：
    - llm_review_*：回复审核（发送 / 重试 / 取消 / :edit 编辑 / :fb 补充反馈）
    - llm_memreview_*：LLM 记忆操作审核（批准 / 取消 / :edit 编辑 add/update）
"""

import time

from telegram import Update
from telegram.ext import ContextTypes, CallbackQueryHandler

from config import Permission, LLM_REVIEW_TTL_SECONDS, TG_MESSAGE_MAX_LEN

from utils.core.errorDecorators import handleTelegramErrors
from utils.llm.memory.action import MemoryAction, executeAction
from utils.llm.review import (
    MEMORY_FAILED_WARNING,
    dispatchMemoryActions,
    formatReviewText,
    renderMemoryReviewCard,
    renderOriginalMsgBlock,
    renderReviewCard,
    reviewRetry,
    reviewRetryWithFeedback,
    truncateCardText,
)
from utils.core.logger import logAction, LogLevel, LogChildType, logSystemEvent
from utils.operators import hasPermission
from utils.telegramHelpers import escapeHtml, safeEditMessage, sendLLMReply


_TG_MAX_LEN = TG_MESSAGE_MAX_LEN  # Telegram 消息上限（平台硬约束，复用 config）
_REPLY_PREVIEW_LEN = 1800  # send 回显中 reply 预览长度（与卡片同量级）

# 结果回显 / 日志的截断长度（按显示需要设定，非业务阈值；卡片渲染常量已随渲染函数迁 utils/llm/review.py）
_RESULT_CONTENT_LEN = 300  # 批准结果回显中 content 的截断长度
_LOG_LEN = 100  # 日志中截断展示的字符数




def _replyReviewKey(chatID, reviewMsgID) -> str:
    return f"llm_review_{chatID}_{reviewMsgID}"


def _memoryReviewKey(chatID, reviewMsgID) -> str:
    return f"llm_memreview_{chatID}_{reviewMsgID}"


def _editIndexKey(reviewMsgID) -> str:
    return f"llm_editidx_{reviewMsgID}"


def _reviewMsgIDFromKey(key: str) -> str:
    return key.rsplit("_", 1)[-1]


def _putReplyReview(bot_data: dict, *, chatID, reviewMsgID, reply, originalMsg, opsID, triggerMsgID, userID, includeContext, urlContexts=None, autoMode=None, displayBlocks=None) -> str:
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
        "displayBlocks": displayBlocks,
        "createdAt": time.time(),
    }
    bot_data[_editIndexKey(reviewMsgID)] = key
    return key


def _putMemoryReview(bot_data: dict, *, chatID, reviewMsgID, action, originalMsg, opsID, userID, displayBlocks=None) -> str:
    key = _memoryReviewKey(chatID, reviewMsgID)
    bot_data[key] = {
        "action": action,
        "originalMsg": originalMsg,
        "chatID": chatID,
        "opsID": opsID,
        "userID": userID,
        "displayBlocks": displayBlocks,
        "createdAt": time.time(),
    }
    bot_data[_editIndexKey(reviewMsgID)] = key
    return key


def _deleteReviewEntry(bot_data: dict, key: str):
    """删除审核条目及其反向索引"""
    bot_data.pop(key, None)
    bot_data.pop(_editIndexKey(_reviewMsgIDFromKey(key)), None)




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
        chatIDEdit = reviewData["chatID"]
        if isMemoryReview:
            # 记忆审核编辑，只允许 add/update
            actionData = reviewData.get("action", {})
            if actionData.get("action") not in ("add", "update"):
                return True
            context.bot_data[reviewKey]["action"]["content"] = newText
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
    displayBlocks=None,
):
    """
    发送 Telegram 审核消息（带 inline keyboard），并存储审核状态到 bot_data

    displayBlocks: 审核卡结构化展示块（None 走退化路径）
    """
    # 一次发送带 keyboard（callback_data 不含 msgID，无需先发再改）
    text, markup = renderReviewCard(originalMsg, reply, chatID, displayBlocks=displayBlocks)
    sent = await bot.send_message(chat_id=opsID, text=text, reply_markup=markup, parse_mode="HTML")
    reviewMsgID = sent.message_id

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
        displayBlocks=displayBlocks,
    )




async def sendMemoryReviewMessage(
    bot,
    opsID: int,
    action: dict,
    originalMsg: str,
    chatID: int | str,
    context: ContextTypes.DEFAULT_TYPE,
    userID: int | str | None = None,
    displayBlocks=None,
):
    """
    发送 Telegram 记忆操作审核消息（带 inline keyboard），并存储审核状态到 bot_data

    参数:
        action: MemoryAction 的 dict 形式
        originalMsg: 触发该操作的用户消息
        chatID: 原始聊天 ID
        displayBlocks: 审核卡结构化展示块（None 走退化路径）
    """
    text, markup = renderMemoryReviewCard(action, originalMsg, chatID, displayBlocks=displayBlocks)
    sent = await bot.send_message(chat_id=opsID, text=text, reply_markup=markup, parse_mode="HTML")
    reviewMsgID = sent.message_id

    _putMemoryReview(
        context.bot_data,
        chatID=chatID,
        reviewMsgID=reviewMsgID,
        action=action,
        originalMsg=originalMsg,
        opsID=opsID,
        userID=userID,
        displayBlocks=displayBlocks,
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




def _makeTelegramDispatcher(context, *, autoMode: str, displayBlocks=None):
    """
    构造 Telegram 端 memoryDispatcher 闭包，把 review.py 期望的 dispatcher 契约
    (actions, *, chatID, originalMsg, opsID, userID, logLabel) 适配到
    review.dispatchMemoryActions（注入 TG 发送回调 _sendTGMemoryReview，respectAutoApprove 固定 False）。

    retry/feedback 路径有意 respectAutoApprove=False（不读 memoryAutoApprove，始终走审核），
    与首生成路径（handlers/llm.py 用 respectAutoApprove=True）不同——这是既有设计。

    闭包捕获 context（TG bot）；TG 的 int() 类型转换在 _sendTGMemoryReview 内完成，
    review.py 不碰 Python-Telegram-Bot。displayBlocks 随审核卡透传（None 走退化渲染）。
    """
    async def _dispatcher(actions, *, chatID, originalMsg, opsID, userID, logLabel):
        async def _sendTGMemoryReview(actDict):
            await sendMemoryReviewMessage(
                bot=context.bot,
                opsID=int(opsID),
                action=actDict,
                originalMsg=originalMsg,
                chatID=int(chatID),
                context=context,
                userID=userID,
                displayBlocks=displayBlocks,
            )

        await dispatchMemoryActions(
            actions,
            autoMode=autoMode,
            opsList=[opsID],
            chatID=chatID,
            originalMsg=originalMsg,
            userID=userID,
            respectAutoApprove=False,
            sendTGMemoryReview=_sendTGMemoryReview,
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

    _cleanupExpiredReviews(context.bot_data)

    parts = query.data.split(":")
    if len(parts) != 4 or query.message is None:
        await query.answer()
        if query.message is not None:
            await safeEditMessage(query.message, "[无效的操作喵]", parse_mode="HTML")
        return
    action, chatID = parts[2:]
    chatID = int(chatID)
    msgID = str(query.message.message_id)

    key = _replyReviewKey(chatID, msgID)
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

        _deleteReviewEntry(context.bot_data, key)
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

        # memoryFailedCount 由 reviewRetry 写回（includeContext=False 时显式置 0）
        failedCount = newItem.get("memoryFailedCount", 0)
        warningText = MEMORY_FAILED_WARNING.format(failedCount) if failedCount > 0 else ""
        textRetry, markupRetry = renderReviewCard(
            reviewData["originalMsg"], newItem["reply"], chatID, suffix=warningText,
            displayBlocks=reviewData.get("displayBlocks"),
        )
        await safeEditMessage(query.message, textRetry, reply_markup=markupRetry, parse_mode="HTML")
        context.bot_data[key]["reply"] = newItem["reply"]
        await logAction("System", f"LLM 生成内容审核重试：{chatID}", f"原文：{reviewData['originalMsg']}", LogLevel.INFO, LogChildType.WITH_CHILD)
        await logAction("System", "", f"生成的消息：{newItem['reply']}", LogLevel.INFO, LogChildType.LAST_CHILD)

    elif action == "cancel":
        await safeEditMessage(query.message, "[已取消]", parse_mode="HTML")
        _deleteReviewEntry(context.bot_data, key)
        await logAction("System", f"LLM 生成内容审核取消：{chatID}", f"原文：{reviewData['originalMsg']}", LogLevel.INFO, LogChildType.WITH_ONE_CHILD)




@handleTelegramErrors(errorReply="……诶、操作好像出了点问题喵……")
async def handleMemoryReviewCallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理记忆审核按钮点击（批准 / 取消）。无权用户点击时静默忽略。"""
    query = update.callback_query
    clickerID = str(query.from_user.id) if query.from_user else None
    if not clickerID:
        return

    _cleanupExpiredReviews(context.bot_data)

    parts = query.data.split(":")
    if len(parts) != 4 or query.message is None:
        await query.answer()
        if query.message is not None:
            await safeEditMessage(query.message, "[无效的操作喵]", parse_mode="HTML")
        return
    action, chatID = parts[2:]
    msgID = str(query.message.message_id)

    key = _memoryReviewKey(chatID, msgID)
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
        success = await executeAction(memAction)
        status = "成功" if success else "失败"

        resultText = (
            f"[记忆操作已批准 · {escapeHtml(status)}]\n\n"
            f"<b>操作</b>：{escapeHtml(actionData.get('action', '?').upper())}\n"
            f"<b>范围</b>：{escapeHtml(actionData.get('scopeType', '?'))}:{escapeHtml(actionData.get('scopeID', 'global'))}\n"
        )
        if actionData.get("content"):
            resultText += f"<b>内容</b>：{escapeHtml(truncateCardText(actionData['content'], _RESULT_CONTENT_LEN))}\n"
        await safeEditMessage(query.message, truncateCardText(resultText, _TG_MAX_LEN), parse_mode="HTML")

        _deleteReviewEntry(context.bot_data, key)
        await logAction(
            "System",
            f"LLM 记忆操作审核通过 ({status})",
            f"action={actionData.get('action')}, scope={actionData.get('scopeType')}:{actionData.get('scopeID', '')}, content={str(actionData.get('content', ''))[:_LOG_LEN]}",
            LogLevel.INFO, LogChildType.WITH_ONE_CHILD,
        )

    elif action == "cancel":
        await safeEditMessage(query.message, "[记忆操作已取消]", parse_mode="HTML")
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

    委托 utils.llm.review.reviewRetryWithFeedback 完成生成与记忆操作分发，
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

    # 成功：memoryFailedCount 由 reviewRetryWithFeedback 写回
    failedCount = newItem.get("memoryFailedCount", 0)
    warningText = MEMORY_FAILED_WARNING.format(failedCount) if failedCount > 0 else ""
    textFb, markupFb = renderReviewCard(
        originalMsg, newItem["reply"], chatIDRetry, suffix=warningText,
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
