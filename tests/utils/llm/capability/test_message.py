"""
tests/utils/llm/capability/test_message.py

测试发言能力（capability/message.py）：给模型的说明、参数校验、handle，
「只收集」与「真正送出」两种构造方式，以及 deliverMessage 的三态送出、
错误分类与重发。

送出用例不连 Telegram：bot 是 AsyncMock，审核卡看 bot.send_message，审核项看
botData，送审标记看 bot.set_message_reaction；控制台队列换成每个用例独立的空队列。
"""

import json
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from telegram import InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, NetworkError, TimedOut

from config import TG_MESSAGE_MAX_LEN

from utils.core.logger import LogLevel
from utils.llm.capability.message import (
    MESSAGE_CAPABILITY_NAME,
    _SEND_RETRY_DELAY_SECONDS,
    deliverMessage,
    makeCollectingMessageCapability,
    makeMessageCapability,
    makeSendingMessageCapability,
)
from utils.llm.memory.types import MemoryQuery, MemoryTurn
from utils.llm.models import MessageInput, MessageOrigin, TaskRequest
from utils.llm.operation.loop import runTask
from utils.llm.operation.types import (
    Operation,
    OperationResult,
    OperationStatus,
    TaskInfo,
    TaskStatus,
)
from utils.llm.review.items import editIndexKey, replyReviewKey




_EMPTY_END = '<OPERATIONS>{"operations": [], "end": true}</OPERATIONS>'

# 送出用例共用的触发消息身份与发言
_CHAT_ID = "100"
_USER_ID = 42
_TRIGGER_MSG_ID = 77
_REPLY_TEXT = "你好喵"

_NO_OPS_RESULT = OperationResult(
    status=OperationStatus.FAILED,
    modelText="没有负责审核的管理员，这条消息没有送出",
)
_DONE_RESULT = OperationResult(status=OperationStatus.DONE, modelText="已发送")
_SUBMITTED_RESULT = OperationResult(
    status=OperationStatus.SUBMITTED,
    modelText="已交给管理员审核，批准前不会到达对方",
)
_UNCERTAIN_TEXT = "送出时网络出错，无法确认是否已经送达{target}"

# 审核模式各自负责送审的内部函数；整段换掉用来模拟送审途中抛出的 Telegram 错误
_REVIEW_SENDERS = {
    "off": "utils.llm.capability.message._sendReviewCards",
    "console": "utils.llm.capability.message._queueForConsole",
}




@pytest.fixture(autouse=True)
def logs(monkeypatch):
    """日志不落盘、直接发送不写聊天历史；用例可以检查记了什么。"""
    mocks = SimpleNamespace(action=AsyncMock(), system=AsyncMock())
    monkeypatch.setattr("utils.llm.capability.message.logAction", mocks.action)
    monkeypatch.setattr("utils.llm.capability.message.logSystemEvent", mocks.system)
    monkeypatch.setattr("utils.llm.operation.loop.logSystemEvent", AsyncMock())
    monkeypatch.setattr("utils.telegramHelpers.recordBotMessage", AsyncMock())
    return mocks


@pytest.fixture(autouse=True)
def reviewQueue(monkeypatch):
    """控制台审核队列换成空队列，用例之间互不影响，也不碰模块级的那一条。"""
    queue = asyncio.Queue()
    monkeypatch.setattr("utils.llm.state._llmReviewQueue", queue)
    return queue


@pytest.fixture
def sendReply(monkeypatch):
    """把直接发送用的 sendLLMReply 换成假的。"""
    mock = AsyncMock()
    monkeypatch.setattr("utils.llm.capability.message.sendLLMReply", mock)
    return mock


@pytest.fixture
def retrySleep(monkeypatch):
    """重发前的等待换成假的，用例不真的睡。"""
    mock = AsyncMock()
    monkeypatch.setattr("utils.llm.capability.message.asyncio.sleep", mock)
    return mock




