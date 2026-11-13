"""
Telegram LLM 入口，负责门禁、消息提取、防抖调度，以及任务异常结束时给触发方的提示。

后台流程顺序写在 _runLLMPipeline；共享数据在 models，任务入口在 task，操作循环在
operation，发言、记忆操作与 reaction 的送出在 capability，人工审核在 review。
"""

import asyncio
from dataclasses import replace

from telegram import Update
from telegram.constants import ChatType
from telegram.error import NetworkError
from telegram.ext import (
    filters,
    ContextTypes,
    MessageHandler,
)

from config import Permission, LLM_DEBOUNCE_SECONDS

from handlers.llmReview import handleEditReply, handleFeedbackRetry

from utils.core.errorDecorators import handleTelegramErrors
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
from utils.llm.capability import (
    makeReactionCapability,
    makeSendingMessageCapability,
    makeSubmittingMemoryCapability,
)
from utils.llm.client import isRetryableError
from utils.llm.config import getAutoMode, getLLMEnabled
from utils.llm.models import MessageInput, TaskRequest
from utils.llm.messagePrep import (
    getRawMessageText,
    getMessageOrigin,
    prepareMessageInput,
)
from utils.llm.operation import TaskOutcome, TaskStatus
from utils.llm.task import runRequest
from utils.llm.trigger import shouldTriggerLLM
from utils.llm.vision import downloadImages, extractImageRefsForPrompt
from utils.core.logger import logAction, LogLevel, LogChildType
from utils.operators import getOperatorsWithPermission
from utils.whitelistManager.data import whetherAuthorizedUser


# 没有 LLM ops 时发给触发方的说明：三种 autoMode 都要求有人为 bot 的发言负责
_HINT_NO_OPS = "诶——等等……管理员配置貌似有缺位……💦\n得有人为锌酱说的话负责，锌酱才可以畅所欲言不逾矩的说……"

# 任务没能正常完成时发给触发方的说明，每个任务最多一句
_HINT_MODEL_RETRYABLE = "……网络好像有些波动，锌酱没能接收到这条消息喵……可以再试一次吗？"
_HINT_MODEL_FAILED = "呜哇——有、有意料之外的错误正向咱袭来喵！"
_HINT_LIMIT_REACHED = "……这件事锌酱绕了好多圈也没能做完……\n\n先停在这里，换个说法再找咱试试吧💦"
_HINT_STOPPED = "……被管理员拦下来了……剩下的部分没能继续的说💦"
_HINT_UNFINISHED = "……做到一半出了点状况喵，剩下的部分没能做完💦"




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
    后台任务按原顺序取出消息，构造可复用的 TaskRequest。

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


async def _notifyAbnormalEnd(
    context: ContextTypes.DEFAULT_TYPE,
    chatID: str,
    outcome: TaskOutcome,
) -> None:
    """任务没能正常完成时，向触发方发一句固定说明；发送失败不再重试。"""
    if outcome.status == TaskStatus.LIMIT_REACHED:
        hint = _HINT_LIMIT_REACHED
    elif outcome.status == TaskStatus.STOPPED:
        # 运行中被停止与排队期间作废共用一句说明，差异记录在任务日志的 detail 里
        hint = _HINT_STOPPED
    elif outcome.status == TaskStatus.FAILED and not outcome.records and outcome.error is not None:
        # 一个操作都还没处理就失败：沿用模型请求失败的两句文案
        hint = _HINT_MODEL_RETRYABLE if isRetryableError(outcome.error) else _HINT_MODEL_FAILED
    else:
        hint = _HINT_UNFINISHED

    try:
        await context.bot.send_message(chat_id=chatID, text=hint)
    except NetworkError:
        pass




# ============================================================================
# 主处理器
# ============================================================================

