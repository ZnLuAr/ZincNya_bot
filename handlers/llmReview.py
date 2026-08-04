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

from config import Permission, LLM_REVIEW_TTL_SECONDS, LLM_REVIEW_REPLY_PREVIEW_LEN

from utils.core.errorDecorators import handleTelegramErrors
from utils.llm.memory.action import MemoryAction, executeAction
from utils.llm.review import dispatchMemoryActions, extractMemoryActionFields, reviewRetry, reviewRetryWithFeedback
from utils.core.logger import logAction, LogLevel, LogChildType, logSystemEvent
from utils.operators import hasPermission
from utils.telegramHelpers import sendLLMReply, truncateText


_TG_MAX_LEN = 4096  # Telegram 消息上限（平台硬限制，非项目配置）
_REPLY_PREVIEW_LEN = LLM_REVIEW_REPLY_PREVIEW_LEN




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
    """审核卡片截断：委托 truncateText 并固定「内容过长」提示（保持既有文案）。"""
    return truncateText(text, limit, suffix="……[内容过长，已截断]")


def _formatReviewText(originalMsg: str, reply: str) -> str:
    """构造回复审核消息文本（自动截断超长内容）。保留为纯文本入口，供 +后缀 场景使用。"""
    reply = _truncate(reply, _REPLY_PREVIEW_LEN)
    text = (
        f"[待审核]\n\n"
        f"原始消息：\n{originalMsg}\n\n"
        f"---\n"
        f"{reply}\n"
        f"---\n\n"
        f"💡 回复此消息以 :edit 修改 / :fb 补充反馈喵"
    )
    return _truncate(text, _TG_MAX_LEN)


def _buildReviewKeyboard(chatID) -> InlineKeyboardMarkup:
    """构造回复审核 inline keyboard（callback_data 不含 msgID，由 query.message.message_id 取）"""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ 发送", callback_data=f"llm:review:send:{chatID}"),
            InlineKeyboardButton("🔄 重试", callback_data=f"llm:review:retry:{chatID}"),
            InlineKeyboardButton("❌ 取消", callback_data=f"llm:review:cancel:{chatID}"),
        ],
    ])


def _renderReviewCard(originalMsg: str, reply: str, chatID, suffix: str = "") -> tuple[str, InlineKeyboardMarkup]:
    """
    构造回复审核卡片 (text, markup)。
    suffix 接 retry/feedback 的动态后缀（warningText / 正在生成 / 失败恢复），整体再截断到 TG 上限。
    """
    text = _formatReviewText(originalMsg, reply) + suffix
    return _truncate(text, _TG_MAX_LEN), _buildReviewKeyboard(chatID)




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


def _buildMemoryReviewKeyboard(chatID) -> InlineKeyboardMarkup:
    """构造记忆审核 inline keyboard（callback_data 不含 msgID，由 query.message.message_id 取）"""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ 批准", callback_data=f"llm:memreview:approve:{chatID}"),
            InlineKeyboardButton("❌ 取消", callback_data=f"llm:memreview:cancel:{chatID}"),
        ],
    ])


