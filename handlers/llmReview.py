"""
handlers/llmReview.py

Telegram 端 LLM 审核回调处理器。

职责：
    - 向 ops 发送审核消息（带 inline keyboard）
    - 处理审核按钮回调（发送 / 重试 / 取消）
    - 处理 ops 对审核消息的 :edit 编辑
    - 管理 bot_data 中的审核状态（含过期清理）
"""

import time
from telegram.ext import ContextTypes, CallbackQueryHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup

from utils.llm import generateReply
from utils.operators import hasPermission
from config import Permission, LLM_REVIEW_TTL_SECONDS
from utils.logger import logAction, LogLevel, LogChildType
from utils.core.errorDecorators import handleTelegramErrors


_TG_MAX_LEN = 4096
_REPLY_PREVIEW_LEN = 1800  # 审核消息中 reply 预留长度（正文 + 框架文字后仍留余量）




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
    reviewKey = None
    for k, v in (context.bot_data or {}).items():
        if k.startswith("llm_review_") and str(v.get("opsID")) == senderID and k.split("_")[-1] == str(replyToID):
            reviewKey = k
            break
    if not reviewKey:
        return True

    reviewData = context.bot_data[reviewKey]
    if str(reviewData.get("opsID")) != senderID:
        return True

    newText = message.text[6:].strip()
    if newText:
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
    context.bot_data[f"llm_review_{chatID}_{reviewMsgID}"] = {
        "reply": reply,
        "originalMsg": originalMsg,
        "chatID": chatID,
        "opsID": opsID,
        "triggerMsgID": triggerMsgID,
        "userID": userID,
        "includeContext": includeContext,
        "createdAt": time.time(),
    }




def _cleanupExpiredReviews(bot_data: dict):
    """清理 bot_data 中已过期的审核条目"""
    cutoff = time.time() - LLM_REVIEW_TTL_SECONDS
    expired = [
        k for k, v in bot_data.items()
        if k.startswith("llm_review_") and v.get("createdAt", 0) < cutoff
    ]
    for k in expired:
        del bot_data[k]




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

        del context.bot_data[key]
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
        del context.bot_data[key]
        await logAction("System", f"LLM 生成内容审核取消：{chatID}", f"原文：{reviewData['originalMsg']}", LogLevel.INFO, LogChildType.WITH_ONE_CHILD)




def register():
    return {
        "handlers": [
            CallbackQueryHandler(handleReviewCallback, pattern=r"^llm:review:"),
        ],
        "name": "LLM Telegram 审核",
        "description": "Telegram 端 LLM 审核按钮回调",
        "auth": False,
    }
