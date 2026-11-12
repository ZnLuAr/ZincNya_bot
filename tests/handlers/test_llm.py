"""
tests/handlers/test_llm.py

验证 LLM 入口门禁、防抖调度，以及任务管线里的无 ops 拦截、三个能力的接线、速率限制、
非正常结束提示；来信历史由全局收信入口记录。发言、记忆操作与 reaction 送出的错误分类、
重发等细节在 tests/utils/llm/capability/ 里测。

awaitability 铁律：getLLMEnabled / whetherAuthorizedUser / shouldTriggerLLM / isRateLimited
是同步 def——必须 MagicMock(return_value=...)，patch 成 AsyncMock 会返回协程对象（恒真值），
isRateLimited=False 变恒限速、写入用例全假红；handleEditReply / handleFeedbackRetry /
downloadImages / _enqueueLLMDebounce 是 async def，用 AsyncMock。

管线用例不调用模型：runRequest 换成 _TaskDouble，由它从 capabilities 里取出
对应的能力并调用 handle，再返回预设的 TaskOutcome。
"""

import asyncio
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch, call, MagicMock, AsyncMock

import pytest
from telegram.error import NetworkError

from config import LLM_DEBOUNCE_SECONDS, TG_MESSAGE_MAX_LEN

from handlers.llm import (
    _HINT_LIMIT_REACHED,
    _HINT_MODEL_FAILED,
    _HINT_MODEL_RETRYABLE,
    _HINT_NO_OPS,
    _HINT_UNFINISHED,
    _takeExtraBlocks,
    _downloadMessageImages,
    _enqueueLLMDebounce,
    _runLLMPipeline,
    handleLLMMessage,
)
from utils.core.logger import LogLevel
from utils.llm.capability import (
    MEMORY_CAPABILITY_NAME,
    MESSAGE_CAPABILITY_NAME,
    REACTION_CAPABILITY_NAME,
)
from utils.llm.memory.types import MemoryQuery, MemoryTurn
from utils.llm.models import MessageInput, MessageOrigin
from utils.llm.operation import (
    Operation,
    OperationRecord,
    OperationResult,
    OperationStatus,
    TaskInfo,
    TaskOutcome,
    TaskStatus,
)
from utils.llm.review.items import memoryReviewKey, replyReviewKey


# 同步门禁（def）——MagicMock(return_value=)
_SYNC_GATES = {
    "handlers.llm.getLLMEnabled": True,
    "handlers.llm.whetherAuthorizedUser": True,
    "handlers.llm.shouldTriggerLLM": True,
    "handlers.llm.isRateLimited": False,
}

# 异步门禁（async def）——AsyncMock
_ASYNC_GATES = {
    "handlers.llm.handleEditReply": False,
    "handlers.llm.handleFeedbackRetry": False,
    "handlers.llm._enqueueLLMDebounce": True,
}

# 管线用例共用的触发消息身份与操作
_CHAT_ID = "100"
_USER_ID = 42
_TRIGGER_MSG_ID = 9
_DEBOUNCE_KEY = "100:42"
_OPS = ("111",)
_REPLY_TEXT = "你好喵"
_MESSAGE_OP = (MESSAGE_CAPABILITY_NAME, {"text": _REPLY_TEXT})
_REACTION_OP = (REACTION_CAPABILITY_NAME, {"emoji": "👍"})
_MEMORY_OP = (MEMORY_CAPABILITY_NAME, {
    "action": "add",
    "scope_type": "global",
    "scope_id": "global",
    "content": "喜欢乌龙茶",
    "mode": "contextual",
})




# ============================================================================
# 入口门禁辅助
# ============================================================================

def _gatePatches(**overrides):
    """门禁全绿 patch 集（ExitStack 进入）。

    同步门禁可经 overrides 覆盖 bool；async 门禁覆盖需传完整 AsyncMock。
    downloadImages 返回 tuple (images, notes)，单独追加。
    """
    gates = {**_SYNC_GATES, **_ASYNC_GATES}
    gates.update(overrides)
    patches = [
        patch(k, MagicMock(return_value=v)) if k in _SYNC_GATES else patch(k, AsyncMock(return_value=v))
        for k, v in gates.items()
    ]
    patches.append(patch("handlers.llm.downloadImages", AsyncMock(return_value=([], []))))
    return patches


