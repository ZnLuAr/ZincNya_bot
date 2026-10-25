"""
Telegram LLM 入口，负责门禁、消息提取、防抖调度、生成与分发。

后台流程顺序写在 _runLLMPipeline；共享数据在 models，生成在 generation，审核在 review。
"""

import asyncio
from dataclasses import replace

from telegram import Update, ReactionTypeEmoji
from telegram.constants import ChatType
from telegram.error import NetworkError
from telegram.ext import (
    filters,
    ContextTypes,
    MessageHandler,
)

from config import Permission, LLM_DEBOUNCE_SECONDS, TG_MESSAGE_MAX_LEN

from handlers.llmReview import handleEditReply, handleFeedbackRetry, sendReviewMessage, sendMemoryReviewMessage

from utils.core.errorDecorators import handleTelegramErrors
from utils.core.stateManager import getStateManager

from utils.chatHistory import saveMessage
from utils.llm.state import (
    addRateLimit,
    appendPendingMessage,
    clearPendingTask,
    consumeContextOnce,
    getPendingTask,
    isRateLimited,
    makeDebounceKey,
    popPendingMessages,
    setPendingTask,
)
from utils.llm.client import isRetryableError
from utils.llm.config import getAutoMode, getLLMEnabled
from utils.llm.generation import generateResponse
from utils.llm.models import (
    DisplayBlocks, GeneratedOutput, GenerationRequest, GenerationResult, MessageInput,
)
from utils.llm.urlReader import readURLContextsForUserText, summarizeURLFetchResults
from utils.llm.messagePrep import (
    getRawMessageText,
    getMessageOrigin,
    prepareMessageInput,
)
from utils.llm.review import dispatchGeneratedOutput
from utils.llm.trigger import shouldTriggerLLM
from utils.llm.vision import downloadImages, extractImageRefsForPrompt
from utils.core.logger import logAction, LogLevel, LogChildType
from utils.operators import getOperatorsWithPermission
from utils.telegramHelpers import sendLLMReply
from utils.whitelistManager.data import whetherAuthorizedUser




# ============================================================================
# 辅助函数
# ============================================================================

def _getAuthorizedUserID(origin) -> int | None:
    if not origin.userID:
        return None
    userID = origin.userID
    if not whetherAuthorizedUser(userID):
        return None
    return userID


def _takeExtraBlocks(context, message) -> tuple[tuple, ...]:
    """取出当前消息对应的扩展上下文，避免不同消息串用临时槽位。"""
    key = f"llm_extra_blocks_{message.chat_id}_{message.message_id}"
    return tuple((context.bot_data or {}).pop(key, {}).values())


async def _downloadMessageImages(bot, imageRefs, messageInput: MessageInput) -> MessageInput:
    """下载图片，保留下载结果及失败说明。"""
    downloadedImages: list[dict] = []
    notes: list[str] = []
    if imageRefs:
        downloadedImages, notes = await downloadImages(bot, imageRefs)
    return replace(messageInput, images=tuple(downloadedImages), imageNotes=tuple(notes))


async def _enqueueLLMDebounce(
    *,
    message,
    context: ContextTypes.DEFAULT_TYPE,
    messageInput: MessageInput,
) -> bool:
    """
    将一条已提取的消息送入防抖缓冲，并（重新）拉起后台生成管线。

    messageInput 由 messagePrep.prepareMessageInput 产出，图片下载后与原文一起存入防抖缓冲；
    后台任务按原顺序取出消息，构造可复用的 GenerationRequest。

    防抖语义：同一 (chatID, userID) 键已有存活任务时取消旧任务（其 sleep 被打断、
    缓冲不被消费），新任务重新计时 LLM_DEBOUNCE_SECONDS，实现「等用户说完一起答」。

    返回 False 表示缓冲已达 LLM_PENDING_MSG_LIMIT（已向用户提示）。
    """
    debounceKey = makeDebounceKey(messageInput.origin.chatID, messageInput.origin.userID)
    if not appendPendingMessage(debounceKey, message=messageInput):
        await message.reply_text("……消息太多了喵，等锌酱处理完再发吧💦")
        return False

    oldTask = getPendingTask(debounceKey)
    if oldTask and not oldTask.done():
        oldTask.cancel()

    task = asyncio.create_task(
        _runLLMPipeline(
            debounceKey=debounceKey,
            context=context,
        )
    )
    setPendingTask(debounceKey, task)
    task.add_done_callback(lambda t: clearPendingTask(debounceKey, t))
    return True


async def _sendTypingActionSafely(context: ContextTypes.DEFAULT_TYPE, chatID: str) -> None:
    try:
        await context.bot.send_chat_action(chat_id=chatID, action="typing")
    except Exception:
        pass


