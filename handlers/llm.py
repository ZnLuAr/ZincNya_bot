"""
handlers/llm.py

LLM 消息处理器。

职责：
    - 监听群聊被 @ / 命中关键词 / 私聊消息（文字 + 图片）
    - 权限检查（whitelist + llmEnabled）
    - 提取图片（同消息 photo/document 或 reply_to_message 中的图片）
    - 当用户明确请求时按低信任策略读取 URL 内容（utils/llm/urlReader）
    - 调用 LLM 生成回复
    - 根据 autoMode 分发结果（直接发送 / Telegram 审核 / 控制台审核）
    - 解析回复中的 <MEMORY_ACTION> 块，按 autoMode 分流记忆操作审核
    - 支持 memoryAutoApprove 自动执行模式
"""

import re
import asyncio

from telegram import Update, ReactionTypeEmoji
from telegram.constants import MessageEntityType, ChatType
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
    getGroupTriggerKeywords,
    getGroupTriggerMode,
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
from utils.telegramHelpers import removeMention
from utils.whitelistManager.data import whetherAuthorizedUser


_TG_MAX_LEN = 4096




# ============================================================================
# 辅助函数
# ============================================================================

def _getRawMessageText(message) -> str:
    return message.text or message.caption or ""


def _isCommandLikeMessage(rawText: str) -> bool:
    return rawText.startswith("/")


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


def _isBotMentioned(message, botUsername: str) -> bool:
    """LLM 专用的 @bot 判断，直接按 Telegram mention entity 匹配"""
    expected = f"@{botUsername}".lower()
    for text, entities in (
        (message.text or "", message.entities or []),
        (message.caption or "", message.caption_entities or []),
    ):
        for entity in entities:
            if entity.type != MessageEntityType.MENTION:
                continue
            mention = text[entity.offset:entity.offset + entity.length].lower()
            if mention == expected:
                return True
    return False


def _matchesGroupTriggerKeyword(message) -> bool:
    """检查群聊消息是否命中 LLM 触发关键词"""
    text = (message.text or message.caption or "").strip().lower()
    if not text:
        return False
    return any(keyword and keyword in text for keyword in getGroupTriggerKeywords())


def _shouldTriggerLLM(message, botUsername: str, isPrivate: bool) -> bool:
    """判断当前消息是否应触发 LLM。私聊始终触发；群聊按配置触发"""
    if isPrivate:
        return True
    if _isBotMentioned(message, botUsername):
        return True
    if getGroupTriggerMode() == "keyword":
        return _matchesGroupTriggerKeyword(message)
    return False


def _getAuthorizedUserID(message) -> int | None:
    userID = message.from_user.id
    if not whetherAuthorizedUser(userID):
        return None
    return userID


def _extractImageRefsForLLM(message):
    imageRefs = extractImageRefs(message)
    if not imageRefs:
        imageRefs = extractReplyImageRefs(message)
    return imageRefs


def _injectReplyTextContext(message, pureText: str) -> str:
    replyMsg = message.reply_to_message
    if not replyMsg:
        return pureText

    replyText = replyMsg.text or replyMsg.caption or ""
    if not replyText:
        return pureText

    replyUser = ""
    if replyMsg.from_user:
        replyUser = replyMsg.from_user.username or replyMsg.from_user.first_name or ""
    if len(replyText) > 300:
        replyText = replyText[:300] + "……"
    prefix = f"@{replyUser}" if replyUser else "某人"
    return f"[回复 {prefix} 的消息: {replyText}]\n\n{pureText}"


def _getReplyURLCandidateText(message) -> str:
    """获取被回复消息的文本，用于 URL 候选提取"""
    replyMsg = message.reply_to_message
    if not replyMsg:
        return ""
    return replyMsg.text or replyMsg.caption or ""