async def _runLLMPipeline(
    *,
    debounceKey: str,
    context: ContextTypes.DEFAULT_TYPE,
):
    """
    防抖结束后聚合消息，运行一个任务；后台异常在此记录。

    任务内模型每输出一个操作，循环就交给这里构造的三个能力：发言和记忆操作按 autoMode
    直接送出或送审，reaction 直接生效，实际结果都交还模型（见 utils/llm/capability/）。
    """
    try:
        await asyncio.sleep(LLM_DEBOUNCE_SECONDS)

        # 收集防抖批次消息，成批组织起来
        messages = popPendingMessages(debounceKey)
        if not messages:
            return

        # autoMode / opsList 是全局配置而非消息属性，任务开始时读一次。
        # 三种 autoMode 都要求有负责的 ops；没有就不调用模型，也不消费一次性上下文标记。
        autoMode = getAutoMode()
        opsList = getOperatorsWithPermission(Permission.LLM)
        if not opsList:
            await context.bot.send_message(
                chat_id=messages[-1].origin.chatID, text=_HINT_NO_OPS
            )
            return

        includeContext = consumeContextOnce() or any(
            message.includeContext for message in messages
        )
        request = TaskRequest(
            messages=messages,
            includeContext=includeContext,
        )
        origin = request.origin

        # 发送 typing action，在状态栏中展示类似于 ZincNya is typing... 字样
        await _sendTypingActionSafely(context, origin.chatID)

        outcome = await runRequest(
            request,
            capabilities=[
                makeSendingMessageCapability(
                    request,
                    bot=context.bot, botData=context.bot_data,
                    autoMode=autoMode, opsList=opsList,
                ),
                makeSubmittingMemoryCapability(
                    request,
                    bot=context.bot, botData=context.bot_data,
                    autoMode=autoMode, opsList=opsList,
                ),
                # reaction 不经审核，三种 autoMode 下都直接生效
                makeReactionCapability(request, bot=context.bot),
            ],
        )

        # 模型至少成功响应过一次才计入速率限制（排队期间被作废的请求连模型都没调用）。
        # 任务失败只来自输入准备或模型请求出错，这时调用次数大于 1 说明前面的请求成功过。
        if outcome.modelCalls > 0 and (outcome.status != TaskStatus.FAILED or outcome.modelCalls > 1):
            addRateLimit(origin.userID)

        if outcome.status == TaskStatus.COMPLETED:
            if not outcome.records:
                # 模型选择不执行任何操作就结束：不是异常，不提示触发方
                await logAction(
                    "System", f"LLM 任务结束且没有操作：{origin.chatID}",
                    f"原文：{request.displayOriginalMsg}",
                    LogLevel.INFO, LogChildType.WITH_ONE_CHILD,
                )
            return

        await logAction(
            "System",
            f"LLM 任务未正常完成：{origin.chatID}",
            f"status={outcome.status.value}, 模型调用 {outcome.modelCalls} 次, "
            f"操作 {len(outcome.records)} 个, {outcome.detail} {outcome.error or ''}".rstrip(),
            LogLevel.ERROR if outcome.status == TaskStatus.FAILED else LogLevel.WARNING,
            LogChildType.WITH_ONE_CHILD,
        )
        await _notifyAbnormalEnd(context, origin.chatID, outcome)

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

    实际调用 LLM、处理模型输出的操作在 _runLLMPipeline 中异步执行
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

    # 下载图片并保存说明，prompt 与展示文本在消费处分别生成
    messageInput = await _downloadMessageImages(context.bot, imageRefs, messageInput)

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
            # 主消息处理器注册在 group 2（而非默认的 0）
            # 原因：
            #   - group 0：命令、mention dispatcher (shutdown)、回调等高优先级处理
            #   - group 1：留给需要先于 llm 运行、向 bot_data 推送层写入扩展上下文的 handler
            #   - group 2：llm 自动回复（本 handler），消费 bot_data 推送层的扩展上下文
            # shutdown 的 mention dispatcher 在 group 0，命中关键词后抛出 ApplicationHandlerStop，
            # 阻止后面各 group 运行，防止 "@bot 关机" 被误处理为聊天
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