async def _run(mockUpdate, mockContext):
    # conftest 的 mockMessage.photo / .document 是 MagicMock（truthy），会骗过
    # extractImageRefsForPrompt 走进 _pickBestPhoto 遍历 mock 抛 TypeError——
    # 显式清空表示「纯文本消息」，走真实的图片提取（结果为空，无需 patch）
    mockUpdate.message.chat_id = mockUpdate.effective_chat.id
    mockUpdate.message.photo = ()
    mockUpdate.message.document = None
    await handleLLMMessage(mockUpdate, mockContext)




# ============================================================================
# 任务管线辅助
# ============================================================================

def _makeMessages(*, includeContext=False, memoryTurn=None, extraBlocks=()) -> tuple[MessageInput, ...]:
    return (MessageInput(
        origin=MessageOrigin(
            chatID=_CHAT_ID, userID=_USER_ID, messageID=_TRIGGER_MSG_ID, sender="alice",
        ),
        text="当前",
        includeContext=includeContext,
        memoryTurn=memoryTurn,
        extraBlocks=extraBlocks,
    ),)


def _makeRecord(status=OperationStatus.DONE) -> OperationRecord:
    return OperationRecord(
        operation=Operation(index=1, name=MESSAGE_CAPABILITY_NAME, args={"text": _REPLY_TEXT}),
        result=OperationResult(status=status),
    )


class _TaskDouble:
    """
    runRequest 的替身。

    operations 是 (能力名, args) 序列：替身从 capabilities 里取出对应能力，按序调用
    handle，把结果记进 TaskOutcome.records。outcome 给定时不处理操作，原样返回；
    raises 给定时直接抛出。校验（validate）属于能力自身，这里不调用。
    收到的请求与能力记在 request / capabilities 上。
    """

    def __init__(self, operations=(), *, status=TaskStatus.COMPLETED, outcome=None, raises=None):
        self.operations = tuple(operations)
        self.status = status
        self.outcome = outcome
        self.raises = raises
        self.callCount = 0
        self.request = None
        self.capabilities: list = []
        self.results: list[OperationResult] = []

    async def __call__(self, request, *, capabilities, feedback=""):
        self.callCount += 1
        self.request = request
        self.capabilities = list(capabilities)
        if self.raises is not None:
            raise self.raises
        if self.outcome is not None:
            return self.outcome

        byName = {capability.name: capability for capability in capabilities}
        task = TaskInfo(
            taskID="test",
            chatID=str(request.chatID),
            userID=request.userID,
            triggerMessageID=request.triggerMessageID,
            includeContext=request.includeContext,
        )
        records = []
        for index, (name, args) in enumerate(self.operations, start=1):
            operation = Operation(index=index, name=name, args=args)
            result = await byName[name].handle(operation, task)
            records.append(OperationRecord(operation=operation, result=result))

        self.results = [record.result for record in records]
        return TaskOutcome(status=self.status, records=tuple(records), modelCalls=1)