def _task(**overrides) -> TaskInfo:
    fields = {"taskID": "t1", "chatID": _CHAT_ID, "userID": _USER_ID, "includeContext": True}
    fields.update(overrides)
    return TaskInfo(**fields)


def _submitted(modelText="已交给管理员审核") -> OperationResult:
    return OperationResult(status=OperationStatus.SUBMITTED, modelText=modelText)


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


def _messageOperation(text=_REPLY_TEXT, **extraArgs) -> Operation:
    return Operation(index=1, name=MESSAGE_CAPABILITY_NAME, args={"text": text, **extraArgs})


def _request(*, includeContext=False, **messageFields) -> TaskRequest:
    """一条触发消息组成的请求；messageFields 覆盖 MessageInput 的字段。"""
    fields = {"text": "原始消息", "includeContext": includeContext}
    fields.update(messageFields)
    return TaskRequest(
        messages=(MessageInput(
            origin=MessageOrigin(
                chatID=_CHAT_ID, userID=_USER_ID, messageID=_TRIGGER_MSG_ID, sender="alice",
            ),
            **fields,
        ),),
        includeContext=includeContext,
    )


def _bot(*sends) -> AsyncMock:
    """
    Telegram Bot 替身。

    sends 依次是每次 send_message 的结果：整数表示发送成功、返回带这个 message_id
    的消息，异常表示这一次发送抛出它。
    """
    bot = AsyncMock()
    if sends:
        bot.send_message.side_effect = [
            SimpleNamespace(message_id=send) if isinstance(send, int) else send
            for send in sends
        ]
    return bot


def _reviewKeys(botData: dict) -> list[str]:
    return [key for key in botData if str(key).startswith("llm_review_")]


def _assertMarkedUnderReview(bot) -> None:
    """触发消息上恰好加了一次送审的 reaction。"""
    bot.set_message_reaction.assert_awaited_once()
    kwargs = bot.set_message_reaction.await_args.kwargs
    assert kwargs["chat_id"] == _CHAT_ID
    assert kwargs["message_id"] == _TRIGGER_MSG_ID
    assert [reaction.emoji for reaction in kwargs["reaction"]] == ["👀"]




# ===========================================================================
# 发言能力：说明与校验
# ===========================================================================

@pytest.mark.parametrize("includeContext", [True, False])
def test_messageCapabilityIsAlwaysOffered(includeContext):
    capability = makeMessageCapability(AsyncMock())

    description = capability.describe(_task(includeContext=includeContext))

    assert capability.name == MESSAGE_CAPABILITY_NAME == "message"
    assert '{"op": "message", "text": "要说的话"}' in description


@pytest.mark.parametrize("args", [
    {},
    {"content": "写错了字段名"},
    {"text": None},
    {"text": 123},
    {"text": True},
    {"text": ["第一条", "第二条"]},
    {"text": {"zh": "你好"}},
])
async def test_messageValidateRejectsMissingOrNonStringText(args):
    capability = makeMessageCapability(AsyncMock())

    assert await capability.validate(args, _task()) == "text 必须是字符串"


@pytest.mark.parametrize("text", ["", "   ", "\n\t"])
async def test_messageValidateRejectsBlankText(text):
    capability = makeMessageCapability(AsyncMock())

    reason = await capability.validate({"text": text}, _task())

    assert reason.startswith("text 不能为空")


async def test_messageValidateAcceptsText():
    capability = makeMessageCapability(AsyncMock())

    assert await capability.validate({"text": "你好喵"}, _task()) is None


async def test_messageValidateIgnoresExtraFields():
    """多余字段不算错，也不会被用到（见下面 handle 的用例）。"""
    capability = makeMessageCapability(AsyncMock())
    args = {"text": "你好喵", "chat_id": "999", "parse_mode": "HTML"}

    assert await capability.validate(args, _task()) is None


