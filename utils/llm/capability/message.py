"""
utils/llm/capability/message.py

发言能力（message）：模型向当前聊天说一条话。

说明、校验与送出都在本文件。送出有两种方式：

    - makeSendingMessageCapability：真正送出，按 autoMode 分三路——on 直接发送，
      off 给每位 LLM ops 发一张 Telegram 审核卡，console 加入控制台审核队列。
      实际结果（已完成 / 已送审 / 失败 / 结果不确定）交还模型
    - makeCollectingMessageCapability：只收集不送出，审核重试用它拿到新的回复
"""

import asyncio
from typing import Awaitable, Callable

from telegram import ReactionTypeEmoji
from telegram.error import BadRequest, Forbidden, NetworkError, TelegramError, TimedOut

from config import TG_MESSAGE_MAX_LEN

from utils.core.logger import logAction, logSystemEvent, LogLevel, LogChildType
from utils.llm.models import TaskRequest
from utils.llm.operation.types import (
    Capability, Operation, OperationResult, OperationStatus, TaskInfo,
)
from utils.llm.review.cards import renderReviewCard
from utils.llm.review.items import addReviewItem, putReplyReview
from utils.telegramHelpers import sendLLMReply


MESSAGE_CAPABILITY_NAME = "message"

_DESCRIPTION = (
    "【message】向当前聊天发送一条消息。\n"
    '  {"op": "message", "text": "要说的话"}\n'
    "  - text 是完整的一条消息，保持你的人设和说话风格。\n"
    "  - 通常一条就够；确实需要分开说时才输出多条。\n"
    "  - 结果为「已送审」时，这条消息在管理员批准前不会到达对方。"
)

# on 模式直接发送遇到连接类错误时，重发前的等待秒数
_SEND_RETRY_DELAY_SECONDS = 2

# Telegram 明确拒绝了请求：内容确定没有送出
_REJECTED_ERRORS = (BadRequest, Forbidden)
# 不重发的错误：被拒绝的重发也不会成功，超时的可能已经送达
_NO_RESEND_ERRORS = (BadRequest, Forbidden, TimedOut)

# 送审后加在触发消息上的 reaction
_REVIEW_REACTION = "👀"

_COLLECTED_NOTE = "已交给管理员审核"




# ============================================================================
# 能力构造
# ============================================================================

def makeMessageCapability(
    deliver: Callable[[str, Operation, TaskInfo], Awaitable[OperationResult]],
) -> Capability:
    """
    构造发言能力。

    deliver(text, operation, task) 负责实际送出并交回结果：直接发送成功记已完成，
    交给人工审核记已送审。
    """

    def _describe(task: TaskInfo) -> str:
        return _DESCRIPTION

    async def _validate(args: dict, task: TaskInfo) -> str | None:
        text = args.get("text")
        if not isinstance(text, str):
            return "text 必须是字符串"
        if not text.strip():
            return "text 不能为空；若不需要发言，则不输出 message 操作"
        return None

    async def _handle(operation: Operation, task: TaskInfo) -> OperationResult:
        return await deliver(operation.args["text"].strip(), operation, task)

    return Capability(
        name=MESSAGE_CAPABILITY_NAME,
        describe=_describe,
        validate=_validate,
        handle=_handle,
    )


def makeSendingMessageCapability(
    request: TaskRequest,
    *,
    bot,
    botData: dict,
    autoMode: str,
    opsList: list,
) -> Capability:
    """发言按 autoMode 直接发送或送审的能力；参数含义见 deliverMessage。"""

    async def _deliver(text, operation, task) -> OperationResult:
        return await deliverMessage(
            text, request=request, bot=bot, botData=botData, autoMode=autoMode, opsList=opsList,
        )

    return makeMessageCapability(_deliver)


def makeCollectingMessageCapability(texts: list[str]) -> Capability:
    """发言只追加到 texts、不送出的能力。"""

    async def _collect(text, operation, task) -> OperationResult:
        texts.append(text)
        return OperationResult(status=OperationStatus.SUBMITTED, modelText=_COLLECTED_NOTE)

    return makeMessageCapability(_collect)