def _preparePurePromptText(message, rawText: str, botUsername: str) -> tuple[str, bool, str, str]:
    """
    准备 LLM prompt text。

    返回:
        pureText:           给 LLM 的 prompt text（可包含 reply 文本注入）
        includeContext:     是否包含 memory/history
        urlIntentText:      当前用户消息去除 mention/#context 后的文本，用于判断 URL 读取意图
        urlCandidateText:   当前用户消息文本 + 被回复消息文本，用于提取 URL

    安全不变式：
        urlIntentText 必须在 _injectReplyTextContext 调用之前取值，
        否则 reply-to 消息里的"帮我总结"等文本会被误判为当前用户的意图，
        变成第三方无声触发 URL 抓取的攻击面。
    """
    pureText, includeContext = _extractPureMessage(rawText, botUsername)
    if not pureText:
        return "", includeContext, "", ""

    # 先取意图（仅当前消息），再做 reply 注入（包含 reply-to 文本）
    urlIntentText = pureText
    urlCandidateText = pureText + "\n" + _getReplyURLCandidateText(message)

    pureText = _injectReplyTextContext(message, pureText)

    return pureText, includeContext, urlIntentText, urlCandidateText


async def _downloadImagesAndAnnotatePrompt(bot, imageRefs, pureText: str) -> tuple[str, list[dict]]:
    downloadedImages: list[dict] = []
    if imageRefs:
        downloadedImages, notes = await downloadImages(bot, imageRefs)
        if notes:
            pureText = "\n".join(notes) + "\n" + pureText
    return pureText, downloadedImages


def _getSenderDisplayName(message, userID: int) -> str:
    return message.from_user.username or message.from_user.first_name or str(userID)


async def _enqueueLLMDebounce(
    *,
    message,
    context: ContextTypes.DEFAULT_TYPE,
    chatID: str,
    userID: int,
    username: str,
    pureText: str,
    includeContext: bool,
    downloadedImages: list[dict],
    urlIntentText: str,
    urlCandidateText: str,
) -> bool:
    debounceKey = makeDebounceKey(chatID, userID)
    if not appendPendingMessage(
        debounceKey,
        pureText,
        includeContext,
        images=downloadedImages,
        urlIntentText=urlIntentText,
        urlCandidateText=urlCandidateText,
    ):
        await message.reply_text("……消息太多了喵，等锌酱处理完再发吧💦")
        return False

    oldTask = getPendingTask(debounceKey)
    if oldTask and not oldTask.done():
        oldTask.cancel()

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
    return True


def _collectDebouncedBatch(debounceKey: str) -> tuple[str, bool, list[dict], str, str] | None:
    """
    收集防抖批次消息。

    返回:
        combinedText: 聚合后的 prompt text
        includeContext: 是否包含 memory/history
        allImages: 所有图片
        combinedURLIntentText: 聚合后的 URL 意图文本
        combinedURLCandidateText: 聚合后的 URL 候选文本
    """

    parts = popPendingMessages(debounceKey)
    if not parts:
        return None

    combinedText = "\n".join(p["text"] for p in parts if p["text"])
    hadOnce = consumeContextOnce()
    includeContext = any(p["includeContext"] for p in parts) or hadOnce

    allImages: list[dict] = []
    for p in parts:
        allImages.extend(p["images"])

    combinedURLIntentText = "\n".join(p["urlIntentText"] for p in parts if p["urlIntentText"])
    combinedURLCandidateText = "\n".join(p["urlCandidateText"] for p in parts if p["urlCandidateText"])

    return combinedText, includeContext, allImages, combinedURLIntentText, combinedURLCandidateText


def _formatDisplayOriginalMsg(username: str, combinedText: str, allImages: list[dict]) -> str:
    if allImages:
        return f"@{username}：[附带 {len(allImages)} 张图片]\n{combinedText}"
    return f"@{username}：{combinedText}"


async def _sendTypingActionSafely(context: ContextTypes.DEFAULT_TYPE, chatID: str) -> None:
    try:
        await context.bot.send_chat_action(chat_id=chatID, action="typing")
    except Exception:
        pass