class _Pipeline:
    """
    _runLLMPipeline 的测试台。

    替换防抖等待、缓冲读取、全局配置读取、模型任务和日志；三个能力由管线真实构造，
    发言能力的送出走 utils/llm/capability/message.py 的真实代码，只把 on 模式最终的
    sendLLMReply 换成 self.sendReply。默认 on 模式、一个 ops、批次里一条消息。
    """

    def __init__(self, *, autoMode="on", ops=_OPS, messages=None):
        self.autoMode = autoMode
        self.ops = list(ops)
        self.messages = _makeMessages() if messages is None else messages
        self.context = SimpleNamespace(bot=AsyncMock(), bot_data={})
        self.sleep = AsyncMock()
        self.popPendingMessages = MagicMock(return_value=self.messages)
        self.addRateLimit = MagicMock()
        self.logAction = AsyncMock()
        self.sendReply = AsyncMock()
        self.recordReaction = AsyncMock()

    async def run(self, taskDouble):
        targets = {
            "handlers.llm.asyncio.sleep": self.sleep,
            "handlers.llm.popPendingMessages": self.popPendingMessages,
            "handlers.llm.consumeContextOnce": MagicMock(return_value=False),
            "handlers.llm.getAutoMode": MagicMock(return_value=self.autoMode),
            "handlers.llm.getOperatorsWithPermission": MagicMock(return_value=self.ops),
            "handlers.llm.runRequest": taskDouble,
            "handlers.llm.addRateLimit": self.addRateLimit,
            "handlers.llm.logAction": self.logAction,
            # 走真实 deliverMessage 的用例不发送、不写日志文件
            "utils.llm.capability.message.sendLLMReply": self.sendReply,
            "utils.llm.capability.message.logAction": AsyncMock(),
            # 走真实 sendReaction 的用例不写聊天历史、不写日志文件
            "utils.llm.capability.reaction.recordBotReaction": self.recordReaction,
            "utils.llm.capability.reaction.logAction": AsyncMock(),
            # 记忆操作一律送审，不读运行时配置文件里的 memoryAutoApprove
            "utils.llm.capability.memory.getMemoryAutoApprove": MagicMock(return_value=False),
        }

        with ExitStack() as stack:
            for target, replacement in targets.items():
                stack.enter_context(patch(target, replacement))
            await _runLLMPipeline(debounceKey=_DEBOUNCE_KEY, context=self.context)

    def sentTexts(self) -> list[str]:
        """经 bot.send_message 发出的文本，按发送顺序。"""
        return [sent.kwargs["text"] for sent in self.context.bot.send_message.await_args_list]

    def loggedEvents(self) -> list[str]:
        return [logged.args[1] for logged in self.logAction.await_args_list]




# ============================================================================
# 入口门禁与防抖入队
# ============================================================================

class TestMessageGates:

    async def test_authorized_message_reaches_debounce(self, mockUpdate, mockContext):
        with ExitStack() as stack:
            for p in _gatePatches():
                stack.enter_context(p)
            enqueue = stack.enter_context(patch("handlers.llm._enqueueLLMDebounce", new_callable=AsyncMock))
            await _run(mockUpdate, mockContext)
        enqueue.assert_awaited_once()

    @pytest.mark.parametrize("gate", ["getLLMEnabled", "whetherAuthorizedUser", "shouldTriggerLLM"])
    async def test_rejected_message_does_not_reach_debounce(self, mockUpdate, mockContext, gate):
        with ExitStack() as stack:
            for p in _gatePatches(**{f"handlers.llm.{gate}": False}):
                stack.enter_context(p)
            enqueue = stack.enter_context(patch("handlers.llm._enqueueLLMDebounce", new_callable=AsyncMock))
            await _run(mockUpdate, mockContext)
        enqueue.assert_not_awaited()

    async def test_disabled_llm_consumes_extra_blocks(self, mockUpdate, mockContext):
        key = f"llm_extra_blocks_{mockUpdate.effective_chat.id}_{mockUpdate.message.message_id}"
        mockContext.bot_data[key] = {"afc": (200, "工具", "临时块")}
        with ExitStack() as stack:
            for p in _gatePatches(**{"handlers.llm.getLLMEnabled": False}):
                stack.enter_context(p)
            await _run(mockUpdate, mockContext)
        assert key not in mockContext.bot_data

    async def test_caption_message_reaches_debounce(self, mockUpdate, mockContext, mockMessage):
        mockMessage.text = None
        mockMessage.caption = "图片说明"
        with ExitStack() as stack:
            for p in _gatePatches():
                stack.enter_context(p)
            enqueue = stack.enter_context(patch("handlers.llm._enqueueLLMDebounce", new_callable=AsyncMock))
            await _run(mockUpdate, mockContext)
        assert enqueue.await_args.kwargs["messageInput"].text == "图片说明"