async def test_messageValidateHasNoSideEffect():
    deliver = AsyncMock()
    capability = makeMessageCapability(deliver)

    await capability.validate({"text": "你好喵"}, _task())

    deliver.assert_not_awaited()




# ===========================================================================
# 发言能力：处理
# ===========================================================================

async def test_messageHandleDeliversTextAndReturnsResult():
    delivered = _submitted()
    deliver = AsyncMock(return_value=delivered)
    capability = makeMessageCapability(deliver)
    operation = Operation(index=2, name=MESSAGE_CAPABILITY_NAME, args={"text": "你好喵"})
    task = _task()

    result = await capability.handle(operation, task)

    assert result is delivered
    deliver.assert_awaited_once_with("你好喵", operation, task)


async def test_messageHandleStripsText():
    deliver = AsyncMock(return_value=_submitted())
    capability = makeMessageCapability(deliver)
    operation = Operation(index=1, name=MESSAGE_CAPABILITY_NAME, args={"text": "\n  你好喵  \n"})

    await capability.handle(operation, _task())

    assert deliver.await_args.args[0] == "你好喵"


async def test_messageHandleUsesOnlyTextFromArgs():
    deliver = AsyncMock(return_value=_submitted())
    capability = makeMessageCapability(deliver)
    args = {"text": "你好喵", "chat_id": "999"}
    operation = Operation(index=1, name=MESSAGE_CAPABILITY_NAME, args=args)
    task = _task()

    await capability.handle(operation, task)

    # 送到哪里由 task 决定；模型在参数里写的 chat_id 只是原样留在 operation 里
    text, deliveredOperation, deliveredTask = deliver.await_args.args
    assert text == "你好喵"
    assert deliveredOperation is operation
    assert deliveredTask is task
    assert deliveredTask.chatID == "100"


async def test_messageOperationRunsThroughLoop():
    deliver = AsyncMock(return_value=OperationResult(status=OperationStatus.DONE))
    callModel, _ = _scriptedModel(_batch({"op": "message", "text": "你好喵"}))
    task = _task()

    outcome = await runTask(
        task=task, capabilities=[makeMessageCapability(deliver)], callModel=callModel,
    )

    assert outcome.status == TaskStatus.COMPLETED
    deliver.assert_awaited_once()
    text, operation, deliveredTask = deliver.await_args.args
    assert text == "你好喵"
    assert operation == Operation(index=1, name="message", args={"text": "你好喵"})
    assert deliveredTask is task


async def test_messageDeliverErrorBecomesUncertainInLoop():
    deliver = AsyncMock(side_effect=RuntimeError("发送超时"))
    callModel, appendices = _scriptedModel(
        _batch({"op": "message", "text": "你好喵"}, end=True),
        _EMPTY_END,
    )

    outcome = await runTask(
        task=_task(), capabilities=[makeMessageCapability(deliver)], callModel=callModel,
    )

    # 送出时抛异常：不知道对方有没有收到，交还模型时标成结果不确定
    assert outcome.records[0].result.status == OperationStatus.UNCERTAIN
    assert "#1 message → 结果不确定" in appendices[1]


async def test_invalidMessageBlocksWholeBatchInLoop():
    deliver = AsyncMock(return_value=OperationResult(status=OperationStatus.DONE))
    callModel, appendices = _scriptedModel(
        _batch({"op": "message", "text": "第一条"}, {"op": "message", "text": "  "}),
        _EMPTY_END,
    )

    outcome = await runTask(
        task=_task(), capabilities=[makeMessageCapability(deliver)], callModel=callModel,
    )

    deliver.assert_not_awaited()
    assert outcome.records == ()
    assert "第 2 个操作（message）：text 不能为空" in appendices[1]




# ===========================================================================
# 只收集的发言能力
# ===========================================================================