async def _generateReplyOrNotify(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    combinedText: str,
    chatID: str,
    includeContext: bool,
    userID: int,
    allImages: list[dict],
    urlContexts: list[dict] | None = None,
) -> str | None:
    try:
        return await generateReply(
            combinedText,
            chatID,
            includeContext=includeContext,
            userID=userID,
            images=(allImages or None),
            urlContexts=urlContexts,
        )
    except Exception as e:
        from utils.llm.client._request import _isRetryable
        await logAction("System", f"LLM 生成回复失败：{chatID}", str(e), LogLevel.ERROR, LogChildType.WITH_ONE_CHILD)
        if _isRetryable(e):
            errMsg = "呜……网络好像有些波动，锌酱没能接收到这条消息喵……可以再试一次吗？"
        else:
            errMsg = "呜哇——有、有意料之外的错误正向咱袭来喵！"
        try:
            await context.bot.send_message(chat_id=chatID, text=errMsg)
        except NetworkError:
            pass
        return None


async def _parseAndValidateMemoryActions(reply: str, includeContext: bool) -> tuple[str, list]:
    memoryActions = []
    if not includeContext:
        return reply, memoryActions

    reply, memoryActions = parseMemoryActions(reply)
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
    return reply, validatedActions


async def _handleEmptyLLMOutputIfNeeded(
    *,
    reply: str,
    memoryActions: list,
    context: ContextTypes.DEFAULT_TYPE,
    chatID: str,
    triggerMsgID: int,
    username: str,
) -> bool:
    if reply.strip() or memoryActions:
        return False

    try:
        await context.bot.set_message_reaction(
            chat_id=chatID,
            message_id=triggerMsgID,
            reaction=[ReactionTypeEmoji(emoji="🤔")],
        )
    except Exception:
        pass
    await logSystemEvent(
        "LLM 回复为空",
        f"chatID={chatID}, user=@{username} | 回复为空且无有效记忆操作",
        LogLevel.WARNING,
        LogChildType.WITH_ONE_CHILD,
    )
    return True


async def _dispatchTextReply(
    *,
    reply: str,
    autoMode: str,
    opsList: list[str],
    context: ContextTypes.DEFAULT_TYPE,
    chatID: str,
    triggerMsgID: int,
    displayOriginalMsg: str,
    username: str,
    userID: int,
    includeContext: bool,
    urlContexts: list[dict] | None = None,
) -> None:
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
                    urlContexts=urlContexts,
                )
            await context.bot.set_message_reaction(
                chat_id=chatID,
                message_id=triggerMsgID,
                reaction=[ReactionTypeEmoji(emoji="👀")],
            )
            await logAction("System", f"LLM 生成内容待审核：@{username}（{chatID}）", f"原文：{displayOriginalMsg}", LogLevel.INFO, LogChildType.WITH_CHILD)
            await logAction("System", "", f"生成的消息：{reply}", LogLevel.INFO, LogChildType.LAST_CHILD)

    else:
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
                urlContexts=urlContexts,
            )
            await context.bot.set_message_reaction(
                chat_id=chatID,
                message_id=triggerMsgID,
                reaction=[ReactionTypeEmoji(emoji="👀")],
            )
            await logAction("System", f"LLM 生成内容等待控制台审核：@{username}（{chatID}）", f"原文：{displayOriginalMsg}", LogLevel.INFO, LogChildType.WITH_CHILD)
            await logAction("System", "", f"生成的消息：{reply}", LogLevel.INFO, LogChildType.LAST_CHILD)


async def _buildMemoryActionReviewPayload(act) -> dict:
    actDict = act.toDict()
    if act.memoryID is not None and not act.content:
        from utils.llm.memory.database import getMemoryByID
        target = await getMemoryByID(act.memoryID)
        if target:
            actDict["originalContent"] = target.get("content", "")
    return actDict


async def _dispatchMemoryActions(
    *,
    memoryActions: list,
    autoMode: str,
    opsList: list[str],
    context: ContextTypes.DEFAULT_TYPE,
    chatID: str,
    displayOriginalMsg: str,
    userID: int,
) -> None:
    if not memoryActions:
        return

    memoryAutoApprove = getMemoryAutoApprove()
    if memoryAutoApprove:
        for act in memoryActions:
            success = await executeMemoryAction(act)
            status = "成功" if success else "失败"
            await logAction(
                "System", f"LLM 记忆操作自动执行 ({status})",
                formatActionDetail(act),
                LogLevel.INFO, LogChildType.WITH_ONE_CHILD,
            )
        return

    if not opsList:
        await logSystemEvent(
            "LLM 记忆操作无审核人",
            f"有 {len(memoryActions)} 个操作被丢弃（无 LLM ops）",
            LogLevel.WARNING,
        )
        return

    for act in memoryActions:
        actDict = await _buildMemoryActionReviewPayload(act)
        if autoMode == "console":
            addMemoryReviewItem(
                action=actDict,
                chatID=chatID,
                originalMsg=displayOriginalMsg,
                opsID=opsList[0],
                userID=userID,
            )
        else:
            await sendMemoryReviewMessage(
                bot=context.bot,
                opsID=int(opsList[0]),
                action=actDict,
                originalMsg=displayOriginalMsg,
                chatID=int(chatID),
                context=context,
                userID=userID,
            )