async def _generateReplyOrNotify(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    request: GenerationRequest,
) -> GenerationResult | None:
    """生成回复；失败时记录原因并向用户提示，返回 None 终止本次流程。"""
    try:
        return await generateResponse(request)
    except Exception as e:
        await logAction("System", f"LLM 生成回复失败：{request.chatID}", str(e), LogLevel.ERROR, LogChildType.WITH_ONE_CHILD)
        if isRetryableError(e):
            errMsg = "……网络好像有些波动，锌酱没能接收到这条消息喵……可以再试一次吗？"
        else:
            errMsg = "呜哇——有、有意料之外的错误正向咱袭来喵！"
        try:
            await context.bot.send_message(chat_id=request.chatID, text=errMsg)
        except NetworkError:
            pass
        return None




# ============================================================================
# 主处理器
# ============================================================================

async def _runLLMPipeline(
    *,
    debounceKey: str,
    context: ContextTypes.DEFAULT_TYPE,
):
    """防抖结束后依次聚合消息、读取 URL、生成回复并分发；后台异常在此记录。"""
    try:
        await asyncio.sleep(LLM_DEBOUNCE_SECONDS)

        # 收集防抖批次消息，成批组织起来
        messages = popPendingMessages(debounceKey)
        if not messages:
            return

        includeContext = consumeContextOnce() or any(
            message.includeContext for message in messages
        )
        request = GenerationRequest(
            messages=messages,
            includeContext=includeContext,
        )
        origin = request.origin

        # URL 读取，根据用户发来的信息，判断用户意图、读取候选文本 URL 内容
        urlContexts = await readURLContextsForUserText(
            intentText=request.urlIntentText,
            candidateText=request.urlCandidateText,
        )
        request = replace(request, urlContexts=tuple(urlContexts))

        # 保存审核展示快照，生成输入仍使用 request 中的原始消息。
        displayOriginalMsg = request.displayOriginalMsg
        urlSummary = ""
        if urlContexts:
            # 需要包含 URL 摘要的情况下
            urlSummary = summarizeURLFetchResults(urlContexts)
            displayOriginalMsg += "\n" + urlSummary

        # 结构化展示块由请求中的消息配对派生，前缀只放图片和 URL 标注。
        prefixParts = []
        if request.images:
            prefixParts.append(f"[附带 {len(request.images)} 张图片]")
        if urlSummary:
            prefixParts.append(urlSummary)
        displayBlocks = DisplayBlocks(
            prefix="\n".join(prefixParts),
            pairs=request.displayPairs,
        )

        # 发送 typing action，在状态栏中展示类似于 ZincNya is typing... 字样
        await _sendTypingActionSafely(context, origin.chatID)

        # 调用 LLM、传递 URL 摘要与图片，生成回复内容
        result = await _generateReplyOrNotify(
            context=context,
            request=request,
        )
        if result is None:
            return

        # 仅在成功时添加速率限制
        addRateLimit(origin.userID)

        # 最终分发 output，包含 文字回复 与 记忆操作
        generated = GeneratedOutput(
            reply=result.reply,
            memoryActions=result.memoryActions,
            displayOriginalMsg=displayOriginalMsg,
            includeContext=request.includeContext,
            urlContexts=urlContexts,
            displayBlocks=displayBlocks,
            memoryQuery=request.memoryQuery,
            generationRequest=request,
        )

        # TG 侧回调闭包：review.py 不碰 PTB，int() 类型转换留在本侧完成
        async def _sendTGReview(opsID):
            await sendReviewMessage(
                bot=context.bot,
                opsID=int(opsID),
                generated=generated,
                origin=origin,
                context=context,
                autoMode=autoMode,
            )

        async def _sendTGMemoryReview(actDict):
            await sendMemoryReviewMessage(
                bot=context.bot,
                opsID=int(opsList[0]),
                action=actDict,
                originalMsg=generated.displayOriginalMsg,
                chatID=int(origin.chatID),
                context=context,
                userID=origin.userID,
                displayBlocks=generated.displayBlocks,
            )

        async def _setReaction(emoji):
            await context.bot.set_message_reaction(
                chat_id=origin.chatID,
                message_id=origin.messageID,
                reaction=[ReactionTypeEmoji(emoji=emoji)],
            )

        # autoMode / opsList 是全局配置而非消息属性，在分发前读取、经参数注入
        autoMode = getAutoMode()
        opsList = getOperatorsWithPermission(Permission.LLM)

        try:
            await dispatchGeneratedOutput(
                generated,
                origin=origin,
                autoMode=autoMode,
                opsList=opsList,
                bot=context.bot,
                sendTGReview=_sendTGReview,
                sendTGMemoryReview=_sendTGMemoryReview,
                sendReaction=_setReaction,
            )
        except NetworkError as e:
            # 发送侧网络错误：等待后仅 on 模式重试一次直接发送（审核卡不重发）
            await logAction("System", f"LLM 分发回复网络错误：{origin.chatID}", str(e), LogLevel.WARNING, LogChildType.WITH_ONE_CHILD)
            await asyncio.sleep(2)
            try:
                if generated.reply.strip() and autoMode == "on":
                    await sendLLMReply(
                        bot=context.bot,
                        chatID=origin.chatID,
                        reply=generated.reply,
                        replyToMessageID=origin.messageID,
                        maxLength=TG_MESSAGE_MAX_LEN,
                    )
                    await logAction("System", f"LLM 分发重试成功：{origin.chatID}", "", LogLevel.INFO, LogChildType.WITH_ONE_CHILD)
            except NetworkError as e2:
                await logAction("System", f"LLM 分发重试仍失败：{origin.chatID}", str(e2), LogLevel.ERROR, LogChildType.WITH_ONE_CHILD)

    except asyncio.CancelledError:
        raise

    except Exception as e:
        await logAction(
            "System",
            f"LLM 后台任务异常：{debounceKey}",
            str(e),
            LogLevel.ERROR,
            LogChildType.WITH_ONE_CHILD,
        )