async def test_collectingMessageCapabilityAppendsStrippedText():
    texts = []
    capability = makeCollectingMessageCapability(texts)

    result = await capability.handle(_messageOperation("\n  你好喵  \n"), _task())

    assert texts == ["你好喵"]
    assert result == OperationResult(status=OperationStatus.SUBMITTED, modelText="已交给管理员审核")


async def test_collectingMessageCapabilityKeepsOrderInLoop(reviewQueue):
    texts = ["已有的一条"]
    callModel, _ = _scriptedModel(
        _batch({"op": "message", "text": "第一条"}, {"op": "message", "text": "第二条"}),
    )

    outcome = await runTask(
        task=_task(), capabilities=[makeCollectingMessageCapability(texts)], callModel=callModel,
    )

    # 只追加到传入的列表，不送出、不进审核队列
    assert texts == ["已有的一条", "第一条", "第二条"]
    assert [record.result.status for record in outcome.records] == [OperationStatus.SUBMITTED] * 2
    assert reviewQueue.empty()




# ===========================================================================
# 真正送出的发言能力
# ===========================================================================

async def test_sendingMessageCapabilityHandsRequestToDeliverMessage(monkeypatch):
    delivered = OperationResult(status=OperationStatus.DONE, modelText="已发送")
    deliver = AsyncMock(return_value=delivered)
    monkeypatch.setattr("utils.llm.capability.message.deliverMessage", deliver)
    request = _request()
    bot = AsyncMock()
    botData = {}
    opsList = ["111", "222"]
    capability = makeSendingMessageCapability(
        request, bot=bot, botData=botData, autoMode="off", opsList=opsList,
    )

    result = await capability.handle(_messageOperation("  你好喵  ", chat_id="999"), _task())

    # 送到哪里、给谁审核都取自构造时的请求与设置；模型参数里只用 text
    assert result is delivered
    deliver.assert_awaited_once_with(
        "你好喵", request=request, bot=bot, botData=botData, autoMode="off", opsList=opsList,
    )
    assert deliver.await_args.kwargs["request"] is request
    assert deliver.await_args.kwargs["botData"] is botData


async def test_sendingMessageCapabilityReviewCardUsesRequestOriginAndDisplay():
    """走真实的 deliverMessage：审核卡和审核项里的来源、原文、展示块都来自请求。"""
    request = _request(text="今天喝什么", replyText="昨天喝了乌龙茶", replySender="bob")
    bot = _bot(501)
    botData = {}
    capability = makeSendingMessageCapability(
        request, bot=bot, botData=botData, autoMode="off", opsList=["111"],
    )

    result = await capability.handle(_messageOperation(), _task())

    assert result.status == OperationStatus.SUBMITTED
    cardText = bot.send_message.await_args.kwargs["text"]
    assert "昨天喝了乌龙茶" in cardText
    assert "今天喝什么" in cardText
    assert f"<pre>{_REPLY_TEXT}</pre>" in cardText

    entry = botData[replyReviewKey(_CHAT_ID, 501)]
    assert entry["chatID"] == _CHAT_ID
    assert entry["userID"] == _USER_ID
    assert entry["triggerMsgID"] == _TRIGGER_MSG_ID
    assert entry["originalMsg"] == request.displayOriginalMsg
    assert entry["displayBlocks"] == request.displayBlocks
    assert entry["displayBlocks"].pairs == [
        {"replyLine": "<@bob> 昨天喝了乌龙茶", "currentText": "今天喝什么"},
    ]
    assert entry["request"] is request




# ===========================================================================
# deliverMessage：三态送出、错误分类与重发
# ===========================================================================