async def _dispatchGeneratedOutput(
    *,
    reply: str,
    memoryActions: list,
    context: ContextTypes.DEFAULT_TYPE,
    chatID: str,
    triggerMsgID: int,
    displayOriginalMsg: str,
    username: str,
    userID: int,
    includeContext: bool,
    urlContexts: list[dict] | None = None,
) -> None:
    autoMode = None
    try:
        autoMode = getAutoMode()
        opsList = _getOpsWithLLMPermission()

        if await _handleEmptyLLMOutputIfNeeded(
            reply=reply,
            memoryActions=memoryActions,
            context=context,
            chatID=chatID,
            triggerMsgID=triggerMsgID,
            username=username,
        ):
            return

        if reply.strip():
            await _dispatchTextReply(
                reply=reply,
                autoMode=autoMode,
                opsList=opsList,
                context=context,
                chatID=chatID,
                triggerMsgID=triggerMsgID,
                displayOriginalMsg=displayOriginalMsg,
                username=username,
                userID=userID,
                includeContext=includeContext,
                urlContexts=urlContexts,
            )

        await _dispatchMemoryActions(
            memoryActions=memoryActions,
            autoMode=autoMode,
            opsList=opsList,
            context=context,
            chatID=chatID,
            displayOriginalMsg=displayOriginalMsg,
            userID=userID,
        )

    except NetworkError as e:
        await logAction("System", f"LLM 分发回复网络错误：{chatID}", str(e), LogLevel.WARNING, LogChildType.WITH_ONE_CHILD)
        await asyncio.sleep(2)
        try:
            if reply.strip() and autoMode == "on":
                await context.bot.send_message(
                    chat_id=chatID,
                    text=_truncate(reply, _TG_MAX_LEN),
                    reply_to_message_id=triggerMsgID,
                )
                await logAction("System", f"LLM 分发重试成功：{chatID}", "", LogLevel.INFO, LogChildType.WITH_ONE_CHILD)
        except NetworkError as e2:
            await logAction("System", f"LLM 分发重试仍失败：{chatID}", str(e2), LogLevel.ERROR, LogChildType.WITH_ONE_CHILD)




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
    后台任务异步处理器，负责处理 handleLLMMessage 发来的任务
    防抖窗口结束后触发 聚合消息、调用 LLM 与 分发回复。

    由 handleLLMMessage 通过 asyncio.create_task 调用，
    不直接持有 update 对象（可能已过期），改用 chatID 和 triggerMsgID
    """
    try:
        await asyncio.sleep(LLM_DEBOUNCE_SECONDS)

        # 收集防抖批次消息，成批组织起来
        batch = _collectDebouncedBatch(debounceKey)
        if batch is None:
            return
        combinedText, includeContext, allImages, urlIntentText, urlCandidateText = batch

        # URL 读取，根据用户发来的信息，判断用户意图、读取候选文本 URL 内容
        from utils.llm.urlReader import readURLContextsForUserText, summarizeURLFetchResults
        urlContexts = await readURLContextsForUserText(
            intentText=urlIntentText,
            candidateText=urlCandidateText,
        )

        # 构造 displayOriginalMsg，供初步展示
        displayOriginalMsg = _formatDisplayOriginalMsg(username, combinedText, allImages)
        if urlContexts:
            # 需要包含 URL 摘要的情况下
            displayOriginalMsg += "\n" + summarizeURLFetchResults(urlContexts)

        # 发送 typing action，在状态栏中展示类似于 ZincNya is typing... 字样
        await _sendTypingActionSafely(context, chatID)

        # 调用 LLM、传递 URL 摘要与图片，生成回复内容
        reply = await _generateReplyOrNotify(
            context=context,
            combinedText=combinedText,
            chatID=chatID,
            includeContext=includeContext,
            userID=userID,
            allImages=allImages,
            urlContexts=urlContexts,
        )
        if reply is None:
            return

        # 仅在成功时添加速率限制
        addRateLimit(userID)

        # 从 reply 中解析并校验 memory actions
        reply, memoryActions = await _parseAndValidateMemoryActions(reply, includeContext)

        # 最终分发 output，包含 文字回复 与 记忆操作
        await _dispatchGeneratedOutput(
            reply=reply,
            memoryActions=memoryActions,
            context=context,
            chatID=chatID,
            triggerMsgID=triggerMsgID,
            displayOriginalMsg=displayOriginalMsg,
            username=username,
            userID=userID,
            includeContext=includeContext,
            urlContexts=urlContexts,
        )

    except asyncio.CancelledError:
        raise

    except Exception as e:
        await logAction(
            "System",
            f"LLM 后台任务异常：{chatID}",
            str(e),
            LogLevel.ERROR,
            LogChildType.WITH_ONE_CHILD,
        )




@handleTelegramErrors(errorReply="……呜、刚才这条消息没能处理好喵……")
async def handleLLMMessage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    LLM 消息处理器（私聊 / 群聊被 @）。

    支持纯文字、图片 + 触发、reply 含图消息三种路径。
    流程：
        LLM 开关检查 → :edit 审核捕获 → 白名单 + 速率限制 →
        图片提取与下载 → 防抖缓冲 → 取消旧任务 → 创建新防抖任务

    实际调用 LLM 和分发回复在 _dispatchLLMReply 中异步执行。
    """
    if not getLLMEnabled():
        return

    message = update.message
    if not message:
        return

    rawText = _getRawMessageText(message)

    # 再次判断收到的消息是否是命令
    if _isCommandLikeMessage(rawText):
        return

    # 检查消息是否经过 ops 编辑。使带 :edit 标签的消息，其标签被消费，不触发 llm 回复
    if await handleEditReply(message, context):
        return

    # 检查文本是否非空，跳过仅适用于仅图片而无文本时
    if not rawText:
        return

    # 白名单校验并获取 userID 
    userID = _getAuthorizedUserID(message)
    if userID is None:
        return

    # 触发判断（私聊 / @ / 关键词）
    isPrivate = update.effective_chat.type == ChatType.PRIVATE
    if not _shouldTriggerLLM(message, context.bot.username, isPrivate):
        return

    if isRateLimited(userID):
        await message.reply_text("……发得太快啦，锌酱的笨脑要跟不上了喵💦")
        return

    # 提取图片 Refs
    imageRefs = _extractImageRefsForLLM(message)

    # 构造 pureText / includeContext / urlIntentText / urlCandidateText
    pureText, includeContext, urlIntentText, urlCandidateText = _preparePurePromptText(message, rawText, context.bot.username)
    if not pureText:
        return

    # 下载图片并补充 prompt 说明
    pureText, downloadedImages = await _downloadImagesAndAnnotatePrompt(context.bot, imageRefs, pureText)

    chatID = str(update.effective_chat.id)
    username = _getSenderDisplayName(message, userID)

    # 进入防抖缓冲，取消旧任务并发起新任务
    await _enqueueLLMDebounce(
        message=message,
        context=context,
        chatID=chatID,
        userID=userID,
        username=username,
        pureText=pureText,
        includeContext=includeContext,
        downloadedImages=downloadedImages,
        urlIntentText=urlIntentText,
        urlCandidateText=urlCandidateText,
    )




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
                (filters.TEXT | filters.PHOTO | filters.Document.IMAGE)
                & ~filters.COMMAND,
                handleLLMMessage,
            ), "group": 1},
        ],
        "name": "LLM 聊天",
        "description": "Claude LLM 自动回复（支持图片）",
        "auth": False,  # 内部自行检查 whitelist + llmEnabled
    }
