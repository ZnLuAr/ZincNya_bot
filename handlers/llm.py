"""
handlers/llm.py

LLM 消息处理器。

职责：
    - 监听群聊被 @ 和私聊消息
    - 权限检查（whitelist + llmEnabled）
    - 调用 LLM 生成回复
    - 根据 autoMode 分发结果（直接发送 / Telegram 审核 / 控制台审核）
    - 处理审核回调（发送 / 编辑 / 重试 / 取消）
"""




import re
import time
import asyncio
from telegram.constants import MessageEntityType
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReactionTypeEmoji
from telegram.ext import (
    filters,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
)

from telegram.error import NetworkError

from utils.core.errorDecorators import handleTelegramErrors
from utils.llm import (
    getAutoMode,
    addRateLimit,
    addReviewItem,
    generateReply,
    getLLMEnabled,
    isRateLimited,
    getPendingTask,
    setPendingTask,
    makeDebounceKey,
    clearPendingTask,
    getMemoryEnabled,
    consumeContextOnce,
    popPendingMessages,
    appendPendingMessage,
)
from config import Permission, LLM_DEBOUNCE_SECONDS, LLM_REVIEW_TTL_SECONDS
from utils.operators import loadOperators, hasPermission
from utils.logger import logAction, LogLevel, LogChildType
from utils.telegramHelpers import isMentioned, removeMention
from utils.whitelistManager.data import whetherAuthorizedUser


_TG_MAX_LEN = 4096
_REPLY_PREVIEW_LEN = 1800  # 审核消息中 reply 预留长度（正文 + 框架文字后仍留余量）




# ============================================================================
# 辅助函数
# ============================================================================

def _extractPureMessage(text: str, botUsername: str) -> tuple[str, bool]:
    """
    去除 @bot 并检查是否包含 #context 标记。

    参数:
        text: 原始消息文本
        botUsername: bot 用户名

    返回:
        (纯文本, 是否需要上下文)
        第二个值：#context 标记存在时为 True；否则跟随全局 memoryEnabled 配置。
        调用方还需将此值与 consumeContextOnce() 取逻辑或，以叠加 one-shot 标记。
    """
    text = removeMention(text, botUsername)

    # 检查 #context 标记（必须在消息开头，去除 @bot 后的第一个词）
    contextMatch = re.match(r"#context\b\s*(.*)", text, re.DOTALL)
    if contextMatch:
        pureText = contextMatch.group(1).strip()
        return pureText, True

    return text, getMemoryEnabled()


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


async def _handleEditReply(message, context: ContextTypes.DEFAULT_TYPE) -> bool:
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