def _renderMemoryReviewCard(action: dict, originalMsg: str, chatID, suffix: str = "") -> tuple[str, InlineKeyboardMarkup]:
    """
    构造记忆审核卡片 (text, markup)。
    memory 审核不支持 :fb（handleFeedbackRetry 拒绝 llm_memreview_），故提示只有 :edit。
    """
    text = _formatMemoryReviewText(action, originalMsg) + suffix
    return _truncate(text, _TG_MAX_LEN), _buildMemoryReviewKeyboard(chatID)




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
            textEdit, markupEdit = _renderMemoryReviewCard(
                context.bot_data[reviewKey]["action"], reviewData["originalMsg"], chatIDEdit,
            )
        else:
            context.bot_data[reviewKey]["reply"] = newText
            textEdit, markupEdit = _renderReviewCard(reviewData["originalMsg"], newText, chatIDEdit)
        await context.bot.edit_message_text(
            chat_id=senderID,
            message_id=replyToID,
            text=textEdit,
            reply_markup=markupEdit,
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
    # 一次发送带 keyboard（callback_data 不含 msgID，无需先发再改）
    text, markup = _renderReviewCard(originalMsg, reply, chatID)
    sent = await bot.send_message(chat_id=opsID, text=text, reply_markup=markup)
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
    text, markup = _renderMemoryReviewCard(action, originalMsg, chatID)
    sent = await bot.send_message(chat_id=opsID, text=text, reply_markup=markup)
    reviewMsgID = sent.message_id

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




def _makeTelegramDispatcher(context, *, autoMode: str):
    """
    构造 Telegram 端 memoryDispatcher 闭包，把 review.py 期望的 dispatcher 契约
    (actions, *, chatID, originalMsg, opsID, userID, logLabel) 适配到
    review.dispatchMemoryActions（注入 TG 发送回调 _sendTGMemoryReview，respectAutoApprove 固定 False）。

    retry/feedback 路径有意 respectAutoApprove=False（不读 memoryAutoApprove，始终走审核），
    与首生成路径（handlers/llm.py 用 respectAutoApprove=True）不同——这是既有设计。

    闭包捕获 context（TG bot）；TG 的 int() 类型转换在 _sendTGMemoryReview 内完成，
    review.py 不碰 PTB。
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




@handleTelegramErrors(errorReply="……诶、操作好像出了点问题喵……")
async def handleReviewCallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理审核按钮点击无权用户点击时静默忽略"""
    query = update.callback_query
    clickerID = str(query.from_user.id) if query.from_user else None
    if not clickerID:
        return

    _cleanupExpiredReviews(context.bot_data)

    parts = query.data.split(":")
    if len(parts) != 4 or query.message is None:
        await query.answer()
        if query.message is not None:
            await query.edit_message_text("[无效的操作喵]")
        return
    action, chatID = parts[2:]
    chatID = int(chatID)
    msgID = str(query.message.message_id)

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
            newItem = await reviewRetry(
                reviewData,
                memoryDispatcher=_makeTelegramDispatcher(
                    context, autoMode=reviewData.get("autoMode", "console"),
                ),
                logLabel="retry",
            )
        except Exception as e:
            await context.bot.send_message(chat_id=opsID, text=f"重试失败：{e}")
            return

        # memoryFailedCount 由 reviewRetry 写回（includeContext=False 时显式置 0）
        failedCount = newItem.get("memoryFailedCount", 0)
        warningText = f"\n\n⚠️ {failedCount} 个记忆操作校验失败，已丢弃" if failedCount > 0 else ""
        textRetry, markupRetry = _renderReviewCard(
            reviewData["originalMsg"], newItem["reply"], chatID, suffix=warningText,
        )
        await query.edit_message_text(text=textRetry, reply_markup=markupRetry)
        context.bot_data[key]["reply"] = newItem["reply"]
        await logAction("System", f"LLM 生成内容审核重试：{chatID}", f"原文：{reviewData['originalMsg']}", LogLevel.INFO, LogChildType.WITH_CHILD)
        await logAction("System", "", f"生成的消息：{newItem['reply']}", LogLevel.INFO, LogChildType.LAST_CHILD)

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
    if len(parts) != 4 or query.message is None:
        await query.answer()
        if query.message is not None:
            await query.edit_message_text("[无效的操作喵]")
        return
    action, chatID = parts[2:]
    msgID = str(query.message.message_id)

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
    generatingText = _formatReviewText(originalMsg, currentReply) + "\n\n🔄 正在根据补充信息重新生成..."
    await context.bot.edit_message_text(
        chat_id=senderID,
        message_id=replyToID,
        text=generatingText,
        reply_markup=None,
    )

    try:
        newItem = await reviewRetryWithFeedback(
            reviewData,
            feedback,
            memoryDispatcher=_makeTelegramDispatcher(
                context, autoMode=reviewData.get("autoMode", "console"),
            ),
            logLabel="feedback retry",
        )
    except Exception as e:
        # 恢复审核卡片（带按钮）；ValueError（反馈过长等用户可纠正错误）用友好 ⚠️，其余用 ❌
        prefix = f"⚠️ {e}" if isinstance(e, ValueError) else f"❌ 生成失败喵：{e}"
        recoverText, recoverMarkup = _renderReviewCard(
            originalMsg, currentReply, chatIDRetry, suffix=f"\n\n{prefix}",
        )
        await context.bot.edit_message_text(
            chat_id=senderID,
            message_id=replyToID,
            text=recoverText,
            reply_markup=recoverMarkup,
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
    warningText = f"\n\n⚠️ {failedCount} 个记忆操作校验失败，已丢弃" if failedCount > 0 else ""
    textFb, markupFb = _renderReviewCard(
        originalMsg, newItem["reply"], chatIDRetry, suffix=warningText,
    )
    await context.bot.edit_message_text(
        chat_id=senderID,
        message_id=replyToID,
        text=textFb,
        reply_markup=markupFb,
    )
    context.bot_data[reviewKey]["reply"] = newItem["reply"]
    await message.delete()

    await logAction(
        "System",
        "LLM Telegram 审核：补充反馈重试",
        f"反馈：{feedback[:100]}",
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
