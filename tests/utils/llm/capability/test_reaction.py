"""
tests/utils/llm/capability/test_reaction.py

测试回应能力（capability/reaction.py）：给模型的说明、emoji 校验、handle，
以及 sendReaction 的送出、错误分类与聊天历史记录。

送出用例不连 Telegram：bot 是 AsyncMock，加上的 reaction 看 bot.set_message_reaction，
聊天历史看换掉的 recordBotReaction。
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.constants import ReactionEmoji
from telegram.error import BadRequest, Forbidden, NetworkError, TimedOut

from utils.core.logger import LogLevel
from utils.llm.capability.reaction import (
    REACTION_CAPABILITY_NAME,
    makeReactionCapability,
    sendReaction,
)
from utils.llm.models import MessageInput, MessageOrigin, TaskRequest
from utils.llm.operation.loop import runTask
from utils.llm.operation.types import (
    Operation,
    OperationResult,
    OperationStatus,
    TaskInfo,
    TaskStatus,
)




_EMPTY_END = '<OPERATIONS>{"operations": [], "end": true}</OPERATIONS>'

_CHAT_ID = "100"
_USER_ID = 42
_TRIGGER_MSG_ID = 77
_EMOJI = "👍"

# 只影响显示样式的变体选择符；模型输出的 emoji 常常带着它
_VARIATION_SELECTOR = chr(0xFE0F)

_DONE_RESULT = OperationResult(status=OperationStatus.DONE, modelText="已加上")
_UNCERTAIN_RESULT = OperationResult(
    status=OperationStatus.UNCERTAIN,
    modelText="加 reaction 时网络出错，无法确认是否已经加上",
)




@pytest.fixture(autouse=True)
def logs(monkeypatch):
    """日志不落盘；用例可以检查记了什么。"""
    mocks = SimpleNamespace(action=AsyncMock())
    monkeypatch.setattr("utils.llm.capability.reaction.logAction", mocks.action)
    monkeypatch.setattr("utils.llm.operation.loop.logSystemEvent", AsyncMock())
    return mocks


@pytest.fixture(autouse=True)
def recordReaction(monkeypatch):
    """聊天历史的写入换成假的。"""
    mock = AsyncMock(return_value=True)
    monkeypatch.setattr("utils.llm.capability.reaction.recordBotReaction", mock)
    return mock




def _task() -> TaskInfo:
    return TaskInfo(
        taskID="t1", chatID=_CHAT_ID, userID=_USER_ID, triggerMessageID=_TRIGGER_MSG_ID,
    )


def _request() -> TaskRequest:
    """一条触发消息组成的请求。"""
    return TaskRequest(
        messages=(MessageInput(
            origin=MessageOrigin(
                chatID=_CHAT_ID, userID=_USER_ID, messageID=_TRIGGER_MSG_ID, sender="alice",
            ),
            text="原始消息",
        ),),
    )


def _reactionOperation(emoji=_EMOJI) -> Operation:
    return Operation(index=1, name=REACTION_CAPABILITY_NAME, args={"emoji": emoji})


def _batch(*operations, end=True) -> str:
    payload = json.dumps({"operations": list(operations), "end": end}, ensure_ascii=False)
    return f"<OPERATIONS>{payload}</OPERATIONS>"


def _scriptedModel(*outputs):
    """按顺序返回预设输出的模型回调，连同它每次收到的 appendix 列表一起返回。"""
    appendices = []

    async def _callModel(*, instructions, appendix):
        appendices.append(appendix)
        return outputs[len(appendices) - 1]

    return _callModel, appendices


def _assertReacted(bot, emoji=_EMOJI) -> None:
    """触发消息上恰好加了一次 emoji。"""
    bot.set_message_reaction.assert_awaited_once()
    kwargs = bot.set_message_reaction.await_args.kwargs
    assert kwargs["chat_id"] == _CHAT_ID
    assert kwargs["message_id"] == _TRIGGER_MSG_ID
    assert [reaction.emoji for reaction in kwargs["reaction"]] == [emoji]




# ===========================================================================
# 回应能力：说明与校验
# ===========================================================================

def test_reactionCapabilityDescribesFormatAndAllowedEmojis():
    capability = makeReactionCapability(_request(), bot=AsyncMock())

    description = capability.describe(_task())

    assert capability.name == REACTION_CAPABILITY_NAME == "reaction"
    exampleLine = next(line.strip() for line in description.splitlines() if line.strip().startswith("{"))
    example = json.loads(exampleLine)
    assert example["op"] == "reaction"
    assert example["emoji"] in {emoji.value for emoji in ReactionEmoji}
    for emoji in ReactionEmoji:
        assert emoji.value in description


@pytest.mark.parametrize("args, reason", [
    ({}, "emoji 必须是字符串"),
    ({"emoji": None}, "emoji 必须是字符串"),
    ({"emoji": 1}, "emoji 必须是字符串"),
    ({"emoji": ""}, "emoji 不在可用列表里"),
    ({"emoji": "好"}, "emoji 不在可用列表里"),
    ({"emoji": "👍👍"}, "emoji 不在可用列表里"),
])
async def test_invalidEmojiIsRejected(args, reason):
    capability = makeReactionCapability(_request(), bot=AsyncMock())

    assert reason in await capability.validate(args, _task())


@pytest.mark.parametrize("emoji", [
    "👍",
    " 👍 ",
    "❤",
    "❤" + _VARIATION_SELECTOR,
])
async def test_allowedEmojiPassesValidation(emoji):
    capability = makeReactionCapability(_request(), bot=AsyncMock())

    assert await capability.validate({"emoji": emoji}, _task()) is None


async def test_everyTelegramReactionEmojiPassesValidation():
    capability = makeReactionCapability(_request(), bot=AsyncMock())

    for emoji in ReactionEmoji:
        assert await capability.validate({"emoji": emoji.value}, _task()) is None




# ===========================================================================
# 回应能力：handle
# ===========================================================================

async def test_handleReactsToTriggerMessage(recordReaction):
    bot = AsyncMock()
    capability = makeReactionCapability(_request(), bot=bot)

    result = await capability.handle(_reactionOperation(), _task())

    assert result == _DONE_RESULT
    _assertReacted(bot)
    recordReaction.assert_awaited_once_with(_CHAT_ID, _EMOJI)


async def test_handleSendsTheEmojiAsTelegramListsIt():
    """模型带上变体选择符或空白时，发给 Telegram 的仍是可用列表里的写法。"""
    bot = AsyncMock()
    capability = makeReactionCapability(_request(), bot=bot)

    await capability.handle(_reactionOperation(" ❤" + _VARIATION_SELECTOR), _task())

    _assertReacted(bot, ReactionEmoji.RED_HEART.value)


async def test_handleIgnoresMessageIDFromModel():
    """目标固定是触发消息：模型在参数里另给的 ID 不起作用。"""
    bot = AsyncMock()
    capability = makeReactionCapability(_request(), bot=bot)
    operation = Operation(
        index=1, name=REACTION_CAPABILITY_NAME,
        args={"emoji": _EMOJI, "message_id": 999, "chat_id": "-1"},
    )

    await capability.handle(operation, _task())

    _assertReacted(bot)


async def test_reactionOperationRunsThroughLoop():
    bot = AsyncMock()
    callModel, _ = _scriptedModel(_batch({"op": "reaction", "emoji": _EMOJI}))

    outcome = await runTask(
        task=_task(), capabilities=[makeReactionCapability(_request(), bot=bot)], callModel=callModel,
    )

    assert outcome.status == TaskStatus.COMPLETED
    assert [record.result for record in outcome.records] == [_DONE_RESULT]
    _assertReacted(bot)


async def test_invalidEmojiBlocksWholeBatchInLoop():
    bot = AsyncMock()
    callModel, appendices = _scriptedModel(
        _batch({"op": "reaction", "emoji": _EMOJI}, {"op": "reaction", "emoji": "好"}),
        _EMPTY_END,
    )

    outcome = await runTask(
        task=_task(), capabilities=[makeReactionCapability(_request(), bot=bot)], callModel=callModel,
    )

    bot.set_message_reaction.assert_not_awaited()
    assert outcome.records == ()
    assert "第 2 个操作（reaction）：emoji 不在可用列表里" in appendices[1]




# ===========================================================================
# 送出：sendReaction
# ===========================================================================

class TestSendReaction:

    async def test_success_is_done_and_recorded(self, logs, recordReaction):
        bot = AsyncMock()

        result = await sendReaction(_EMOJI, request=_request(), bot=bot)

        assert result == _DONE_RESULT
        _assertReacted(bot)
        recordReaction.assert_awaited_once_with(_CHAT_ID, _EMOJI)
        logged = logs.action.await_args
        assert "加了 reaction" in logged.args[1]
        assert logged.args[3] == LogLevel.INFO

    @pytest.mark.parametrize("error", [
        BadRequest("Reaction_invalid"),
        Forbidden("bot was blocked by the user"),
    ])
    async def test_rejected_is_failed_with_reason(self, error, logs, recordReaction):
        bot = AsyncMock()
        bot.set_message_reaction.side_effect = error

        result = await sendReaction(_EMOJI, request=_request(), bot=bot)

        assert result.status == OperationStatus.FAILED
        assert result.modelText == f"Telegram 拒绝了这次请求，reaction 没有加上：{error}"
        bot.set_message_reaction.assert_awaited_once()
        recordReaction.assert_not_awaited()
        assert logs.action.await_args.args[3] == LogLevel.WARNING

    @pytest.mark.parametrize("error", [
        TimedOut(),
        NetworkError("connection reset"),
    ])
    async def test_network_error_is_uncertain_without_resend(self, error, recordReaction):
        bot = AsyncMock()
        bot.set_message_reaction.side_effect = error

        result = await sendReaction(_EMOJI, request=_request(), bot=bot)

        assert result == _UNCERTAIN_RESULT
        bot.set_message_reaction.assert_awaited_once()
        recordReaction.assert_not_awaited()

    async def test_other_error_propagates(self, recordReaction):
        bot = AsyncMock()
        bot.set_message_reaction.side_effect = RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            await sendReaction(_EMOJI, request=_request(), bot=bot)

        recordReaction.assert_not_awaited()

    async def test_other_error_becomes_uncertain_in_loop(self):
        bot = AsyncMock()
        bot.set_message_reaction.side_effect = RuntimeError("boom")
        callModel, appendices = _scriptedModel(
            _batch({"op": "reaction", "emoji": _EMOJI}),
            _EMPTY_END,
        )

        outcome = await runTask(
            task=_task(), capabilities=[makeReactionCapability(_request(), bot=bot)], callModel=callModel,
        )

        assert outcome.records[0].result.status == OperationStatus.UNCERTAIN
        assert "#1 reaction → 结果不确定" in appendices[1]
