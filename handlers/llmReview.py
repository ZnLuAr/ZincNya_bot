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

from config import Permission, LLM_REVIEW_TTL_SECONDS, LLM_REVIEW_REPLY_PREVIEW_LEN, TG_MESSAGE_MAX_LEN

from utils.core.errorDecorators import handleTelegramErrors
from utils.llm.memory.action import MemoryAction, executeAction
from utils.llm.review import dispatchMemoryActions, extractMemoryActionFields, reviewRetry, reviewRetryWithFeedback
from utils.core.logger import logAction, LogLevel, LogChildType, logSystemEvent
from utils.operators import hasPermission
from utils.markdownToHtml import convertMarkdownToHtml
from utils.telegramHelpers import escapeHtml, safeEditMessage, sendLLMReply, truncateText


_TG_MAX_LEN = TG_MESSAGE_MAX_LEN  # Telegram 消息上限（平台硬约束，复用 config）
_REPLY_PREVIEW_LEN = LLM_REVIEW_REPLY_PREVIEW_LEN

# 审核卡片 / 结果 / 日志的截断长度（按显示需要设定，非业务阈值）
_CARD_CONTENT_LEN = 500  # 记忆审核卡片中 content 的截断长度
_RESULT_CONTENT_LEN = 300  # 批准结果回显中 content 的截断长度
_LOG_LEN = 100  # 日志中截断展示的字符数
_ORIGINAL_MSG_LEN = 1800  # 原始消息区截断长度（与 reply 预览同量级，控制卡片总长 < TG 上限）

# 重复使用的动态后缀模板（记忆操作校验失败警告）
_MEMORY_FAILED_WARNING = "\n\n⚠️ {} 个记忆操作校验失败，已丢弃"




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


def _renderOriginalMsgBlock(originalMsg: str) -> str:
    """渲染原始消息区为 HTML blockquote。

    originalMsg 可能是 _injectReplyTextContext 的输出（含 [引用的消息]/[当前用户消息]
    标记 + <@sender>），拆成两个独立 blockquote 便于 ops 区分引用 vs 当前；否则整块
    一个 blockquote。先截断原始文本再 escape（铁律：截断 → 转义 → 拼），避免断实体。
    解析失败回退单 blockquote。
    """
    truncated = _truncate(originalMsg, _ORIGINAL_MSG_LEN)
    replyMark = "[引用的消息]"
    currentMark = "[当前用户消息]"
    if replyMark in truncated and currentMark in truncated:
        try:
            replyPart, _, afterCurrent = truncated.partition(currentMark)
            return (
                f"<blockquote>{escapeHtml(replyPart.strip())}</blockquote>"
                f"<blockquote>{escapeHtml(currentMark + chr(10) + afterCurrent.strip())}</blockquote>"
            )
        except Exception:
            pass
    return f"<blockquote>{escapeHtml(truncated)}</blockquote>"