async def _sendReviewMessage(
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




# ============================================================================
# 主处理器
# ============================================================================

async def _dispatchLLMReply(
    debounceKey: str,
    userID: int,
    username: str,
    chatID: str,
    triggerMsgID: int,
    context: ContextTypes.DEFAULT_TYPE,
):
    """
    防抖窗口结束后触发：聚合消息、调用 LLM、分发回复。

    由 handleLLMMessage 通过 asyncio.create_task 调用，
    不直接持有 update 对象（可能已过期），改用 chatID 和 triggerMsgID。
    """
    try:
        # 防抖等待：window 内若有新消息，旧 task 被 cancel，新 task 重新计时
        await asyncio.sleep(LLM_DEBOUNCE_SECONDS)

        # 取出并合并该防抖轮次中的所有待聚合消息
        parts = popPendingMessages(debounceKey)
        if not parts:
            return
        combinedText = "\n".join(text for text, _ in parts)
        includeContext = any(flag for _, flag in parts) or consumeContextOnce()

        # 显示 typing 状态
        try:
            await context.bot.send_chat_action(chat_id=chatID, action="typing")
        except Exception:
            pass

        # 调用 LLM
        try:
            reply = await generateReply(combinedText, chatID, includeContext=includeContext, userID=userID)
        except Exception as e:
            await logAction("System", f"LLM 生成回复失败：{chatID}", str(e), LogLevel.ERROR, LogChildType.WITH_ONE_CHILD)
            try:
                await context.bot.send_message(chat_id=chatID, text="呜……抱歉……刚刚、出了点问题喵……")
            except NetworkError:
                pass
            return

        # 记录调用时间（速率限制基于轮次，而非单条消息）
        addRateLimit(userID)

        # 根据审核模式分发（Telegram API 调用统一包装，防止超时逃逸到 Task）
        try:
            autoMode = getAutoMode()

            if autoMode == "on":
                await context.bot.send_message(
                    chat_id=chatID,
                    text=_truncate(reply, _TG_MAX_LEN),
                    reply_to_message_id=triggerMsgID,
                )
                await logAction("System", f"LLM 生成内容直接发送至 @{username}（{chatID}）", f"原文：{combinedText}", LogLevel.INFO, LogChildType.WITH_CHILD)
                await logAction("System", "", f"生成的消息：{reply}", LogLevel.INFO, LogChildType.LAST_CHILD)

            elif autoMode == "off":
                opsList = _getOpsWithLLMPermission()
                if not opsList:
                    await context.bot.send_message(
                        chat_id=chatID,
                        text="诶——等等……管理员配置貌似有缺位……💦\n得有人为锌酱说的话负责，锌酱才可以畅所欲言不逾矩的喵……"
                    )
                    return
                for opsID in opsList:
                    await _sendReviewMessage(
                        bot=context.bot,
                        opsID=int(opsID),
                        originalMsg=combinedText,
                        reply=reply,
                        chatID=int(chatID),
                        context=context,
                        triggerMsgID=triggerMsgID,
                        userID=userID,
                        includeContext=includeContext,
                    )
                await context.bot.set_message_reaction(
                    chat_id=chatID,
                    message_id=triggerMsgID,
                    reaction=[ReactionTypeEmoji(emoji="👀")],
                )
                await logAction("System", f"LLM 生成内容待审核：@{username}（{chatID}）", f"原文：{combinedText}", LogLevel.INFO, LogChildType.WITH_CHILD)
                await logAction("System", "", f"生成的消息：{reply}", LogLevel.INFO, LogChildType.LAST_CHILD)

            else:  # console
                opsList = _getOpsWithLLMPermission()
                if not opsList:
                    await context.bot.send_message(chat_id=chatID, text="诶——等等……管理员配置貌似有缺位……💦\n得有人为锌酱说的话负责，锌酱才可以畅所欲言不逾矩的喵……")
                    return
                addReviewItem(
                    chatID=chatID,
                    messageID=triggerMsgID,
                    originalMsg=combinedText,
                    reply=reply,
                    opsID=opsList[0],
                    userID=userID,
                    includeContext=includeContext,
                )
                await context.bot.set_message_reaction(
                    chat_id=chatID,
                    message_id=triggerMsgID,
                    reaction=[ReactionTypeEmoji(emoji="👀")],
                )
                await logAction("System", f"LLM 生成内容等待控制台审核：@{username}（{chatID}）", f"原文：{combinedText}", LogLevel.INFO, LogChildType.WITH_CHILD)
                await logAction("System", "", f"生成的消息：{reply}", LogLevel.INFO, LogChildType.LAST_CHILD)

        except NetworkError as e:
            await logAction("System", f"LLM 分发回复失败：{chatID}", str(e), LogLevel.ERROR, LogChildType.WITH_ONE_CHILD)

    except asyncio.CancelledError:
        raise
    finally:
        clearPendingTask(debounceKey)




@handleTelegramErrors(errorReply="……呜、刚才这条消息没能处理好喵……")
async def handleLLMMessage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    LLM 消息处理器（私聊 / 群聊被 @）。

    流程：LLM 开关检查 → :edit 审核捕获 → 白名单 + 速率限制 →
          防抖缓冲（appendPendingMessage）→ 取消旧任务 → 创建新防抖任务
    实际调用 LLM 和分发回复在 _dispatchLLMReply 中异步执行。
    """
    # 1. 检查 LLM 功能是否启用
    if not getLLMEnabled():
        return

    message = update.message
    if not message or not message.text:
        return

    # 过滤指令消息（/ 开头）——filter 层已排除，此处保留作双重保障
    if message.text.startswith("/"):
        return

    # 检测 ops 对审核消息的 reply 编辑（reply 原消息并以 :edit 新内容 开头）
    if await _handleEditReply(message, context):
        return

    # 2. 检查用户是否在 whitelist
    userID = message.from_user.id
    if not whetherAuthorizedUser(userID):
        return

    # 3. 检查速率限制（基于上一次完整轮次，而非单条消息）
    if isRateLimited(userID):
        await message.reply_text("……发得太快啦，锌酱的笨脑要跟不上了喵💦")
        return

    # 4. 群聊检测（私聊直接触发，群聊需要被 @）
    isPrivate = update.effective_chat.type == "private"
    if not isPrivate:
        botUsername = context.bot.username
        if not isMentioned(message, botUsername):
            return

    # 5. 提取纯文本和上下文标记（#context 标记 或 全局 memory）
    pureText, includeContext = _extractPureMessage(message.text, context.bot.username)
    if not pureText:
        return

    # 6. 防抖：同一 chat + user 独立聚合，one-shot 延后到真正 dispatch 时再消费
    chatID = str(update.effective_chat.id)
    debounceKey = makeDebounceKey(chatID, userID)
    if not appendPendingMessage(debounceKey, pureText, includeContext):
        await message.reply_text("……消息太多了喵，等锌酱处理完再发吧💦")
        return

    oldTask = getPendingTask(debounceKey)
    if oldTask and not oldTask.done():
        oldTask.cancel()

    username = message.from_user.username or message.from_user.first_name or str(userID)
    task = asyncio.create_task(
        _dispatchLLMReply(
            debounceKey=debounceKey,
            userID=userID,
            username=username,
            chatID=chatID,
            triggerMsgID=message.message_id,
            context=context,
        )
    )
    setPendingTask(debounceKey, task)
    task.add_done_callback(lambda t: clearPendingTask(debounceKey, t))




def _getOpsWithLLMPermission() -> list[str]:
    """获取拥有 LLM 权限的所有 ops ID"""
    operators = loadOperators()
    result = []
    for uid, data in operators.items():
        perms = data.get("permissions", [])
        if str(Permission.LLM) in perms:
            result.append(uid)
    return result




# ============================================================================
# 审核回调处理器
# ============================================================================

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
        # 重新生成
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




# ============================================================================
# 注册
# ============================================================================

def register():
    return {
        "handlers": [
            # 主消息处理器注册在 group 1，而非默认的 group 0。
            # 原因：shutdown.py 的 _mentionDispatch 也监听 MENTION 消息，
            # 且注册在 group 0。将 LLM 消息处理器置于 group 1 可确保
            # _mentionDispatch 优先匹配；若关键词命中，_mentionDispatch 会
            # 抛出 ApplicationHandlerStop 阻止本 handler 运行。
            {"handler": MessageHandler(
                filters.TEXT & ~filters.COMMAND & (
                    filters.ChatType.PRIVATE
                    | filters.Entity(MessageEntityType.MENTION)
                ),
                handleLLMMessage,
            ), "group": 1},
            # 审核按钮回调（group 0，无冲突）
            CallbackQueryHandler(handleReviewCallback, pattern=r"^llm:review:"),
        ],
        "name": "LLM 聊天",
        "description": "Claude LLM 自动回复",
        "auth": False,  # 内部自行检查 whitelist + llmEnabled
    }
