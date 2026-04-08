"""
handlers/llm.py

LLM 消息处理器。

职责：
    - 监听群聊被 @ 和私聊消息
    - 权限检查（whitelist + llmEnabled）
    - 调用 LLM 生成回复
    - 根据 autoMode 分发结果（直接发送 / Telegram 审核 / 控制台审核）
"""




import re
import asyncio
from telegram import Update, ReactionTypeEmoji
from telegram.constants import MessageEntityType
from telegram.ext import (
    filters,
    ContextTypes,
    MessageHandler,
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
from config import Permission, LLM_DEBOUNCE_SECONDS
from utils.operators import loadOperators
from utils.logger import logAction, LogLevel, LogChildType
from utils.telegramHelpers import isMentioned, removeMention
from utils.whitelistManager.data import whetherAuthorizedUser
from handlers.llmReview import handleEditReply, sendReviewMessage, _truncate


_TG_MAX_LEN = 4096




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
                    await sendReviewMessage(
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
    if await handleEditReply(message, context):
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




# ============================================================================
# 注册
# ============================================================================

def register():
    return {
        "handlers": [
            # 主消息处理器注册在 group 1，而非默认的 group 0。
            # 这是因为 shutdown.py 的 _mentionDispatch 也监听 MENTION 消息，
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
        ],
        "name": "LLM 聊天",
        "description": "Claude LLM 自动回复",
        "auth": False,  # 内部自行检查 whitelist + llmEnabled
    }
