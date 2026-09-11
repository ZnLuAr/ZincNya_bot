"""
tests/handlers/test_llm.py

handleLLMMessage 的 incoming 历史写入（issue #1）：
    - 门禁全绿 + 无 receiver 活跃 → 写 incoming（chatID / sender / 原文 rawText）
    - interactiveChatID 守卫：receiver 活跃于该聊天 → 让位不写；异聊天 → 照写
    - 门禁早退（LLM 关 / 未授权）→ 不写
    - 写入不影响主流程（防抖入队照常）

awaitability 铁律：getLLMEnabled / whetherAuthorizedUser / shouldTriggerLLM / isRateLimited
是同步 def——必须 MagicMock(return_value=...)，patch 成 AsyncMock 会返回协程对象（恒真值），
isRateLimited=False 变恒限速、写入用例全假红；handleEditReply / handleFeedbackRetry /
downloadImages / _enqueueLLMDebounce 是 async def，用 AsyncMock。
"""

from contextlib import ExitStack
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from handlers.llm import (
    _downloadImagesAndAnnotatePrompt,
    _enqueueLLMDebounce,
    _generateReplyOrNotify,
    _runLLMPipeline,
    handleLLMMessage,
)
from utils.llm.memory.types import MemoryQuery, MemoryTurn
from utils.llm.messagePrep import PromptPayload
from utils.llm.review import DispatchTarget
from utils.llm.state import DebouncedBatch


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


def _stateManagerPatch(interactiveChatID):
    """getStateManager → MagicMock，getInteractiveChatID 返回指定值"""
    sm = MagicMock()
    sm.getInteractiveChatID.return_value = interactiveChatID
    return patch("handlers.llm.getStateManager", return_value=sm)


def _savePatch():
    return patch("handlers.llm.saveMessage", new_callable=AsyncMock)


async def _run(mockUpdate, mockContext):
    # conftest 的 mockMessage.photo / .document 是 MagicMock（truthy），会骗过
    # extractImageRefsForPrompt 走进 _pickBestPhoto 遍历 mock 抛 TypeError——
    # 显式清空表示「纯文本消息」，走真实的图片提取（结果为空，无需 patch）
    mockUpdate.message.photo = ()
    mockUpdate.message.document = None
    await handleLLMMessage(mockUpdate, mockContext)


class TestIncomingHistoryWrite:

    async def test_incoming_written_after_gate(self, mockUpdate, mockContext):
        """门禁全绿 + 无 receiver 活跃 → 写 incoming 恰一次，参数为 (str(chatID), incoming, username, rawText)"""
        with ExitStack() as stack, _savePatch() as mockSave:
            for p in _gatePatches():
                stack.enter_context(p)
            stack.enter_context(_stateManagerPatch(None))
            await _run(mockUpdate, mockContext)

        mockSave.assert_awaited_once_with("987654321", "incoming", "test_user", "test message")

    async def test_skipped_when_receiver_active(self, mockUpdate, mockContext):
        """receiver 活跃于同一聊天（interactiveChatID == chatID）→ 让位不写"""
        with ExitStack() as stack, _savePatch() as mockSave:
            for p in _gatePatches():
                stack.enter_context(p)
            stack.enter_context(_stateManagerPatch("987654321"))
            await _run(mockUpdate, mockContext)

        mockSave.assert_not_awaited()

    async def test_written_when_receiver_on_other_chat(self, mockUpdate, mockContext):
        """receiver 活跃于另一聊天 → 该聊天无人写，llm 侧照写"""
        with ExitStack() as stack, _savePatch() as mockSave:
            for p in _gatePatches():
                stack.enter_context(p)
            stack.enter_context(_stateManagerPatch("111111"))
            await _run(mockUpdate, mockContext)

        mockSave.assert_awaited_once()

    async def test_not_written_llm_disabled(self, mockUpdate, mockContext):
        """LLM 总开关关闭 → 门禁早退，不写"""
        with ExitStack() as stack, _savePatch() as mockSave:
            for p in _gatePatches(**{"handlers.llm.getLLMEnabled": False}):
                stack.enter_context(p)
            await _run(mockUpdate, mockContext)

        mockSave.assert_not_awaited()

    async def test_not_written_unauthorized(self, mockUpdate, mockContext):
        """非白名单用户 → 门禁早退，不写"""
        with ExitStack() as stack, _savePatch() as mockSave:
            for p in _gatePatches(**{"handlers.llm.whetherAuthorizedUser": False}):
                stack.enter_context(p)
            await _run(mockUpdate, mockContext)

        mockSave.assert_not_awaited()

    async def test_enqueue_still_called(self, mockUpdate, mockContext):
        """写入后主流程不受影响：防抖入队照常被调（单独 patch 以持有同一 mock 实例）"""
        with ExitStack() as stack:
            stack.enter_context(_savePatch())
            for p in _gatePatches():
                stack.enter_context(p)
            stack.enter_context(_stateManagerPatch(None))
            enqueue = stack.enter_context(patch("handlers.llm._enqueueLLMDebounce", new_callable=AsyncMock, return_value=True))
            await _run(mockUpdate, mockContext)

        enqueue.assert_awaited_once()

    async def test_caption_message_uses_caption(self, mockUpdate, mockContext, mockMessage):
        """图片消息（text=None）→ content 走 caption"""
        mockMessage.text = None
        mockMessage.caption = "图片说明"
        with ExitStack() as stack, _savePatch() as mockSave:
            for p in _gatePatches():
                stack.enter_context(p)
            stack.enter_context(_stateManagerPatch(None))
            await _run(mockUpdate, mockContext)

        mockSave.assert_awaited_once()
        assert mockSave.await_args.args[3] == "图片说明"


