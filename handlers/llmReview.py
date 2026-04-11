"""
handlers/llmReview.py

Telegram 端 LLM 审核回调处理器。

职责：
    - 向 ops 发送审核消息（带 inline keyboard）
    - 处理审核按钮回调（发送 / 重试 / 取消）
    - 处理 ops 对审核消息的 :edit 编辑
    - 管理 bot_data 中的审核状态（含过期清理）

支持两类审核项：
    - llm_review_*：回复审核（发送 / 重试 / 取消 / :edit 编辑）
    - llm_memreview_*：LLM 记忆操作审核（批准 / 取消 / :edit 编辑 add/update）
"""

import time
from telegram.ext import ContextTypes, CallbackQueryHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup

from utils.llm import generateReply
from utils.llm.memory.action import MemoryAction, executeAction
from utils.llm.review import extractMemoryActionFields
from utils.operators import hasPermission
from config import Permission, LLM_REVIEW_TTL_SECONDS
from utils.logger import logAction, LogLevel, LogChildType
from utils.core.errorDecorators import handleTelegramErrors


_TG_MAX_LEN = 4096
_REPLY_PREVIEW_LEN = 1800  # 审核消息中 reply 预留长度（正文 + 框架文字后仍留余量）




def _deleteReviewEntry(bot_data: dict, key: str):
    """删除审核条目及其反向索引。"""
    bot_data.pop(key, None)
    # key 格式: llm_review_{chatID}_{msgID} 或 llm_memreview_{chatID}_{msgID}
    msgID = key.rsplit("_", 1)[-1]
    bot_data.pop(f"llm_editidx_{msgID}", None)


def _truncate(text: str, limit: int) -> str:
    """超出 limit 时截断并附加省略提示，保证返回长度不超过 limit。"""
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
        f"原始消息：{originalMsg}\n\n"
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
    处理 ops 对审核消息的 :edit 编辑。
    返回 True 表示消息已处理，调用方应 return。

    安全约束：
        - 只有拥有 Permission.LLM 的 ops 本人才能编辑自己的审核消息
        - 无权用户或点错消息时静默忽略，不打断正常群聊/审核流程
    """
    if not (message.reply_to_message and message.text.startswith(":edit ")):
        return False

    senderID = str(message.from_user.id) if message.from_user else None
    if not senderID or not hasPermission(senderID, Permission.LLM):
        return True

    replyToID = message.reply_to_message.message_id
    reviewKey = (context.bot_data or {}).get(f"llm_editidx_{replyToID}")
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
            msgIDEdit = reviewKey.split("_")[-1]
            await context.bot.edit_message_text(
                chat_id=senderID,
                message_id=replyToID,
                text=_formatMemoryReviewText(context.bot_data[reviewKey]["action"], reviewData["originalMsg"]),
                reply_markup=_buildMemoryReviewKeyboard(chatIDEdit, msgIDEdit),
            )
        else:
            context.bot_data[reviewKey]["reply"] = newText
            chatIDEdit = reviewData["chatID"]
            msgIDEdit = reviewKey.split("_")[-1]
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
):
    """
    发送 Telegram 审核消息（带 inline keyboard），并存储审核状态到 bot_data。
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
    key = f"llm_review_{chatID}_{reviewMsgID}"
    context.bot_data[key] = {
        "reply": reply,
        "originalMsg": originalMsg,
        "chatID": chatID,
        "opsID": opsID,
        "triggerMsgID": triggerMsgID,
        "userID": userID,
        "includeContext": includeContext,
        "createdAt": time.time(),
    }
    context.bot_data[f"llm_editidx_{reviewMsgID}"] = key




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
    发送 Telegram 记忆操作审核消息（带 inline keyboard），并存储审核状态到 bot_data。

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

    context.bot_data[f"llm_memreview_{chatID}_{reviewMsgID}"] = {
        "action": action,
        "originalMsg": originalMsg,
        "chatID": chatID,
        "opsID": opsID,
        "userID": userID,
        "createdAt": time.time(),
    }
    context.bot_data[f"llm_editidx_{reviewMsgID}"] = f"llm_memreview_{chatID}_{reviewMsgID}"




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




@handleTelegramErrors(errorReply="……诶、操作好像出了点问题喵……")
async def handleReviewCallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理审核按钮点击。无权用户点击时静默忽略。"""
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

    key = f"llm_review_{chatID}_{msgID}"
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
        await context.bot.send_message(
            chatID,
            _truncate(reviewData["reply"], _TG_MAX_LEN),
            reply_to_message_id=reviewData.get("triggerMsgID"),
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
            )
        except Exception as e:
            await context.bot.send_message(chat_id=opsID, text=f"重试失败：{e}")
            return

        await query.edit_message_text(
            text=_formatReviewText(reviewData["originalMsg"], newReply),
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
    """处理记忆审核按钮点击。无权用户点击时静默忽略。"""
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

    key = f"llm_memreview_{chatID}_{msgID}"
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