class TestMemoryQueryWiring:

    async def test_image_notes_do_not_pollute_memory_turn(self):
        memoryTurn = MemoryTurn(
            currentText="看这张图",
            replyText="引用原文",
            currentSender="@alice",
            replySender="@bob",
        )
        payload = MessageInput(
            origin=MessageOrigin(chatID="100", userID=42, messageID=9, sender="alice"),
            text="看这张图",
            replyText="引用原文",
            replySender="bob",
            includeContext=True,
            memoryTurn=memoryTurn,
        )
        with patch(
            "handlers.llm.downloadImages",
            new_callable=AsyncMock,
            return_value=([{"data": "image", "mimeType": "image/jpeg"}], ["[图片过大]"]),
        ):
            annotated = await _downloadMessageImages(
                MagicMock(),
                [MagicMock()],
                payload,
            )

        assert annotated.text == "看这张图"
        assert annotated.imageNotes == ("[图片过大]",)
        assert annotated.memoryTurn is memoryTurn
        assert annotated.images == ({"data": "image", "mimeType": "image/jpeg"},)

    async def test_enqueue_forwards_memory_turn_to_pending_buffer(self):
        memoryTurn = MemoryTurn(currentText="当前", replyText="引用")
        payload = MessageInput(
            origin=MessageOrigin(chatID="100", userID=42, messageID=9, sender="alice"),
            text="当前",
            replyText="引用",
            replySender="bob",
            includeContext=True,
            memoryTurn=memoryTurn,
        )
        message = AsyncMock()
        with patch("handlers.llm.appendPendingMessage", return_value=False) as mockAppend:
            accepted = await _enqueueLLMDebounce(
                message=message,
                context=MagicMock(),
                messageInput=payload,
            )

        assert accepted is False
        assert mockAppend.call_args.args == ("100:42",)
        assert mockAppend.call_args.kwargs["message"] is payload

    async def test_pipeline_keeps_memory_query_in_task_and_delivery(self):
        """任务收到的请求、送审快照里的记忆查询与重试输入都来自同一批消息。"""
        memoryQuery = MemoryQuery(
            turns=(MemoryTurn(currentText="当前", replyText="引用"),),
        )
        messages = _makeMessages(
            includeContext=True,
            memoryTurn=memoryQuery.turns[0],
            extraBlocks=((200, "工具", "工具说明"),),
        )
        pipeline = _Pipeline(autoMode="off", messages=messages)
        pipeline.context.bot.send_message.return_value = SimpleNamespace(message_id=501)
        task = _TaskDouble([_MESSAGE_OP])

        await pipeline.run(task)

        request = task.request
        assert request.memoryQuery == memoryQuery
        assert request.chatID == "100"
        assert request.userID == 42
        assert request.includeContext is True
        assert request.extraBlocks == ((200, "工具", "工具说明"),)
        assert request.origin is messages[-1].origin
        entry = pipeline.context.bot_data[replyReviewKey(_CHAT_ID, 501)]
        assert entry["memoryQuery"] == memoryQuery
        assert entry["request"] is request
        assert entry["originalMsg"] == request.displayOriginalMsg
        assert entry["includeContext"] is True


async def test_message_handler_reaches_real_debounce_queue(mockUpdate, mockContext):
    """入口使用真实入队函数，检查消息身份和后台任务调度。"""
    from utils.llm.state import getPendingTask, makeDebounceKey, popPendingMessages

    mockContext.bot.username = "ZincNyaBot"
    with ExitStack() as stack:
        for p in _gatePatches():
            if p.attribute != "_enqueueLLMDebounce":
                stack.enter_context(p)
        key = makeDebounceKey(str(mockUpdate.effective_chat.id), mockUpdate.message.from_user.id)
        try:
            await _run(mockUpdate, mockContext)
            task = getPendingTask(key)
            assert task is not None
            assert not task.done()
            messages = popPendingMessages(key)
            assert len(messages) == 1
            assert messages[0].text == "test message"
            assert messages[0].origin.messageID == mockUpdate.message.message_id
            assert messages[0].origin.chatID == str(mockUpdate.effective_chat.id)
        finally:
            task = getPendingTask(key)
            if task is not None:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                await asyncio.sleep(0)
            popPendingMessages(key)