class TestMemoryQueryWiring:

    async def test_image_notes_do_not_pollute_memory_turn(self):
        memoryTurn = MemoryTurn(
            currentText="看这张图",
            replyText="引用原文",
            currentSender="@alice",
            replySender="@bob",
        )
        payload = PromptPayload(
            pureText="看这张图",
            includeContext=True,
            urlIntentText="看这张图",
            urlCandidateText="看这张图",
            currentText="看这张图",
            memoryTurn=memoryTurn,
        )
        with patch(
            "handlers.llm.downloadImages",
            new_callable=AsyncMock,
            return_value=([{"data": "image", "mimeType": "image/jpeg"}], ["[图片过大]"]),
        ):
            annotated, images = await _downloadImagesAndAnnotatePrompt(
                MagicMock(),
                [MagicMock()],
                payload,
            )

        assert annotated.pureText == "[图片过大]\n看这张图"
        assert annotated.currentText == "[图片过大]\n看这张图"
        assert annotated.memoryTurn is memoryTurn
        assert images == [{"data": "image", "mimeType": "image/jpeg"}]

    async def test_enqueue_forwards_memory_turn_to_pending_buffer(self):
        memoryTurn = MemoryTurn(currentText="当前", replyText="引用")
        payload = PromptPayload(
            pureText="prompt",
            includeContext=True,
            urlIntentText="当前",
            urlCandidateText="当前\n引用",
            replyLine="<@bob> 引用",
            currentText="当前",
            memoryTurn=memoryTurn,
        )
        message = AsyncMock()
        target = DispatchTarget(
            chatID="100",
            userID=42,
            username="alice",
            triggerMsgID=9,
        )
        with patch("handlers.llm.appendPendingMessage", return_value=False) as mockAppend:
            accepted = await _enqueueLLMDebounce(
                message=message,
                context=MagicMock(),
                target=target,
                payload=payload,
                images=[],
            )

        assert accepted is False
        assert mockAppend.call_args.kwargs["memoryTurn"] is memoryTurn

    async def test_generate_helper_forwards_memory_query(self):
        memoryQuery = MemoryQuery(turns=(MemoryTurn(currentText="当前"),))
        context = MagicMock()
        with patch(
            "handlers.llm.generateReply",
            new_callable=AsyncMock,
            return_value="回复",
        ) as mockGenerate:
            result = await _generateReplyOrNotify(
                context=context,
                combinedText="prompt",
                chatID="100",
                includeContext=True,
                userID=42,
                allImages=[],
                memoryQuery=memoryQuery,
            )

        assert result == "回复"
        assert mockGenerate.await_args.kwargs["memoryQuery"] is memoryQuery

    async def test_pipeline_keeps_memory_query_in_generation_and_dispatch(self):
        memoryQuery = MemoryQuery(
            turns=(MemoryTurn(currentText="当前", replyText="引用"),),
        )
        batch = DebouncedBatch(
            combinedText="prompt",
            includeContext=True,
            images=[],
            urlIntentText="当前",
            urlCandidateText="当前\n引用",
            memoryQuery=memoryQuery,
        )
        target = DispatchTarget(
            chatID="100",
            userID=42,
            username="alice",
            triggerMsgID=9,
        )
        context = MagicMock()
        with (
            patch("handlers.llm.asyncio.sleep", new_callable=AsyncMock),
            patch("handlers.llm.collectDebouncedBatch", return_value=batch),
            patch(
                "utils.llm.urlReader.readURLContextsForUserText",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch("handlers.llm._sendTypingActionSafely", new_callable=AsyncMock),
            patch(
                "handlers.llm._generateReplyOrNotify",
                new_callable=AsyncMock,
                return_value="回复",
            ) as mockGenerate,
            patch(
                "handlers.llm.extractValidatedMemoryActions",
                new_callable=AsyncMock,
                return_value=("回复", [], 0),
            ),
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
                target=target,
                context=context,
            )

        assert mockGenerate.await_args.kwargs["memoryQuery"] is memoryQuery
        generated = mockDispatch.await_args.args[0]
        assert generated.memoryQuery is memoryQuery
