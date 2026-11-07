"""
tests/utils/llm/client/test_generate.py

测试 LLM 回复生成编排（utils/llm/client/_generate.py）的 system message 构建。

重点：includeContext=True 时应在 MEMORY_ACTION_INSTRUCTIONS 之后追加
OPS_FEEDBACK_INSTRUCTIONS（本批新增，为补充反馈功能提供指令）。
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from utils.llm.client._generate import _buildSystemMessages, generateReply
from utils.llm.client._guardrails import (
    SYSTEM_GUARDRAILS,
    MEMORY_ACTION_INSTRUCTIONS,
    OPS_FEEDBACK_INSTRUCTIONS,
)
from utils.llm.memory.types import MemoryQuery, MemoryTurn


_PROMPTS = {"system_prompt": "你是测试助手", "max_tokens": 1024, "temperature": 0.8}


class TestBuildSystemMessages:
    """_buildSystemMessages：人设 + guardrails (+ 记忆/反馈指令)"""

    @patch("utils.llm.client._generate.getForceFallbackPrompt", return_value=False)
    def test_base_includes_system_prompt_and_guardrails(self, mockFallback):
        """基础：含人设与 guardrails"""
        parts = _buildSystemMessages(_PROMPTS, includeContext=False)

        assert "你是测试助手" in parts
        for g in SYSTEM_GUARDRAILS:
            assert g in parts

    @patch("utils.llm.client._generate.getForceFallbackPrompt", return_value=False)
    def test_context_off_excludes_memory_and_feedback(self, mockFallback):
        """includeContext=False：不含记忆操作指令与反馈指令"""
        parts = _buildSystemMessages(_PROMPTS, includeContext=False)

        for m in MEMORY_ACTION_INSTRUCTIONS:
            assert m not in parts
        for o in OPS_FEEDBACK_INSTRUCTIONS:
            assert o not in parts

    @patch("utils.llm.client._generate.getForceFallbackPrompt", return_value=False)
    def test_context_on_includes_memory_and_feedback(self, mockFallback):
        """includeContext=True：含记忆操作指令与反馈指令"""
        parts = _buildSystemMessages(_PROMPTS, includeContext=True)

        for m in MEMORY_ACTION_INSTRUCTIONS:
            assert m in parts
        for o in OPS_FEEDBACK_INSTRUCTIONS:
            assert o in parts

    @patch("utils.llm.client._generate.getForceFallbackPrompt", return_value=False)
    def test_feedback_after_memory_instructions(self, mockFallback):
        """顺序：OPS_FEEDBACK_INSTRUCTIONS 在 MEMORY_ACTION_INSTRUCTIONS 之后"""
        parts = _buildSystemMessages(_PROMPTS, includeContext=True)

        lastMemoryIdx = max(parts.index(m) for m in MEMORY_ACTION_INSTRUCTIONS)
        firstFeedbackIdx = min(parts.index(o) for o in OPS_FEEDBACK_INSTRUCTIONS)
        assert firstFeedbackIdx > lastMemoryIdx

    @patch("utils.llm.client._generate.getForceFallbackPrompt", return_value=False)
    def test_list_system_prompt_filtered(self, mockFallback):
        """system_prompt 为 list 时，过滤空串后逐条加入"""
        prompts = {"system_prompt": ["第一段", "  ", "第二段", ""]}
        parts = _buildSystemMessages(prompts, includeContext=False)

        assert "第一段" in parts
        assert "第二段" in parts
        assert "  " not in parts
        assert "" not in parts


@pytest.mark.asyncio
async def test_generate_reply_passes_memory_query_without_extra_provider_call():
    memoryQuery = MemoryQuery(
        turns=(MemoryTurn(currentText="当前", replyText="引用"),),
    )
    provider = MagicMock()
    with (
        patch("utils.llm.client._generate.loadPrompts", return_value=_PROMPTS),
        patch("utils.llm.client._generate._buildSystemMessages", return_value=["system"]),
        patch(
            "utils.llm.client._generate.loadLLMConfig",
            return_value={"model": "main-model", "visionModel": "main-model"},
        ),
        patch(
            "utils.llm.client._generate.buildConversationContext",
            new_callable=AsyncMock,
            return_value="context",
        ) as mockContext,
        patch("utils.llm.client._generate.getProvider", return_value=provider),
        patch(
            "utils.llm.client._generate.requestWithRetry",
            new_callable=AsyncMock,
            return_value="raw reply",
        ) as mockRequest,
        patch(
            "utils.llm.afcApi.handleAFCInLLMResponse",
            new_callable=AsyncMock,
            return_value="final reply",
        ),
    ):
        result = await generateReply(
            "当前",
            "chat",
            includeContext=True,
            memoryQuery=memoryQuery,
        )

    assert result == "final reply"
    assert mockContext.await_args.kwargs["memoryQuery"] is memoryQuery
    mockRequest.assert_awaited_once()




@pytest.mark.parametrize("mainModel, mainProvider, visionModel, visionProvider, expectedCalls", [
    ("same-model", "anthropic", "same-model", "openai", 2),
    ("claude-test", "auto", "claude-test", "anthropic", 1),
    ("text-model", "openai", "vision-model", "openai", 2),
    ("same-model", "openai", "same-model", "openai", 1),
])
async def test_generationRoutesMainAndVisionFromSameSnapshot(
    mainModel, mainProvider, visionModel, visionProvider, expectedCalls,
):
    """同名但不同端点的模型必须双调用；auto 与显式同路由仍可单调用。"""
    cfg = {"model": mainModel, "modelProvider": mainProvider,
           "visionModel": visionModel, "visionProvider": visionProvider}
    with (
        patch("utils.llm.client._generate.loadPrompts", return_value=_PROMPTS),
        patch("utils.llm.client._generate._buildSystemMessages", return_value=["system"]),
        patch("utils.llm.client._generate.loadLLMConfig", return_value=cfg) as loadConfig,
        patch("utils.llm.client._generate.buildConversationContext", new_callable=AsyncMock, return_value="context"),
        patch("utils.llm.client._generate.getProvider", side_effect=lambda model, key: key) as getProvider,
        patch("utils.llm.client._generate.requestWithRetry", new_callable=AsyncMock, return_value="图片描述" * 200) as request,
        patch("utils.llm.afcApi.handleAFCInLLMResponse", new_callable=AsyncMock, return_value="最终回复"),
    ):
        result = await generateReply("当前消息", "chat", images=[{"data": "aW1hZ2U=", "mimeType": "image/png"}])
    assert result == "最终回复"
    loadConfig.assert_called_once_with()
    assert request.await_count == expectedCalls
    assert getProvider.call_args_list[-1].args == (mainModel, mainProvider)
    assert request.await_args_list[-1].args == (mainProvider,)
    assert request.await_args_list[-1].kwargs["model"] == mainModel
    if expectedCalls == 2:
        assert getProvider.call_args_list[0].args == (visionModel, visionProvider)
        assert request.await_args_list[0].kwargs["model"] == visionModel
        assert request.await_args_list[0].args == (visionProvider,)
    else:
        assert isinstance(request.await_args.kwargs["userContent"], list)


async def test_sharedRequestUsesSingleConfigSnapshotAndOpaqueModel():
    from utils.llm.client._request import requestReply
    model = "tenant/模型:latest"
    with (
        patch("utils.llm.client._request.loadLLMConfig", return_value={"model": model, "modelProvider": "anthropic"}) as loadConfig,
        patch("utils.llm.client._request.getProvider", return_value="provider") as getProvider,
        patch("utils.llm.client._request.requestWithRetry", new_callable=AsyncMock, return_value="回复") as request,
    ):
        result = await requestReply(systemBlocks=[{"text": "规则"}], userContent="内容", maxTokens=32, temperature=0.5)
    assert result == "回复"
    loadConfig.assert_called_once_with()
    getProvider.assert_called_once_with(model, "anthropic")
    assert request.await_args.kwargs["model"] == model
    assert request.await_args.kwargs["systemMessages"] == ["规则"]