async def test_anonymous_message_is_skipped(mockUpdate, mockContext):
    mockUpdate.message.from_user = None
    with ExitStack() as stack:
        for p in _gatePatches():
            stack.enter_context(p)
        enqueue = stack.enter_context(patch("handlers.llm._enqueueLLMDebounce", new_callable=AsyncMock))
        await _run(mockUpdate, mockContext)
    enqueue.assert_not_awaited()


def test_extra_blocks_are_consumed_by_message_id():
    message = SimpleNamespace(chat_id=100, message_id=9)
    context = SimpleNamespace(bot_data={
        "llm_extra_blocks_100_9": {"afc": (200, "工具", "本条消息的工具")},
        "llm_extra_blocks_100_10": {"afc": (200, "工具", "下一条消息的工具")},
    })
    assert _takeExtraBlocks(context, message) == ((200, "工具", "本条消息的工具"),)
    assert "llm_extra_blocks_100_9" not in context.bot_data
    assert "llm_extra_blocks_100_10" in context.bot_data




# ============================================================================
# 任务管线：没有负责的 ops
# ============================================================================

class TestPipelineWithoutOps:

    @pytest.mark.parametrize("autoMode", ["on", "off", "console"])
    async def test_missing_ops_sends_hint_without_calling_model(self, autoMode):
        pipeline = _Pipeline(autoMode=autoMode, ops=())
        task = _TaskDouble([_MESSAGE_OP])

        await pipeline.run(task)

        pipeline.context.bot.send_message.assert_awaited_once_with(chat_id=_CHAT_ID, text=_HINT_NO_OPS)
        assert task.callCount == 0
        pipeline.addRateLimit.assert_not_called()




# ============================================================================
# 任务管线：三个能力的接线
# ============================================================================