# ============================================================================
# 发送的相关操作
# ============================================================================

async def deliverMessage(
    text: str,
    *,
    request: TaskRequest,
    bot,
    botData: dict,
    autoMode: str,
    opsList: list,
) -> OperationResult:
    """
    按 autoMode 直接发送或送审一条发言，交回实际结果。

    参数:
        request: 触发这次任务的请求；回复对象、审核卡上的原文和重试输入都取自它
        bot: Telegram Bot
        botData: 存放 Telegram 审核项的 bot_data
        autoMode: "on" / "off" / "console"
        opsList: 有 LLM 权限的 ops；三种模式都要求非空，入口在调用模型前已经检查过

    Telegram 明确拒绝（BadRequest / Forbidden）记失败；超时记结果不确定；其余网络错误
    在 on 模式下等待后重发一次，审核模式记结果不确定。其他异常不在这里处理，
    由操作循环记为结果不确定。
    """
    if not opsList:
        return OperationResult(
            status=OperationStatus.FAILED,
            modelText="没有负责审核的管理员，这条消息没有送出",
        )

    origin = request.origin
    try:
        if autoMode == "on":
            await _sendDirectly(text, request=request, bot=bot)
        elif autoMode == "off":
            await _sendReviewCards(
                text, request=request, bot=bot, botData=botData, opsList=opsList,
            )
        else:
            await _queueForConsole(text, request=request, bot=bot, opsID=opsList[0])

    except (NetworkError, Forbidden) as e:
        await logAction("System", f"LLM 分发回复出错：{origin.chatID}", f"{type(e).__name__}: {e}", LogLevel.WARNING, LogChildType.WITH_ONE_CHILD)

        # 只有 on 模式的连接类错误才重发一次：
        #   - 审核卡不重发，可能已经有 ops 收到
        #   - 超时的请求可能已经到达 Telegram，重发会产生重复消息
        #   - 被 Telegram 拒绝的请求重发也不会成功
        if autoMode != "on" or isinstance(e, _NO_RESEND_ERRORS):
            return _sendFailureResult(e, autoMode)

        await asyncio.sleep(_SEND_RETRY_DELAY_SECONDS)
        try:
            await sendLLMReply(
                bot=bot,
                chatID=origin.chatID,
                reply=text,
                replyToMessageID=origin.messageID,
                maxLength=TG_MESSAGE_MAX_LEN,
            )
            await logAction("System", f"LLM 分发重试成功：{origin.chatID}", "", LogLevel.INFO, LogChildType.WITH_ONE_CHILD)
        except (NetworkError, Forbidden) as e2:
            await logAction("System", f"LLM 分发重试仍失败：{origin.chatID}", f"{type(e2).__name__}: {e2}", LogLevel.ERROR, LogChildType.WITH_ONE_CHILD)
            return _sendFailureResult(e2, autoMode)

    if autoMode == "on":
        return OperationResult(status=OperationStatus.DONE, modelText="已发送")
    return OperationResult(
        status=OperationStatus.SUBMITTED,
        modelText="已交给管理员审核，批准前不会到达对方",
    )


def _sendFailureResult(error: TelegramError, autoMode: str) -> OperationResult:
    """把送出一条发言时的 Telegram 错误换成交还模型的结果：明确被拒记失败，其余记结果不确定。"""
    if isinstance(error, _REJECTED_ERRORS):
        return OperationResult(
            status=OperationStatus.FAILED,
            modelText=f"Telegram 拒绝了这次请求，内容没有送出：{error}",
        )

    target = "对方" if autoMode == "on" else "管理员"
    return OperationResult(
        status=OperationStatus.UNCERTAIN,
        modelText=f"送出时网络出错，无法确认是否已经送达{target}",
    )


async def _sendDirectly(text: str, *, request: TaskRequest, bot) -> None:
    """on：直接发到触发消息所在的聊天。"""
    origin = request.origin
    await sendLLMReply(
        bot=bot,
        chatID=origin.chatID,
        reply=text,
        replyToMessageID=origin.messageID,
        maxLength=TG_MESSAGE_MAX_LEN,
    )
    await logAction("System", f"LLM 生成内容直接发送至 @{origin.sender}（{origin.chatID}）", f"原文：{request.displayOriginalMsg}", LogLevel.INFO, LogChildType.WITH_CHILD)
    await logAction("System", "", f"生成的消息：{text}", LogLevel.INFO, LogChildType.LAST_CHILD)


