"""
handlers/llm.py

LLM 消息处理器。

职责：
    - 监听群聊被 @ 和私聊消息（文字 + 图片）
    - 权限检查（whitelist + llmEnabled）
    - 提取图片（同消息 photo/document 或 reply_to_message 中的图片）
    - 调用 LLM 生成回复
    - 根据 autoMode 分发结果（直接发送 / Telegram 审核 / 控制台审核）
    - 解析回复中的 <MEMORY_ACTION> 块，按 autoMode 分流记忆操作审核
    - 支持 memoryAutoApprove 自动执行模式
"""




import re
import asyncio

from telegram import Update, ReactionTypeEmoji
from telegram.constants import MessageEntityType
from telegram.error import NetworkError
from telegram.ext import (
    filters,
    ContextTypes,
    MessageHandler,
)

from config import Permission, LLM_DEBOUNCE_SECONDS

from handlers.llmReview import handleEditReply, sendReviewMessage, sendMemoryReviewMessage, _truncate

from utils.core.errorDecorators import handleTelegramErrors
from utils.llm import (
    addRateLimit,
    addReviewItem,
    appendPendingMessage,
    clearPendingTask,
    consumeContextOnce,
    generateReply,
    getAutoMode,
    getLLMEnabled,
    getMemoryAutoApprove,
    getMemoryEnabled,
    getPendingTask,
    isRateLimited,
    makeDebounceKey,
    popPendingMessages,
    setPendingTask,
)
from utils.llm.memory.action import (
    LLM_MEMORY_MAX_ACTIONS,
    executeAction as executeMemoryAction,
    formatActionDetail,
    parseMemoryActions,
    validateAction,
)
from utils.llm.state import addMemoryReviewItem
from utils.llm.vision import extractImageRefs, extractReplyImageRefs, downloadImages
from utils.logger import logAction, logSystemEvent, LogLevel, LogChildType
from utils.operators import loadOperators
from utils.telegramHelpers import isMentioned, removeMention
from utils.whitelistManager.data import whetherAuthorizedUser


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
        # 防抖等待，window 内若有新消息，旧 task 被 cancel，新 task 重新计时
        await asyncio.sleep(LLM_DEBOUNCE_SECONDS)

        # 取出并合并该防抖轮次中的所有待聚合消息
        parts = popPendingMessages(debounceKey)
        if not parts:
            return
        combinedText = "\n".join(text for text, _, _ in parts if text)
        includeContext = any(flag for _, flag, _ in parts) or consumeContextOnce()

        # 收集所有图片
        allImages: list[dict] = []
        for _, _, imgs in parts:
            allImages.extend(imgs)

        # 构造审核展示用的原始消息文本（含发送者和图片标注）
        if allImages:
            displayOriginalMsg = f"@{username}：[附带 {len(allImages)} 张图片]\n{combinedText}"
        else:
            displayOriginalMsg = f"@{username}：{combinedText}"

        # 在 Telegram 上边栏显示 typing... 状态
        try:
            await context.bot.send_chat_action(chat_id=chatID, action="typing")
        except Exception:
            pass

        # 调用 LLM
        try:
            reply = await generateReply(combinedText, chatID, includeContext=includeContext, userID=userID, images=(allImages or None))
        except Exception as e:
            await logAction("System", f"LLM 生成回复失败：{chatID}", str(e), LogLevel.ERROR, LogChildType.WITH_ONE_CHILD)
            try:
                await context.bot.send_message(chat_id=chatID, text="呜……抱歉……刚刚、出了点问题喵……")
            except NetworkError:
                pass
            return

        # 记录调用时间（速率限制基于轮次，而非单条消息）
        addRateLimit(userID)

        # 解析记忆操作（仅在 includeContext 时生效）
        memoryActions = []
        if includeContext:
            reply, memoryActions = parseMemoryActions(reply)
            # 截断超出上限的操作，并逐一校验
            if len(memoryActions) > LLM_MEMORY_MAX_ACTIONS:
                await logSystemEvent(
                    "LLM 记忆操作数量超限",
                    f"请求 {len(memoryActions)} 个，上限 {LLM_MEMORY_MAX_ACTIONS}，截断",
                    LogLevel.WARNING,
                )
                memoryActions = memoryActions[:LLM_MEMORY_MAX_ACTIONS]

            validatedActions = []
            for act in memoryActions:
                err = await validateAction(act)
                if err:
                    await logSystemEvent(
                        "LLM 记忆操作校验失败",
                        f"{act.action} | {err}",
                        LogLevel.WARNING,
                    )
                else:
                    validatedActions.append(act)
            memoryActions = validatedActions

        # 根据审核模式分发
        try:
            autoMode = getAutoMode()
            opsList = _getOpsWithLLMPermission()

            # 发送回复
            if reply.strip():
                if autoMode == "on":
                    await context.bot.send_message(
                        chat_id=chatID,
                        text=_truncate(reply, _TG_MAX_LEN),
                        reply_to_message_id=triggerMsgID,
                    )
                    await logAction("System", f"LLM 生成内容直接发送至 @{username}（{chatID}）", f"原文：{displayOriginalMsg}", LogLevel.INFO, LogChildType.WITH_CHILD)
                    await logAction("System", "", f"生成的消息：{reply}", LogLevel.INFO, LogChildType.LAST_CHILD)

                elif autoMode == "off":
                    if not opsList:
                        await context.bot.send_message(
                            chat_id=chatID,
                            text="诶——等等……管理员配置貌似有缺位……💦\n得有人为锌酱说的话负责，锌酱才可以畅所欲言不逾矩的喵……"
                        )
                    else:
                        for opsID in opsList:
                            await sendReviewMessage(
                                bot=context.bot,
                                opsID=int(opsID),
                                originalMsg=displayOriginalMsg,
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
                        await logAction("System", f"LLM 生成内容待审核：@{username}（{chatID}）", f"原文：{displayOriginalMsg}", LogLevel.INFO, LogChildType.WITH_CHILD)
                        await logAction("System", "", f"生成的消息：{reply}", LogLevel.INFO, LogChildType.LAST_CHILD)

                else:  # Console 审核模式下
                    if not opsList:
                        await context.bot.send_message(chat_id=chatID, text="诶——等等……管理员配置貌似有缺位……💦\n得有人为锌酱说的话负责，锌酱才可以畅所欲言不逾矩的喵……")
                    else:
                        addReviewItem(
                            chatID=chatID,
                            messageID=triggerMsgID,
                            originalMsg=displayOriginalMsg,
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
                        await logAction("System", f"LLM 生成内容等待控制台审核：@{username}（{chatID}）", f"原文：{displayOriginalMsg}", LogLevel.INFO, LogChildType.WITH_CHILD)
                        await logAction("System", "", f"生成的消息：{reply}", LogLevel.INFO, LogChildType.LAST_CHILD)

            # 分发记忆操作
            if memoryActions:
                memoryAutoApprove = getMemoryAutoApprove()

                if memoryAutoApprove:
                    # 直接执行，不审核
                    for act in memoryActions:
                        success = await executeMemoryAction(act)
                        status = "成功" if success else "失败"
                        await logAction(
                            "System", f"LLM 记忆操作自动执行 ({status})",
                            formatActionDetail(act),
                            LogLevel.INFO, LogChildType.WITH_ONE_CHILD,
                        )

                elif not opsList:
                    await logSystemEvent(
                        "LLM 记忆操作无审核人",
                        f"有 {len(memoryActions)} 个操作被丢弃（无 LLM ops）",
                        LogLevel.WARNING,
                    )
                else:
                    for act in memoryActions:
                        actDict = act.toDict()
                        # delete/update 缺少 content 时，查原记忆用于预览
                        if act.memoryID is not None and not act.content:
                            from utils.llm.memory.database import getMemoryByID
                            target = await getMemoryByID(act.memoryID)
                            if target:
                                actDict["originalContent"] = target.get("content", "")
                        if autoMode == "console":
                            addMemoryReviewItem(
                                action=actDict,
                                chatID=chatID,
                                originalMsg=displayOriginalMsg,
                                opsID=opsList[0],
                                userID=userID,
                            )
                        else:
                            # autoMode == "on" 或 "off" 均走 Telegram 审核
                            # 只发给第一个 ops，避免重复批准
                            await sendMemoryReviewMessage(
                                bot=context.bot,
                                opsID=int(opsList[0]),
                                action=actDict,
                                originalMsg=displayOriginalMsg,
                                chatID=int(chatID),
                                context=context,
                                userID=userID,
                            )

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

    支持纯文字、图片 + 触发、reply 含图消息三种路径。
    流程：LLM 开关检查 → :edit 审核捕获 → 白名单 + 速率限制 →
          图片提取与下载 → 防抖缓冲 → 取消旧任务 → 创建新防抖任务
    实际调用 LLM 和分发回复在 _dispatchLLMReply 中异步执行。
    """
    # 1. 检查 LLM 功能是否启用
    if not getLLMEnabled():
        return

    message = update.message
    if not message:
        return

    rawText = message.text or message.caption or ""

    # 过滤指令消息（/ 开头）
    # 其实在 filter 层已经排除，此处保留作双重保障
    if rawText.startswith("/"):
        return

    # 检测 ops 对审核消息的 reply 编辑（reply 原消息并以 :edit 新内容 开头）
    if await handleEditReply(message, context):
        return

    # 2. 提取图片引用：优先同消息图片，其次 reply 目标图片
    imageRefs = extractImageRefs(message)
    if not imageRefs:
        imageRefs = extractReplyImageRefs(message)

    # 纯图片无文字 → 不触发，静默忽略
    if not rawText:
        return

    # 3. 检查用户是否在 whitelist
    userID = message.from_user.id
    if not whetherAuthorizedUser(userID):
        return

    # 4. 检查速率限制（基于上一次完整轮次，而非单条消息）
    if isRateLimited(userID):
        await message.reply_text("……发得太快啦，锌酱的笨脑要跟不上了喵💦")
        return

    # 5. 群聊检测（私聊直接触发，群聊需要被 @）
    isPrivate = update.effective_chat.type == "private"
    if not isPrivate:
        botUsername = context.bot.username
        if not isMentioned(message, botUsername):
            return

    # 6. 提取纯文本和上下文标记（#context 标记 或 全局 memory）
    pureText, includeContext = _extractPureMessage(rawText, context.bot.username)
    if not pureText:
        return

    # 6.5 注入被回复消息的文本（让 LLM 知道用户在回复哪条消息）
    replyMsg = message.reply_to_message
    if replyMsg:
        replyText = replyMsg.text or replyMsg.caption or ""
        if replyText:
            replyUser = ""
            if replyMsg.from_user:
                replyUser = replyMsg.from_user.username or replyMsg.from_user.first_name or ""
            # 截断过长的引用，避免占用过多 token
            if len(replyText) > 300:
                replyText = replyText[:300] + "……"
            prefix = f"@{replyUser}" if replyUser else "某人"
            pureText = f"[回复 {prefix} 的消息: {replyText}]\n\n{pureText}"

    # 7. 下载图片（在 handler 阶段完成，此时有 bot 引用）
    downloadedImages: list[dict] = []
    if imageRefs:
        downloadedImages, notes = await downloadImages(context.bot, imageRefs)
        if notes:
            pureText = "\n".join(notes) + "\n" + pureText

    # 8. 防抖：同一 chat + user 独立聚合，one-shot 延后到真正 dispatch 时再消费
    chatID = str(update.effective_chat.id)
    debounceKey = makeDebounceKey(chatID, userID)
    if not appendPendingMessage(debounceKey, pureText, includeContext, images=downloadedImages):
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
    # 群聊中 @mention 可能出现在 text 或 caption 中
    _mentionOrCaptionMention = (
        filters.Entity(MessageEntityType.MENTION)
        | filters.CaptionEntity(MessageEntityType.MENTION)
    )

    return {
        "handlers": [
            # 主消息处理器注册在 group 1，而非默认的 group 0。
            # 这是因为 shutdown.py 的 _mentionDispatch 也监听 MENTION 消息，
            # 且注册在 group 0。将 LLM 消息处理器置于 group 1 可确保
            # _mentionDispatch 优先匹配；若关键词命中，_mentionDispatch 会
            # 抛出 ApplicationHandlerStop 阻止本 handler 运行。
            {"handler": MessageHandler(
                (filters.TEXT | filters.PHOTO | filters.Document.IMAGE)
                & ~filters.COMMAND
                & (filters.ChatType.PRIVATE | _mentionOrCaptionMention),
                handleLLMMessage,
            ), "group": 1},
        ],
        "name": "LLM 聊天",
        "description": "Claude LLM 自动回复（支持图片）",
        "auth": False,  # 内部自行检查 whitelist + llmEnabled
    }