class TestCapabilityWiring:
    """管线交给任务的三个能力带着本次请求与 context.bot；发言和记忆能力还带着 context.bot_data、autoMode 与 opsList。"""

    async def test_on_mode_message_replies_to_trigger_message(self):
        pipeline = _Pipeline(autoMode="on")
        task = _TaskDouble([_MESSAGE_OP])

        await pipeline.run(task)

        assert [capability.name for capability in task.capabilities] == [
            MESSAGE_CAPABILITY_NAME, MEMORY_CAPABILITY_NAME, REACTION_CAPABILITY_NAME,
        ]
        assert task.results[0].status == OperationStatus.DONE
        pipeline.sendReply.assert_awaited_once_with(
            bot=pipeline.context.bot,
            chatID=_CHAT_ID,
            reply=_REPLY_TEXT,
            replyToMessageID=_TRIGGER_MSG_ID,
            maxLength=TG_MESSAGE_MAX_LEN,
        )
        pipeline.context.bot.send_message.assert_not_awaited()
        assert pipeline.context.bot_data == {}
        assert pipeline.sleep.await_args_list == [call(LLM_DEBOUNCE_SECONDS)]

    async def test_off_mode_message_sends_review_card_to_each_ops(self):
        pipeline = _Pipeline(autoMode="off", ops=("111", "222"))
        bot = pipeline.context.bot
        bot.send_message.side_effect = [
            SimpleNamespace(message_id=501),
            SimpleNamespace(message_id=502),
        ]
        task = _TaskDouble([_MESSAGE_OP])

        await pipeline.run(task)

        assert task.results[0].status == OperationStatus.SUBMITTED
        pipeline.sendReply.assert_not_awaited()
        assert [sent.kwargs["chat_id"] for sent in bot.send_message.await_args_list] == [111, 222]
        for reviewMsgID, opsID in ((501, 111), (502, 222)):
            entry = pipeline.context.bot_data[replyReviewKey(_CHAT_ID, reviewMsgID)]
            assert entry["reply"] == _REPLY_TEXT
            assert entry["opsID"] == opsID
            assert entry["autoMode"] == "off"
            assert entry["triggerMsgID"] == _TRIGGER_MSG_ID
            assert entry["request"] is task.request
        bot.set_message_reaction.assert_awaited_once()
        reaction = bot.set_message_reaction.await_args.kwargs
        assert (reaction["chat_id"], reaction["message_id"]) == (_CHAT_ID, _TRIGGER_MSG_ID)

    async def test_off_mode_memory_action_sends_review_card_to_first_ops(self):
        pipeline = _Pipeline(
            autoMode="off", ops=("111", "222"), messages=_makeMessages(includeContext=True),
        )
        bot = pipeline.context.bot
        bot.send_message.return_value = SimpleNamespace(message_id=601)
        task = _TaskDouble([_MEMORY_OP])

        await pipeline.run(task)

        assert task.results[0].status == OperationStatus.SUBMITTED
        bot.send_message.assert_awaited_once()
        assert bot.send_message.await_args.kwargs["chat_id"] == 111
        entry = pipeline.context.bot_data[memoryReviewKey(_CHAT_ID, 601)]
        assert (entry["action"]["action"], entry["action"]["content"]) == ("add", "喜欢乌龙茶")
        assert entry["opsID"] == 111
        assert entry["userID"] == _USER_ID
        assert entry["originalMsg"] == task.request.displayOriginalMsg
        assert entry["displayBlocks"] == task.request.displayBlocks

    @pytest.mark.parametrize("autoMode", ["on", "off", "console"])
    async def test_reaction_is_set_on_trigger_message_in_every_mode(self, autoMode):
        """reaction 不经审核：三种模式下都直接加在触发消息上，不产生审核项。"""
        pipeline = _Pipeline(autoMode=autoMode)
        bot = pipeline.context.bot
        task = _TaskDouble([_REACTION_OP])

        await pipeline.run(task)

        assert task.results[0].status == OperationStatus.DONE
        bot.set_message_reaction.assert_awaited_once()
        reaction = bot.set_message_reaction.await_args.kwargs
        assert (reaction["chat_id"], reaction["message_id"]) == (_CHAT_ID, _TRIGGER_MSG_ID)
        assert [item.emoji for item in reaction["reaction"]] == ["👍"]
        pipeline.recordReaction.assert_awaited_once_with(_CHAT_ID, "👍")
        bot.send_message.assert_not_awaited()
        assert pipeline.context.bot_data == {}




# ============================================================================
# 任务管线：速率限制与任务结束后的提示
# ============================================================================

class TestRateLimit:

    @pytest.mark.parametrize("status, records, modelCalls, counted", [
        (TaskStatus.COMPLETED, (_makeRecord(),), 1, True),
        (TaskStatus.COMPLETED, (), 1, True),
        (TaskStatus.LIMIT_REACHED, (), 16, True),
        (TaskStatus.FAILED, (_makeRecord(OperationStatus.UNCERTAIN),), 2, True),
        # 整批被退回不留记录，但模型已经成功响应过一次
        (TaskStatus.FAILED, (), 2, True),
        # 第一次请求就失败，或输入准备失败
        (TaskStatus.FAILED, (), 1, False),
        (TaskStatus.FAILED, (), 0, False),
    ])
    async def test_rate_limit_follows_task_outcome(self, status, records, modelCalls, counted):
        error = RuntimeError("模型请求失败") if status == TaskStatus.FAILED else None
        pipeline = _Pipeline()
        task = _TaskDouble(outcome=TaskOutcome(
            status=status, records=records, modelCalls=modelCalls, error=error,
        ))

        await pipeline.run(task)

        if counted:
            pipeline.addRateLimit.assert_called_once_with(_USER_ID)
        else:
            pipeline.addRateLimit.assert_not_called()


