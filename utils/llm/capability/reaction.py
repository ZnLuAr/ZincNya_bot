"""
utils/llm/capability/reaction.py

回应能力（reaction）：模型给触发本次任务的那条消息加一个 emoji reaction。

说明、校验与送出都在本文件。和发言不同，reaction 不经人工审核，三种 autoMode 下
都直接生效；目标固定是触发消息，模型不能指定别的消息。只有「真正送出」一种构造
方式，审核重试不提供这个能力。
"""

from telegram import ReactionTypeEmoji
from telegram.constants import ReactionEmoji
from telegram.error import BadRequest, Forbidden, NetworkError

from utils.core.logger import logAction, LogLevel, LogChildType
from utils.chatHistory import recordBotReaction
from utils.llm.models import TaskRequest
from utils.llm.operation.types import (
    Capability, Operation, OperationResult, OperationStatus, TaskInfo,
)


REACTION_CAPABILITY_NAME = "reaction"

# U+FE0F 只影响 emoji 的显示样式，同一个 emoji 带不带它两种写法都常见；
# 比对前两边都去掉
_VARIATION_SELECTOR = chr(0xFE0F)

# Telegram 允许 bot 使用的 reaction，随 python-telegram-bot 的版本走；
# 键是去掉 U+FE0F 的写法，值是发给 Telegram 的写法
_ALLOWED_EMOJIS = {
    emoji.value.replace(_VARIATION_SELECTOR, ""): emoji.value
    for emoji in ReactionEmoji
}

_DESCRIPTION = (
    "【reaction】给对方刚发来的这条消息加一个 emoji 回应。\n"
    '  {"op": "reaction", "emoji": "👀"}\n'
    "  - 只能回应触发本次任务的那条消息；同一条消息上只保留你最后一次的回应。\n"
    "  - 适合表个态就够、不需要说话的时候；可以只回应不发言，也可以两样都做。\n"
    f"  - 可用的 emoji：{' '.join(_ALLOWED_EMOJIS.values())}"
)

# Telegram 明确拒绝了请求：reaction 确定没有加上
_REJECTED_ERRORS = (BadRequest, Forbidden)




# ============================================================================
# 能力构造
# ============================================================================

def makeReactionCapability(request: TaskRequest, *, bot) -> Capability:
    """构造回应能力：给 request 的触发消息加 reaction；参数含义见 sendReaction。"""

    def _describe(task: TaskInfo) -> str:
        return _DESCRIPTION

    async def _validate(args: dict, task: TaskInfo) -> str | None:
        emoji = args.get("emoji")
        if not isinstance(emoji, str):
            return "emoji 必须是字符串"
        if _toAllowedEmoji(emoji) is None:
            return "emoji 不在可用列表里，请从 reaction 说明列出的 emoji 中选一个"
        return None

    async def _handle(operation: Operation, task: TaskInfo) -> OperationResult:
        return await sendReaction(
            _toAllowedEmoji(operation.args["emoji"]), request=request, bot=bot,
        )

    return Capability(
        name=REACTION_CAPABILITY_NAME,
        describe=_describe,
        validate=_validate,
        handle=_handle,
    )


def _toAllowedEmoji(emoji: str) -> str | None:
    """把模型给的 emoji 换成 Telegram 可用列表里的写法；不在列表里返回 None。"""
    return _ALLOWED_EMOJIS.get(emoji.strip().replace(_VARIATION_SELECTOR, ""))




# ============================================================================
# 发送的相关操作
# ============================================================================

async def sendReaction(emoji: str, *, request: TaskRequest, bot) -> OperationResult:
    """
    给触发消息加上 reaction，交回实际结果；加上之后写入聊天历史。

    参数:
        emoji: Telegram 可用列表里的 emoji
        request: 触发这次任务的请求；reaction 加在它的触发消息上
        bot: Telegram Bot

    Telegram 明确拒绝（BadRequest / Forbidden，例如这个聊天不允许这个 emoji）记失败；
    其余网络错误记结果不确定，不重发。其他异常不在这里处理，由操作循环记为结果不确定。
    """
    origin = request.origin
    try:
        await bot.set_message_reaction(
            chat_id=origin.chatID,
            message_id=origin.messageID,
            reaction=[ReactionTypeEmoji(emoji=emoji)],
        )
    except (NetworkError, Forbidden) as e:
        await logAction("System", f"LLM 加 reaction 出错：{origin.chatID}", f"emoji={emoji} | {type(e).__name__}: {e}", LogLevel.WARNING, LogChildType.WITH_ONE_CHILD)

        if isinstance(e, _REJECTED_ERRORS):
            return OperationResult(
                status=OperationStatus.FAILED,
                modelText=f"Telegram 拒绝了这次请求，reaction 没有加上：{e}",
            )
        return OperationResult(
            status=OperationStatus.UNCERTAIN,
            modelText="加 reaction 时网络出错，无法确认是否已经加上",
        )

    await recordBotReaction(str(origin.chatID), emoji)
    await logAction("System", f"LLM 给 @{origin.sender}（{origin.chatID}）的消息加了 reaction", f"{emoji} | 原文：{request.displayOriginalMsg}", LogLevel.INFO, LogChildType.WITH_ONE_CHILD)
    return OperationResult(status=OperationStatus.DONE, modelText="已加上")
