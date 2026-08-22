"""
handlers/llm.py

LLM 消息处理器（Telegram 接线层）

本文件只负责 PTB wiring：入口门禁、防抖调度、后台生成管线编排、
TG 审核发送回调。领域逻辑已下沉 utils/llm/：

    - utils/llm/messagePrep.py    消息文本准备（prompt 清洗 / reply 注入 / URL 意图切分）
    - utils/llm/trigger.py        群聊触发判断（私聊 / @entity / 关键词）
    - utils/llm/vision.py         图片引用提取（本消息 + reply 回退）与下载
    - utils/llm/urlReader.py      URL 读取（管线内按意图触发）
    - utils/llm/state.py          防抖缓冲与批次聚合（DebouncedBatch）
    - utils/llm/review.py         审核域：autoMode 三分流 / 空输出检测 / 记忆操作分流 /
                                  审核队列 item 契约 / TG 之外三端共用的审核原语

数据流（入口 → 分发）：
    update → handleLLMMessage（门禁 + PromptPayload + DispatchTarget 组装）
           → _enqueueLLMDebounce（防抖缓冲 + create_task）
           → _runLLMPipeline（DebouncedBatch 聚合 → URL 读取 → 生成 →
              GeneratedOutput 组装 → dispatchGeneratedOutput 经回调发 TG 审核卡 / reaction）

载体（frozen dataclass，PTB-free）：
    PromptPayload    入口阶段的消息文本载体（4 个 prompt 字段 + replyLine/currentText 展示切分，messagePrep）
    DispatchTarget   入口到分发全程稳定的定位四元组（review）
    DebouncedBatch   防抖窗口结束后的聚合批次（state）
    GeneratedOutput  一次生成的完整产出（review）
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
from utils.llm import (
    addRateLimit,
    appendPendingMessage,
    clearPendingTask,
    collectDebouncedBatch,
    generateReply,
    getAutoMode,
    getLLMEnabled,
    getPendingTask,
    isRateLimited,
    makeDebounceKey,
    setPendingTask,
)
from utils.llm.messagePrep import (
    DisplayBlocks,
    PromptPayload,
    formatDisplayOriginalMsg,
    getRawMessageText,
    getSenderDisplayName,
    preparePurePromptText,
)
from utils.llm.review import (
    DispatchTarget,
    GeneratedOutput,
    dispatchGeneratedOutput,
    extractValidatedMemoryActions,
)
from utils.llm.trigger import shouldTriggerLLM
from utils.llm.vision import downloadImages, extractImageRefsForPrompt
from utils.core.logger import logAction, LogLevel, LogChildType
from utils.operators import getOperatorsWithPermission
from utils.telegramHelpers import sendLLMReply
from utils.whitelistManager.data import whetherAuthorizedUser




# ============================================================================
# 辅助函数
# ============================================================================

def _isCommandLikeMessage(rawText: str) -> bool:
    return rawText.startswith("/")


def _getAuthorizedUserID(message) -> int | None:
    userID = message.from_user.id
    if not whetherAuthorizedUser(userID):
        return None
    return userID


async def _downloadImagesAndAnnotatePrompt(bot, imageRefs, pureText: str) -> tuple[str, list[dict], str]:
    """下载图片引用并把说明（过大 / 失败）前置注入 prompt 文本（同步阶段，失败可立即说明）。

    返回 (annotatedText, downloadedImages, notesText)——notesText 为说明行的换行拼接
    （无说明为空串），供调用方同时前置到 prompt 文本与展示侧 currentText（结构化拆分后
    图片说明归入「当前消息」段）。
    """
    downloadedImages: list[dict] = []
    notesText = ""
    if imageRefs:
        downloadedImages, notes = await downloadImages(bot, imageRefs)
        if notes:
            notesText = "\n".join(notes)
            pureText = notesText + "\n" + pureText
    return pureText, downloadedImages, notesText


async def _enqueueLLMDebounce(
    *,
    message,
    context: ContextTypes.DEFAULT_TYPE,
    target: DispatchTarget,
    payload: PromptPayload,
    images: list[dict],
) -> bool:
    """
    将一条已提取的消息送入防抖缓冲，并（重新）拉起后台生成管线。

    payload 来历：handleLLMMessage 阶段由 messagePrep.preparePurePromptText 产出
    （pureText 已含 reply 注入、图片下载说明由 _downloadImagesAndAnnotatePrompt
    以 replace() 更新进 pureText）；随后拆字段存入防抖缓冲（state.appendPendingMessage），
    后台侧由 state.collectDebouncedBatch 聚合为 DebouncedBatch——payload 本体不跨任务传递。

    防抖语义：同一 (chatID, userID) 键已有存活任务时取消旧任务（其 sleep 被打断、
    缓冲不被消费），新任务重新计时 LLM_DEBOUNCE_SECONDS，实现「等用户说完一起答」。

    返回 False 表示缓冲已达 LLM_PENDING_MSG_LIMIT（已向用户提示）。
    """
    debounceKey = makeDebounceKey(target.chatID, target.userID)
    if not appendPendingMessage(
        debounceKey,
        payload.pureText,
        payload.includeContext,
        images=images,
        urlIntentText=payload.urlIntentText,
        urlCandidateText=payload.urlCandidateText,
        replyLine=payload.replyLine,
        currentText=payload.currentText,
    ):
        await message.reply_text("……消息太多了喵，等锌酱处理完再发吧💦")
        return False

    oldTask = getPendingTask(debounceKey)
    if oldTask and not oldTask.done():
        oldTask.cancel()

    task = asyncio.create_task(
        _runLLMPipeline(
            debounceKey=debounceKey,
            target=target,
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
    combinedText: str,
    chatID: str,
    includeContext: bool,
    userID: int,
    allImages: list[dict],
    urlContexts: list[dict] | None = None,
) -> str | None:
    """
    调用 LLM 生成回复；失败时向用户发送错误提示。

    返回:
        str - 生成成功的回复文本
        None - 生成失败（错误提示已发出），调用方应立即 return，
               不记录速率限制、不进入分发

    错误分类：
        _isRetryable(e)（网络波动等瞬时错误）→ 「再试一次」文案；
        其余 → 「意料之外的错误」文案。提示发送自身的 NetworkError 静默吞掉
        （用户侧网络已断，重试无意义）。

    注意：_isRetryable 走函数内延迟 import——它是 client 私有符号，
    不值得提到模块顶层 import 面。
    """
    try:
        return await generateReply(
            combinedText,
            chatID,
            includeContext=includeContext,
            userID=userID,
            images=(allImages or None),
            urlContexts=urlContexts,
            telegramContext=context,
        )
    except Exception as e:
        from utils.llm.client._request import _isRetryable
        await logAction("System", f"LLM 生成回复失败：{chatID}", str(e), LogLevel.ERROR, LogChildType.WITH_ONE_CHILD)
        if _isRetryable(e):
            errMsg = "……网络好像有些波动，锌酱没能接收到这条消息喵……可以再试一次吗？"
        else:
            errMsg = "呜哇——有、有意料之外的错误正向咱袭来喵！"
        try:
            await context.bot.send_message(chat_id=chatID, text=errMsg)
        except NetworkError:
            pass
        return None




# ============================================================================
# 主处理器
# ============================================================================

async def _runLLMPipeline(
    *,
    debounceKey: str,
    target: DispatchTarget,
    context: ContextTypes.DEFAULT_TYPE,
):
    """
    后台生成管线：防抖窗口结束后 聚合消息 → 读取 URL → 生成回复 → 分发。

    由 _enqueueLLMDebounce 经 asyncio.create_task 拉起；不持有 update 对象
    （可能已过期），目标定位（chatID / userID / username / triggerMsgID）全部
    经 DispatchTarget 传入。

    分工：本函数负责编排与 PTB 接线；autoMode 三分流 / 空输出检测 / 记忆操作
    分流在 utils/llm/review.dispatchGeneratedOutput（TG 依赖经回调注入）。

    错误兜底：create_task 内异常不冒泡回 handler（@handleTelegramErrors 抓不到），
    除 CancelledError（防抖取消的常态，重新抛出）外在此记录 ERROR 日志。
    """
    try:
        await asyncio.sleep(LLM_DEBOUNCE_SECONDS)

        # 收集防抖批次消息，成批组织起来
        batch = collectDebouncedBatch(debounceKey)
        if batch is None:
            return

        # URL 读取，根据用户发来的信息，判断用户意图、读取候选文本 URL 内容
        from utils.llm.urlReader import readURLContextsForUserText, summarizeURLFetchResults
        urlContexts = await readURLContextsForUserText(
            intentText=batch.urlIntentText,
            candidateText=batch.urlCandidateText,
        )

        # 构造 displayOriginalMsg（字符串，日志/console item/memory 分发依赖）与
        # displayBlocks（结构化展示块）——header 同处派生，防两处组装漂移
        header = formatDisplayOriginalMsg(target.username, batch.combinedText, batch.images)
        displayOriginalMsg = header
        urlSummary = ""
        if urlContexts:
            # 需要包含 URL 摘要的情况下
            urlSummary = summarizeURLFetchResults(urlContexts)
            displayOriginalMsg += "\n" + urlSummary

        # 结构化展示块：prefix = 图片/URL 标注行；pairs 由防抖批次配对而来
        # （注意 header 含 combinedText，展示前缀只保留标注部分——由 formatDisplayOriginalMsg 拆分）
        prefixParts = []
        if batch.images:
            prefixParts.append(f"[附带 {len(batch.images)} 张图片]")
        if urlSummary:
            prefixParts.append(urlSummary)
        displayBlocks = DisplayBlocks(
            prefix="\n".join(prefixParts),
            pairs=list(batch.displayPairs),
        )

        # 发送 typing action，在状态栏中展示类似于 ZincNya is typing... 字样
        await _sendTypingActionSafely(context, target.chatID)

        # 调用 LLM、传递 URL 摘要与图片，生成回复内容
        reply = await _generateReplyOrNotify(
            context=context,
            combinedText=batch.combinedText,
            chatID=target.chatID,
            includeContext=batch.includeContext,
            userID=target.userID,
            allImages=batch.images,
            urlContexts=urlContexts,
        )
        if reply is None:
            return

        # 仅在成功时添加速率限制
        addRateLimit(target.userID)

        # 从 reply 中解析并校验 memory actions
        # 门禁：includeContext=False 时不解析，<MEMORY_ACTION> 块保留在 reply 原文中
        memoryActions = []
        if batch.includeContext:
            reply, memoryActions, _ = await extractValidatedMemoryActions(reply, logLabel="generate")

        # 最终分发 output，包含 文字回复 与 记忆操作
        generated = GeneratedOutput(
            reply=reply,
            memoryActions=memoryActions,
            displayOriginalMsg=displayOriginalMsg,
            includeContext=batch.includeContext,
            urlContexts=urlContexts,
            displayBlocks=displayBlocks,
        )

        # TG 侧回调闭包：review.py 不碰 PTB，int() 类型转换留在本侧完成
        async def _sendTGReview(opsID):
            await sendReviewMessage(
                bot=context.bot,
                opsID=int(opsID),
                originalMsg=generated.displayOriginalMsg,
                reply=generated.reply,
                chatID=int(target.chatID),
                context=context,
                triggerMsgID=target.triggerMsgID,
                userID=target.userID,
                includeContext=generated.includeContext,
                urlContexts=generated.urlContexts,
                autoMode=autoMode,
                displayBlocks=generated.displayBlocks,
            )

        async def _sendTGMemoryReview(actDict):
            await sendMemoryReviewMessage(
                bot=context.bot,
                opsID=int(opsList[0]),
                action=actDict,
                originalMsg=generated.displayOriginalMsg,
                chatID=int(target.chatID),
                context=context,
                userID=target.userID,
                displayBlocks=generated.displayBlocks,
            )

        async def _setReaction(emoji):
            await context.bot.set_message_reaction(
                chat_id=target.chatID,
                message_id=target.triggerMsgID,
                reaction=[ReactionTypeEmoji(emoji=emoji)],
            )

        # autoMode / opsList 是全局配置而非消息属性，在分发前读取、经参数注入
        #（放 try 外侧无意义——两行同步读不抛 NetworkError，且重试分支需要 autoMode）
        autoMode = getAutoMode()
        opsList = getOperatorsWithPermission(Permission.LLM)

        try:
            await dispatchGeneratedOutput(
                generated,
                target=target,
                autoMode=autoMode,
                opsList=opsList,
                bot=context.bot,
                sendTGReview=_sendTGReview,
                sendTGMemoryReview=_sendTGMemoryReview,
                sendReaction=_setReaction,
            )
        except NetworkError as e:
            # 发送侧网络错误：等待后仅 on 模式重试一次直接发送（审核卡不重发）
            await logAction("System", f"LLM 分发回复网络错误：{target.chatID}", str(e), LogLevel.WARNING, LogChildType.WITH_ONE_CHILD)
            await asyncio.sleep(2)
            try:
                if generated.reply.strip() and autoMode == "on":
                    await sendLLMReply(
                        bot=context.bot,
                        chatID=target.chatID,
                        reply=generated.reply,
                        replyToMessageID=target.triggerMsgID,
                        maxLength=TG_MESSAGE_MAX_LEN,
                    )
                    await logAction("System", f"LLM 分发重试成功：{target.chatID}", "", LogLevel.INFO, LogChildType.WITH_ONE_CHILD)
            except NetworkError as e2:
                await logAction("System", f"LLM 分发重试仍失败：{target.chatID}", str(e2), LogLevel.ERROR, LogChildType.WITH_ONE_CHILD)

    except asyncio.CancelledError:
        raise

    except Exception as e:
        await logAction(
            "System",
            f"LLM 后台任务异常：{target.chatID}",
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
    if not getLLMEnabled():
        return

    message = update.message
    if not message:
        return

    rawText = getRawMessageText(message)

    # 再次判断收到的消息是否是命令
    if _isCommandLikeMessage(rawText):
        return

    # 检查消息是否经过 ops 编辑，使带 :edit 标签的消息，其标签被消费，不触发 llm 回复
    if await handleEditReply(message, context):
        return

    # 检查消息是否为补充反馈重试，使带 :fb 标签的消息，其标签被消费，不触发 llm 回复
    if await handleFeedbackRetry(message, context):
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
    if not shouldTriggerLLM(message, context.bot.username, isPrivate):
        return

    if isRateLimited(userID):
        await message.reply_text("……发得太快啦，锌酱要跟不上了喵💦")
        return

    # 提取图片 Refs
    imageRefs = extractImageRefsForPrompt(message)

    # 构造 PromptPayload（pureText / includeContext / urlIntentText / urlCandidateText）
    payload = preparePurePromptText(message, rawText, context.bot.username)
    if not payload.pureText:
        return

    # 下载图片并补充 prompt 说明——frozen 载体不可原地改写，用 replace 生成新 payload；
    # 说明行同步前置到 currentText（展示侧「当前消息」段），时间线必须在 _enqueueLLMDebounce 之前
    annotatedText, downloadedImages, notesText = await _downloadImagesAndAnnotatePrompt(context.bot, imageRefs, payload.pureText)
    annotatedCurrent = (notesText + "\n" + payload.currentText) if notesText else payload.currentText
    payload = replace(payload, pureText=annotatedText, currentText=annotatedCurrent)

    # 组装分发目标（从入口到分发全程稳定的定位四元组）
    target = DispatchTarget(
        chatID=str(update.effective_chat.id),
        userID=userID,
        username=getSenderDisplayName(message, userID),
        triggerMsgID=message.message_id,
    )

    # 进入防抖缓冲，取消旧任务并发起新任务
    await _enqueueLLMDebounce(
        message=message,
        context=context,
        target=target,
        payload=payload,
        images=downloadedImages,
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