class TestAbnormalEnd:

    @pytest.mark.parametrize("status, records, error, hint", [
        (TaskStatus.LIMIT_REACHED, (_makeRecord(),), None, _HINT_LIMIT_REACHED),
        (TaskStatus.FAILED, (), TimeoutError("request timeout"), _HINT_MODEL_RETRYABLE),
        (TaskStatus.FAILED, (), ValueError("响应无法解析"), _HINT_MODEL_FAILED),
        (TaskStatus.FAILED, (_makeRecord(),), TimeoutError("request timeout"), _HINT_UNFINISHED),
        (TaskStatus.STOPPED, (_makeRecord(),), None, _HINT_UNFINISHED),
    ])
    async def test_abnormal_end_sends_one_hint(self, status, records, error, hint):
        pipeline = _Pipeline()
        task = _TaskDouble(outcome=TaskOutcome(status=status, records=records, error=error))

        await pipeline.run(task)

        pipeline.context.bot.send_message.assert_awaited_once_with(chat_id=_CHAT_ID, text=hint)
        logged = pipeline.logAction.await_args
        assert "LLM 任务未正常完成" in logged.args[1]
        assert logged.args[3] == (LogLevel.ERROR if status == TaskStatus.FAILED else LogLevel.WARNING)

    async def test_hint_network_error_is_swallowed(self):
        pipeline = _Pipeline()
        pipeline.context.bot.send_message.side_effect = NetworkError("断网")
        task = _TaskDouble(outcome=TaskOutcome(status=TaskStatus.LIMIT_REACHED))

        await pipeline.run(task)

        pipeline.context.bot.send_message.assert_awaited_once()
        assert not any("后台任务异常" in event for event in pipeline.loggedEvents())

    async def test_completed_without_operations_stays_silent(self):
        """模型不执行任何操作就结束：只记日志，不向触发方发消息。"""
        pipeline = _Pipeline()
        task = _TaskDouble(outcome=TaskOutcome(status=TaskStatus.COMPLETED))

        await pipeline.run(task)

        bot = pipeline.context.bot
        bot.send_message.assert_not_awaited()
        bot.set_message_reaction.assert_not_awaited()
        logged = pipeline.logAction.await_args
        assert "LLM 任务结束且没有操作" in logged.args[1]
        assert logged.args[3] == LogLevel.INFO

    async def test_completed_with_operations_sends_no_hint(self):
        pipeline = _Pipeline()
        task = _TaskDouble([_MESSAGE_OP])

        await pipeline.run(task)

        assert pipeline.sentTexts() == []
        pipeline.logAction.assert_not_awaited()




# ============================================================================
# 任务管线：取消与后台异常
# ============================================================================

class TestPipelineErrors:

    async def test_cancel_during_debounce_propagates_and_keeps_buffer(self):
        pipeline = _Pipeline()
        pipeline.sleep.side_effect = asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await pipeline.run(_TaskDouble())

        pipeline.popPendingMessages.assert_not_called()
        pipeline.logAction.assert_not_awaited()

    async def test_cancel_during_task_propagates(self):
        pipeline = _Pipeline()
        task = _TaskDouble(raises=asyncio.CancelledError())

        with pytest.raises(asyncio.CancelledError):
            await pipeline.run(task)

        assert task.callCount == 1
        pipeline.logAction.assert_not_awaited()
        pipeline.context.bot.send_message.assert_not_awaited()

    async def test_task_exception_is_logged_not_raised(self):
        pipeline = _Pipeline()
        task = _TaskDouble(raises=RuntimeError("任务入口异常"))

        await pipeline.run(task)

        logged = pipeline.logAction.await_args
        assert logged.args[1] == f"LLM 后台任务异常：{_DEBOUNCE_KEY}"
        assert logged.args[2] == "任务入口异常"
        assert logged.args[3] == LogLevel.ERROR
        pipeline.addRateLimit.assert_not_called()
        pipeline.context.bot.send_message.assert_not_awaited()

    async def test_pipeline_logs_error_before_target_is_known(self):
        pipeline = _Pipeline()
        pipeline.popPendingMessages.side_effect = RuntimeError("缓冲读取失败")

        await pipeline.run(_TaskDouble())

        logged = pipeline.logAction.await_args
        assert _DEBOUNCE_KEY in logged.args[1]
        assert logged.args[2] == "缓冲读取失败"