def _formatReviewText(originalMsg: str, reply: str) -> str:
    """构造回复审核卡片 HTML（加粗标题 + blockquote + reply 渲染 Markdown）。

    各动态段先截断原始再转义/渲染（铁律），整体不裸截断 escape 文本。
    保留为纯文本入口（返回 HTML 字符串），供 _renderReviewCard 加 suffix 后发给 ops。
    """
    replyRendered = convertMarkdownToHtml(_truncate(reply, _REPLY_PREVIEW_LEN))
    originalBlock = _renderOriginalMsgBlock(originalMsg)
    return (
        f"<b>待审核</b>\n\n"
        f"<b>原始消息</b>\n{originalBlock}\n\n"
        f"<b>回复</b>\n{replyRendered}\n\n"
        f"<i>回复此消息以 :edit 修改 / :fb 补充反馈喵</i>"
    )


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
    """构造记忆审核卡片 HTML（字段加粗 + 触发消息 blockquote）。

    各动态段先截断原始再 escape（铁律）。触发消息同样走 _renderOriginalMsgBlock
    （用户 reply 触发记忆操作时，originalMsg 也可能含 [引用的消息]/[当前用户消息] 标记）。
    """
    f = extractMemoryActionFields(action)

    lines = [
        f"<b>记忆操作待审核 · {escapeHtml(f['actionType'].upper())}</b>",
        "",
        f"<b>范围</b>：{escapeHtml(f['scopeType'])}:{escapeHtml(f['scopeID'] or 'global')}",
    ]
    if f["memoryID"] is not None:
        lines.append(f"<b>目标 ID</b>：#{f['memoryID']}")
    if f["content"]:
        lines.append(f"<b>内容</b>：{escapeHtml(_truncate(f['content'], _CARD_CONTENT_LEN))}")
    if f["tags"]:
        lines.append(f"<b>标签</b>：{escapeHtml(', '.join(f['tags']))}")
    if f["priority"]:
        lines.append(f"<b>优先级</b>：{f['priority']}")
    if f["reason"]:
        lines.append(f"<b>理由</b>：{escapeHtml(_truncate(f['reason'], _CARD_CONTENT_LEN))}")
    lines.append("")
    lines.append("<b>触发消息</b>")
    lines.append(_renderOriginalMsgBlock(originalMsg))

    if f["actionType"] in ("add", "update"):
        lines.append("\n<i>回复此消息并以 :edit 开头修改记忆内容</i>")

    return "\n".join(lines)


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
):
    """
    发送 Telegram 审核消息（带 inline keyboard），并存储审核状态到 bot_data
    """
    # 一次发送带 keyboard（callback_data 不含 msgID，无需先发再改）
    text, markup = _renderReviewCard(originalMsg, reply, chatID)
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
            "<b>消息已发送</b>\n\n"
            "<b>原始消息</b>\n"
            f"{_renderOriginalMsgBlock(reviewData['originalMsg'])}\n\n"
            "<b>回复</b>\n"
            f"{convertMarkdownToHtml(_truncate(reviewData['reply'], _REPLY_PREVIEW_LEN))}"
        )
        await safeEditMessage(query.message, _truncate(sentText, _TG_MAX_LEN), parse_mode="HTML")

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
            await context.bot.send_message(chat_id=opsID, text=f"重试失败：{escapeHtml(str(e))}", parse_mode="HTML")
            return

        # memoryFailedCount 由 reviewRetry 写回（includeContext=False 时显式置 0）
        failedCount = newItem.get("memoryFailedCount", 0)
        warningText = _MEMORY_FAILED_WARNING.format(failedCount) if failedCount > 0 else ""
        textRetry, markupRetry = _renderReviewCard(
            reviewData["originalMsg"], newItem["reply"], chatID, suffix=warningText,
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
            f"<b>记忆操作已批准 · {escapeHtml(status)}</b>\n\n"
            f"<b>操作</b>：{escapeHtml(actionData.get('action', '?').upper())}\n"
            f"<b>范围</b>：{escapeHtml(actionData.get('scopeType', '?'))}:{escapeHtml(actionData.get('scopeID', 'global'))}\n"
        )
        if actionData.get("content"):
            resultText += f"<b>内容</b>：{escapeHtml(_truncate(actionData['content'], _RESULT_CONTENT_LEN))}\n"
        await safeEditMessage(query.message, _truncate(resultText, _TG_MAX_LEN), parse_mode="HTML")

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
    generatingText = _formatReviewText(originalMsg, currentReply) + "\n\n🔄 正在根据补充信息重新生成..."
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
            ),
            logLabel="feedback retry",
        )
    except Exception as e:
        # 恢复审核卡片（带按钮）；ValueError（反馈过长等用户可纠正错误）用友好 ⚠️，其余用 ❌
        prefix = f"⚠️ {escapeHtml(str(e))}" if isinstance(e, ValueError) else f"❌ 生成失败喵：{escapeHtml(str(e))}"
        recoverText, recoverMarkup = _renderReviewCard(
            originalMsg, currentReply, chatIDRetry, suffix=f"\n\n{prefix}",
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
    warningText = _MEMORY_FAILED_WARNING.format(failedCount) if failedCount > 0 else ""
    textFb, markupFb = _renderReviewCard(
        originalMsg, newItem["reply"], chatIDRetry, suffix=warningText,
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