@handleTelegramErrors(errorReply="……抱歉、刚才这条消息没能处理好呢……")
async def handleLLMMessage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    LLM 消息处理器（私聊 / 群聊被 @）

    支持纯文字、图片 + 触发、reply 含图消息三种路径
    流程：
        LLM 开关检查 → :edit 审核捕获 → 白名单 + 速率限制 →
        图片提取与下载 → 防抖缓冲 → 取消旧任务 → 创建新防抖任务

    实际调用 LLM 和分发回复在 _runLLMPipeline 中异步执行
    """
    message = update.message
    if not message:
        return

    extraBlocks = _takeExtraBlocks(context, message)

    if not getLLMEnabled():
        return

    rawText = getRawMessageText(message)

    # 再次判断收到的消息是否是命令
    if rawText.startswith("/"):
        return

    # 检查消息是否经过 ops 编辑，使带 :edit 标签的消息，其标签被消费，不触发 llm 回复
    if await handleEditReply(message, context):
        return

    # 检查消息是否为补充反馈重试，使带 :fb 标签的消息，其标签被消费，不触发 llm 回复
    if await handleFeedbackRetry(message, context):
        return

    # 检查文本是否非空，跳过仅适用于仅图片而无文本的情况
    if not rawText:
        return

    # 提取消息来源，并用其中的发送者 ID 校验白名单
    origin = getMessageOrigin(message)
    userID = _getAuthorizedUserID(origin)
    if userID is None:
        return

    # 触发判断（私聊 / @ / 关键词）
    isPrivate = update.effective_chat.type == ChatType.PRIVATE
    if not shouldTriggerLLM(message, context.bot.username, isPrivate):
        return

    if isRateLimited(userID):
        await message.reply_text("……发得太快啦，锌酱要跟不上了喵💦")
        return

    # 提取图片 Refs
    imageRefs = extractImageRefsForPrompt(message)

    # 提取单条消息数据
    messageInput = prepareMessageInput(message, rawText, context.bot.username, origin=origin)
    messageInput = replace(messageInput, extraBlocks=extraBlocks)
    if not messageInput.text:
        return

    chatID = origin.chatID

    # 下载图片并保存说明，prompt 与展示文本在消费处分别生成
    messageInput = await _downloadMessageImages(context.bot, imageRefs, messageInput)

    # 写入 incoming 历史（消息确实发生，与生成成败无关）。
    # /send -c receiver 活跃于该聊天时由 receiver 负责写入（其记录更全：含媒体标注、
    # 非触发消息），此处让位避免双写
    if getStateManager().getInteractiveChatID() != chatID:
        await saveMessage(chatID, "incoming", messageInput.origin.sender, rawText)

    # 进入防抖缓冲，取消旧任务并发起新任务
    await _enqueueLLMDebounce(
        message=message,
        context=context,
        messageInput=messageInput,
    )




# ============================================================================
# 注册
# ============================================================================

def register():
    return {
        "handlers": [
            # 主消息处理器注册在 group 2（而非默认的 0 或之前的 1）
            # 原因：
            #   - group 0：命令、mention dispatcher (shutdown)、回调等高优先级处理
            #   - group 1：afc 意图检测（独占），先于 llm 运行，注入工具上下文到 bot_data
            #   - group 2：llm 自动回复（本 handler），消费 bot_data 推送层的工具上下文
            # shutdown 的 mention dispatcher 在 group 0，命中关键词后抛出 ApplicationHandlerStop，
            # 阻止 group 1 的 afc 和 group 2 的 llm 运行，防止 "@bot 关机" 被误处理为聊天
            {"handler": MessageHandler(
                (filters.TEXT | filters.PHOTO | filters.Document.IMAGE)
                & ~filters.COMMAND,
                handleLLMMessage,
            ), "group": 2},
        ],
        "name": "LLM 聊天",
        "description": "LLM 自动回复",
        "auth": False,  # 内部自行检查 whitelist + llmEnabled
    }
