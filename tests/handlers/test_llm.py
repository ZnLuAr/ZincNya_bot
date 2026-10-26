"""
tests/handlers/test_llm.py

验证 LLM 入口门禁、防抖调度和生成输入；来信历史由全局收信入口记录。

awaitability 铁律：getLLMEnabled / whetherAuthorizedUser / shouldTriggerLLM / isRateLimited
是同步 def——必须 MagicMock(return_value=...)，patch 成 AsyncMock 会返回协程对象（恒真值），
isRateLimited=False 变恒限速、写入用例全假红；handleEditReply / handleFeedbackRetry /
downloadImages / _enqueueLLMDebounce 是 async def，用 AsyncMock。
"""

import asyncio
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from handlers.llm import (
    _takeExtraBlocks,
    _downloadMessageImages,
    _enqueueLLMDebounce,
    _generateReplyOrNotify,
    _runLLMPipeline,
    handleLLMMessage,
)
from utils.llm.memory.types import MemoryQuery, MemoryTurn
from utils.llm.models import GenerationRequest, GenerationResult, MessageInput, MessageOrigin


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

    async def test_generate_helper_forwards_memory_query(self):
        context = MagicMock()
        with patch(
            "handlers.llm.generateResponse",
            new_callable=AsyncMock,
            return_value=GenerationResult(reply="回复"),
        ) as mockGenerate:
            result = await _generateReplyOrNotify(
                context=context,
                request=GenerationRequest(
                    messages=(MessageInput(
                        origin=MessageOrigin(chatID="100", userID=42, messageID=1), text="当前",
                    ),),
                    includeContext=True,
                ),
            )

        assert result == GenerationResult(reply="回复")
        assert isinstance(mockGenerate.await_args.args[0], GenerationRequest)

    async def test_pipeline_keeps_memory_query_in_generation_and_dispatch(self):
        memoryQuery = MemoryQuery(
            turns=(MemoryTurn(currentText="当前", replyText="引用"),),
        )
        messages = (MessageInput(
            origin=MessageOrigin(chatID="100", userID=42, messageID=9, sender="alice"), text="当前",
            replyText="引用", replySender="bob", includeContext=True,
            memoryTurn=memoryQuery.turns[0],
            extraBlocks=((200, "工具", "工具说明"),),
        ),)
        context = SimpleNamespace(bot=AsyncMock(), bot_data={})
        with (
            patch("handlers.llm.asyncio.sleep", new_callable=AsyncMock),
            patch("handlers.llm.popPendingMessages", return_value=messages),
            patch(
                "handlers.llm.readURLContextsForUserText",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch("handlers.llm._sendTypingActionSafely", new_callable=AsyncMock),
            patch(
                "handlers.llm._generateReplyOrNotify",
                new_callable=AsyncMock,
                return_value=GenerationResult(reply="回复"),
            ) as mockGenerate,
            patch("handlers.llm.addRateLimit"),
            patch("handlers.llm.getAutoMode", return_value="on"),
            patch("handlers.llm.getOperatorsWithPermission", return_value=[]),
            patch(
                "handlers.llm.dispatchGeneratedOutput",
                new_callable=AsyncMock,
            ) as mockDispatch,
        ):
            await _runLLMPipeline(
                debounceKey="100:42",
                context=context,
            )

        request = mockGenerate.await_args.kwargs["request"]
        assert request.memoryQuery == memoryQuery
        assert request.chatID == "100"
        assert request.userID == 42
        assert request.extraBlocks == ((200, "工具", "工具说明"),)
        generated = mockDispatch.await_args.args[0]
        assert generated.memoryQuery == memoryQuery
        assert mockDispatch.await_args.kwargs["origin"] is messages[-1].origin


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


async def test_pipeline_logs_error_before_target_is_known():
    with (
        patch("handlers.llm.asyncio.sleep", new_callable=AsyncMock),
        patch("handlers.llm.popPendingMessages", side_effect=RuntimeError("缓冲读取失败")),
        patch("handlers.llm.logAction", new_callable=AsyncMock) as log,
    ):
        await _runLLMPipeline(debounceKey="100:42", context=MagicMock())
    assert "100:42" in log.await_args.args[1]
    assert log.await_args.args[2] == "缓冲读取失败"


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
