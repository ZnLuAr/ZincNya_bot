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

from handlers.llm import handleLLMMessage


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