class TestDeliverMessage:

    @pytest.fixture(autouse=True)
    def _noRealSleep(self, retrySleep):
        self.sleep = retrySleep

    # --- on：直接发送 ---

    async def test_on_mode_sends_directly_and_is_done(self, sendReply, reviewQueue):
        bot = AsyncMock()
        botData = {}

        result = await deliverMessage(
            _REPLY_TEXT, request=_request(), bot=bot, botData=botData, autoMode="on", opsList=["111"],
        )

        assert result == _DONE_RESULT
        sendReply.assert_awaited_once_with(
            bot=bot,
            chatID=_CHAT_ID,
            reply=_REPLY_TEXT,
            replyToMessageID=_TRIGGER_MSG_ID,
            maxLength=TG_MESSAGE_MAX_LEN,
        )
        # on 模式不送审：没有审核卡、审核项和 reaction
        bot.send_message.assert_not_awaited()
        bot.set_message_reaction.assert_not_awaited()
        assert botData == {}
        assert reviewQueue.empty()
        self.sleep.assert_not_awaited()

    @pytest.mark.parametrize("error", [
        BadRequest("Message to be replied not found"),
        Forbidden("bot was blocked by the user"),
    ])
    async def test_on_mode_rejected_send_is_failed_without_retry(self, error):
        """Telegram 明确拒绝的发送确定没有送出：记失败，不等待也不重发，原因交给模型。"""
        bot = _bot(error)

        result = await deliverMessage(
            _REPLY_TEXT, request=_request(), bot=bot, botData={}, autoMode="on", opsList=["111"],
        )

        assert result.status == OperationStatus.FAILED
        assert result.modelText == f"Telegram 拒绝了这次请求，内容没有送出：{error}"
        bot.send_message.assert_awaited_once()
        assert bot.send_message.await_args.kwargs["chat_id"] == _CHAT_ID
        assert bot.send_message.await_args.kwargs["reply_to_message_id"] == _TRIGGER_MSG_ID
        self.sleep.assert_not_awaited()

    async def test_on_mode_timeout_is_uncertain_without_retry(self, sendReply):
        """超时的请求可能已经到达 Telegram，重发会产生重复消息。"""
        sendReply.side_effect = TimedOut()

        result = await deliverMessage(
            _REPLY_TEXT, request=_request(), bot=AsyncMock(), botData={}, autoMode="on", opsList=["111"],
        )

        assert result.status == OperationStatus.UNCERTAIN
        assert result.modelText == _UNCERTAIN_TEXT.format(target="对方")
        sendReply.assert_awaited_once()
        self.sleep.assert_not_awaited()

    async def test_on_mode_network_error_retries_once_then_done(self, sendReply):
        sendReply.side_effect = [NetworkError("断网"), None]
        bot = AsyncMock()

        result = await deliverMessage(
            _REPLY_TEXT, request=_request(), bot=bot, botData={}, autoMode="on", opsList=["111"],
        )

        assert result == _DONE_RESULT
        self.sleep.assert_awaited_once_with(_SEND_RETRY_DELAY_SECONDS)
        assert sendReply.await_count == 2
        for sent in sendReply.await_args_list:
            assert sent.kwargs == {
                "bot": bot,
                "chatID": _CHAT_ID,
                "reply": _REPLY_TEXT,
                "replyToMessageID": _TRIGGER_MSG_ID,
                "maxLength": TG_MESSAGE_MAX_LEN,
            }

    @pytest.mark.parametrize("secondError", [NetworkError("仍然断网"), TimedOut()])
    async def test_on_mode_retry_failure_is_uncertain(self, sendReply, secondError):
        sendReply.side_effect = [NetworkError("断网"), secondError]

        result = await deliverMessage(
            _REPLY_TEXT, request=_request(), bot=AsyncMock(), botData={}, autoMode="on", opsList=["111"],
        )

        # 只重发一次；重发仍失败就按第二个错误分类
        assert result.status == OperationStatus.UNCERTAIN
        assert result.modelText == _UNCERTAIN_TEXT.format(target="对方")
        assert sendReply.await_count == 2
        self.sleep.assert_awaited_once_with(_SEND_RETRY_DELAY_SECONDS)

    @pytest.mark.parametrize("secondError", [
        BadRequest("Message to be replied not found"),
        Forbidden("bot was blocked by the user"),
    ])
    async def test_on_mode_retry_rejected_is_failed(self, sendReply, secondError):
        sendReply.side_effect = [NetworkError("断网"), secondError]

        result = await deliverMessage(
            _REPLY_TEXT, request=_request(), bot=AsyncMock(), botData={}, autoMode="on", opsList=["111"],
        )

        assert result.status == OperationStatus.FAILED
        assert str(secondError) in result.modelText
        assert sendReply.await_count == 2
        self.sleep.assert_awaited_once_with(_SEND_RETRY_DELAY_SECONDS)

    async def test_on_mode_other_error_propagates(self, sendReply):
        """Telegram 错误之外的异常不在这里处理，也不重发。"""
        error = RuntimeError("boom")
        sendReply.side_effect = error

        with pytest.raises(RuntimeError) as excInfo:
            await deliverMessage(
                _REPLY_TEXT, request=_request(), bot=AsyncMock(), botData={}, autoMode="on", opsList=["111"],
            )

        assert excInfo.value is error
        sendReply.assert_awaited_once()
        self.sleep.assert_not_awaited()

    # --- off：给每位 ops 发审核卡 ---

    async def test_off_mode_sends_review_card_to_each_ops(self, sendReply, reviewQueue, logs):
        """每位 ops 按列表顺序各收到一张可操作的审核卡，bot_data 各存一份审核项，reaction 恰一次。"""
        turn = MemoryTurn(currentText="当前")
        request = _request(includeContext=True, memoryTurn=turn)
        bot = _bot(501, 502)
        botData = {}

        result = await deliverMessage(
            _REPLY_TEXT, request=request, bot=bot, botData=botData, autoMode="off", opsList=["111", "222"],
        )

        assert result == _SUBMITTED_RESULT
        assert [sent.kwargs["chat_id"] for sent in bot.send_message.await_args_list] == [111, 222]
        for sent in bot.send_message.await_args_list:
            assert f"<pre>{_REPLY_TEXT}</pre>" in sent.kwargs["text"]
            assert sent.kwargs["parse_mode"] == "HTML"
            assert isinstance(sent.kwargs["reply_markup"], InlineKeyboardMarkup)

        for reviewMsgID, opsID in ((501, 111), (502, 222)):
            key = replyReviewKey(_CHAT_ID, reviewMsgID)
            entry = botData[key]
            assert entry["reply"] == _REPLY_TEXT
            assert entry["originalMsg"] == request.displayOriginalMsg
            assert entry["chatID"] == _CHAT_ID
            assert entry["opsID"] == opsID
            assert entry["triggerMsgID"] == _TRIGGER_MSG_ID
            assert entry["userID"] == _USER_ID
            assert entry["includeContext"] is True
            assert entry["autoMode"] == "off"
            assert entry["displayBlocks"] == request.displayBlocks
            assert entry["memoryQuery"] == MemoryQuery(turns=(turn,))
            assert entry["request"] is request
            assert botData[editIndexKey(reviewMsgID)] == key

        _assertMarkedUnderReview(bot)
        # 送审不直接发给对方，也不进控制台队列
        sendReply.assert_not_awaited()
        assert reviewQueue.empty()
        logs.system.assert_not_awaited()
        self.sleep.assert_not_awaited()

    @pytest.mark.parametrize("sends, reviewMsgID, failedOps", [
        ([501, NetworkError("第二位 ops 不可达")], 501, "222"),
        ([Forbidden("第一位 ops 屏蔽了 bot"), 502], 502, "111"),
        ([RuntimeError("送不到"), 502], 502, "111"),
    ])
    async def test_off_mode_partial_delivery_is_submitted(self, logs, sends, reviewMsgID, failedOps):
        """只要有一位 ops 拿到可批准的审核卡，就是已送审；送不到的那位记 WARNING，其余照发。"""
        bot = _bot(*sends)
        botData = {}

        result = await deliverMessage(
            _REPLY_TEXT, request=_request(), bot=bot, botData=botData, autoMode="off", opsList=["111", "222"],
        )

        assert result == _SUBMITTED_RESULT
        assert [sent.kwargs["chat_id"] for sent in bot.send_message.await_args_list] == [111, 222]
        assert _reviewKeys(botData) == [replyReviewKey(_CHAT_ID, reviewMsgID)]
        _assertMarkedUnderReview(bot)

        logs.system.assert_awaited_once()
        event, detail, level = logs.system.await_args.args
        assert event == "LLM 审核消息发送失败"
        assert f"ops={failedOps}" in detail
        assert level == LogLevel.WARNING

    @pytest.mark.parametrize("sends, expected, expectedText", [
        (
            [NetworkError("断网"), NetworkError("断网")],
            OperationStatus.UNCERTAIN,
            _UNCERTAIN_TEXT.format(target="管理员"),
        ),
        (
            [Forbidden("第一位 ops 屏蔽了 bot"), TimedOut()],
            OperationStatus.UNCERTAIN,
            _UNCERTAIN_TEXT.format(target="管理员"),
        ),
        (
            [NetworkError("断网"), Forbidden("第二位 ops 屏蔽了 bot")],
            OperationStatus.FAILED,
            "Telegram 拒绝了这次请求，内容没有送出：第二位 ops 屏蔽了 bot",
        ),
    ])
    async def test_off_mode_no_ops_reached_is_classified_by_last_error(
        self, sendReply, logs, sends, expected, expectedText,
    ):
        """一位都没送到：按最后一个 Telegram 错误记结果，不加 reaction、不留审核项、不重发。"""
        bot = _bot(*sends)
        botData = {}

        result = await deliverMessage(
            _REPLY_TEXT, request=_request(), bot=bot, botData=botData, autoMode="off", opsList=["111", "222"],
        )

        assert result == OperationResult(status=expected, modelText=expectedText)
        assert bot.send_message.await_count == 2
        assert botData == {}
        bot.set_message_reaction.assert_not_awaited()
        sendReply.assert_not_awaited()
        self.sleep.assert_not_awaited()
        assert logs.system.await_count == 2
        # 只记了分发出错，没有「待审核」日志
        assert [logged.args[1] for logged in logs.action.await_args_list] == [f"LLM 分发回复出错：{_CHAT_ID}"]

    async def test_off_mode_no_ops_reached_raises_last_other_error(self, logs):
        """一位都没送到且最后一个异常不是 Telegram 错误：原样抛出，不加 reaction、不记待审核日志。"""
        lastError = RuntimeError("第二位也送不到")
        bot = _bot(RuntimeError("第一位送不到"), lastError)
        botData = {}

        with pytest.raises(RuntimeError) as excInfo:
            await deliverMessage(
                _REPLY_TEXT, request=_request(), bot=bot, botData=botData, autoMode="off", opsList=["111", "222"],
            )

        assert excInfo.value is lastError
        assert bot.send_message.await_count == 2
        assert logs.system.await_count == 2
        assert botData == {}
        bot.set_message_reaction.assert_not_awaited()
        logs.action.assert_not_awaited()

    # --- console：加入控制台审核队列 ---

    @patch("utils.llm.capability.message.addReviewItem")
    async def test_console_mode_queues_item(self, mockAdd, sendReply):
        """审核项交给第一位 ops，重试要用的请求一并入队；触发消息加 reaction。"""
        turn = MemoryTurn(currentText="当前")
        request = _request(memoryTurn=turn)
        bot = AsyncMock()
        botData = {}

        result = await deliverMessage(
            _REPLY_TEXT, request=request, bot=bot, botData=botData, autoMode="console", opsList=["9", "2"],
        )

        assert result == _SUBMITTED_RESULT
        mockAdd.assert_called_once_with(
            chatID=_CHAT_ID,
            messageID=_TRIGGER_MSG_ID,
            originalMsg=request.displayOriginalMsg,
            reply=_REPLY_TEXT,
            opsID="9",
            userID=_USER_ID,
            includeContext=False,
            displayBlocks=request.displayBlocks,
            memoryQuery=MemoryQuery(turns=(turn,)),
            request=request,
        )
        assert mockAdd.call_args.kwargs["request"] is request
        _assertMarkedUnderReview(bot)
        # 控制台审核不往 Telegram 发任何消息，也不占 bot_data
        bot.send_message.assert_not_awaited()
        sendReply.assert_not_awaited()
        assert botData == {}

    # --- 审核模式共有的行为 ---

    @pytest.mark.parametrize("autoMode", ["off", "console"])
    async def test_reaction_failure_logged_and_continues(self, logs, reviewQueue, autoMode):
        """reaction 抛异常：记 WARNING，送审结果不变。"""
        bot = _bot(501)
        bot.set_message_reaction.side_effect = RuntimeError("boom")
        botData = {}

        result = await deliverMessage(
            _REPLY_TEXT, request=_request(), bot=bot, botData=botData, autoMode=autoMode, opsList=["9"],
        )

        assert result == _SUBMITTED_RESULT
        assert logs.system.await_count == 1
        assert logs.system.await_args.args[0] == "LLM reaction 发送失败"
        assert logs.system.await_args.args[2] == LogLevel.WARNING
        if autoMode == "off":
            assert _reviewKeys(botData) == [replyReviewKey(_CHAT_ID, 501)]
        else:
            assert reviewQueue.qsize() == 1
            assert reviewQueue.get_nowait()["reply"] == _REPLY_TEXT

    @pytest.mark.parametrize("autoMode", ["off", "console"])
    @pytest.mark.parametrize("error, expected, expectedText", [
        (NetworkError("断网"), OperationStatus.UNCERTAIN, _UNCERTAIN_TEXT.format(target="管理员")),
        (TimedOut(), OperationStatus.UNCERTAIN, _UNCERTAIN_TEXT.format(target="管理员")),
        (
            Forbidden("bot was blocked by the user"),
            OperationStatus.FAILED,
            "Telegram 拒绝了这次请求，内容没有送出：bot was blocked by the user",
        ),
    ])
    async def test_review_mode_telegram_error_is_not_resent(
        self, sendReply, autoMode, error, expected, expectedText,
    ):
        """审核模式送审途中出错不重发（可能已经有 ops 收到）：网络错误记结果不确定，被拒记失败。"""
        with patch(_REVIEW_SENDERS[autoMode], new_callable=AsyncMock, side_effect=error) as sender:
            result = await deliverMessage(
                _REPLY_TEXT, request=_request(), bot=AsyncMock(), botData={}, autoMode=autoMode, opsList=["111"],
            )

        assert result == OperationResult(status=expected, modelText=expectedText)
        sender.assert_awaited_once()
        sendReply.assert_not_awaited()
        self.sleep.assert_not_awaited()

    # --- 没有 ops ---

    @pytest.mark.parametrize("autoMode", ["on", "off", "console"])
    async def test_no_ops_is_failed_and_sends_nothing(self, sendReply, reviewQueue, autoMode):
        """没有负责审核的 ops：三种模式都记失败，什么都不发（也不再向聊天发提示）。"""
        bot = AsyncMock()
        botData = {}

        result = await deliverMessage(
            _REPLY_TEXT, request=_request(), bot=bot, botData=botData, autoMode=autoMode, opsList=[],
        )

        assert result == _NO_OPS_RESULT
        bot.send_message.assert_not_awaited()
        bot.set_message_reaction.assert_not_awaited()
        sendReply.assert_not_awaited()
        assert botData == {}
        assert reviewQueue.empty()