async def _sendReviewCards(text: str, *, request: TaskRequest, bot, botData: dict, opsList: list) -> None:
    """
    off：给每位 ops 发一张审核卡。

    某位 ops 送不到时记日志并继续发给其余 ops；只要有一位拿到了可操作的审核卡，
    这条发言就算已送审。一位都没送到才抛出最后一个异常。
    """
    origin = request.origin
    delivered = 0
    lastError = None
    for opsID in opsList:
        try:
            await _sendReviewCard(text, request=request, bot=bot, botData=botData, opsID=int(opsID))
            delivered += 1
        except Exception as e:
            lastError = e
            await logSystemEvent(
                "LLM 审核消息发送失败",
                f"ops={opsID}, chat={origin.chatID} | {type(e).__name__}: {e}",
                LogLevel.WARNING,
            )
    if delivered == 0:
        raise lastError

    await _markUnderReview(bot, request)
    await logAction("System", f"LLM 生成内容待审核：@{origin.sender}（{origin.chatID}）", f"原文：{request.displayOriginalMsg}", LogLevel.INFO, LogChildType.WITH_CHILD)
    await logAction("System", "", f"生成的消息：{text}", LogLevel.INFO, LogChildType.LAST_CHILD)


async def _sendReviewCard(text: str, *, request: TaskRequest, bot, botData: dict, opsID: int) -> None:
    """给一位 ops 发审核卡（带 inline keyboard），并把审核项存进 bot_data。"""
    origin = request.origin
    originalMsg = request.displayOriginalMsg
    displayBlocks = request.displayBlocks

    # 一次发送带 keyboard（callback_data 不含 msgID，无需先发再改）
    cardText, markup = renderReviewCard(originalMsg, text, origin.chatID, displayBlocks=displayBlocks)
    sent = await bot.send_message(chat_id=opsID, text=cardText, reply_markup=markup, parse_mode="HTML")

    putReplyReview(
        botData,
        chatID=origin.chatID,
        reviewMsgID=sent.message_id,
        reply=text,
        originalMsg=originalMsg,
        opsID=opsID,
        triggerMsgID=origin.messageID,
        userID=origin.userID,
        includeContext=request.includeContext,
        autoMode="off",
        displayBlocks=displayBlocks,
        memoryQuery=request.memoryQuery,
        request=request,
    )


async def _queueForConsole(text: str, *, request: TaskRequest, bot, opsID) -> None:
    """console：加入控制台审核队列。"""
    origin = request.origin
    addReviewItem(
        chatID=origin.chatID,
        messageID=origin.messageID,
        originalMsg=request.displayOriginalMsg,
        reply=text,
        opsID=opsID,
        userID=origin.userID,
        includeContext=request.includeContext,
        displayBlocks=request.displayBlocks,
        memoryQuery=request.memoryQuery,
        request=request,
    )
    await _markUnderReview(bot, request)
    await logAction("System", f"LLM 生成内容等待控制台审核：@{origin.sender}（{origin.chatID}）", f"原文：{request.displayOriginalMsg}", LogLevel.INFO, LogChildType.WITH_CHILD)
    await logAction("System", "", f"生成的消息：{text}", LogLevel.INFO, LogChildType.LAST_CHILD)


async def _markUnderReview(bot, request: TaskRequest) -> None:
    """给触发消息加上送审的 reaction；失败记 WARNING 后继续，不影响送审结果。"""
    origin = request.origin
    try:
        await bot.set_message_reaction(
            chat_id=origin.chatID,
            message_id=origin.messageID,
            reaction=[ReactionTypeEmoji(emoji=_REVIEW_REACTION)],
        )
    except Exception as e:
        await logSystemEvent(
            "LLM reaction 发送失败",
            f"emoji={_REVIEW_REACTION} | {e}",
            LogLevel.WARNING,
        )
