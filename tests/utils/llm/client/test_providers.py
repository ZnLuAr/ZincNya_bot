"""
tests/utils/llm/client/test_providers.py

三 provider 的「截断提额重试」测试（思考模型思考吃光 max_tokens 预算的兜底）：

    - 正常响应 → 返回文本，只调一次
    - 截断形态（anthropic: 仅 thinking block / openai: finish_reason=length + content 空 /
      gemini: text=None）→ 提额（×2，封顶 cap）重试一次；重试成功 → 返回文本
    - 重试后仍截断 → 抛 RuntimeError（requestWithRetry 判不可重试，冒泡给上层错误路径）
    - anthropic：thinking 标签清理逻辑在重试成功后照常生效
    - openai：finish_reason=stop + content 空（模型有意空回）→ 不重试，返回空串（交上层空检测）
"""

from types import SimpleNamespace
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from config import LLM_MAX_TOKENS_HARD_CAP

from utils.llm.client.anthropic import AnthropicProvider
from utils.llm.client.gemini import GeminiProvider
from utils.llm.client.openaiCompat import OpenAICompatProvider


_KWARGS = {
    "systemMessages": ["你是测试助手"],
    "userContent": "你好",
    "model": "test-model",
    "maxTokens": 1024,
    "temperature": 0.5,
}


def _makeAnthropicResponse(blocks, stopReason):
    return SimpleNamespace(content=blocks, stop_reason=stopReason)


def _anthropicTextBlock(text):
    return SimpleNamespace(type="text", text=text)


def _anthropicThinkingBlock(text="思考中"):
    return SimpleNamespace(type="thinking", thinking=text)


def _makeOpenAIResponse(content, finishReason):
    message = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=message, finish_reason=finishReason)
    return SimpleNamespace(choices=[choice])


def _makeGeminiResponse(text, finishReason="MAX_TOKENS"):
    candidate = SimpleNamespace(finish_reason=finishReason)
    return SimpleNamespace(text=text, candidates=[candidate])



class TestAnthropicTruncationRetry:

    @pytest.fixture
    def provider(self):
        p = AnthropicProvider("test-key")
        mockClient = MagicMock()
        mockClient.messages.create = AsyncMock()
        p._client = mockClient
        return p

    async def test_normal_reply_no_retry(self, provider):
        """正常响应：返回文本，create 恰好调用 1 次"""
        provider._client.messages.create.return_value = _makeAnthropicResponse(
            [_anthropicTextBlock("你好喵")], "end_turn",
        )

        result = await provider.requestReply(**_KWARGS)

        assert result == "你好喵"
        assert provider._client.messages.create.await_count == 1
        assert provider._client.messages.create.await_args.kwargs["max_tokens"] == 1024

    async def test_thinking_only_truncated_then_success(self, provider):
        """仅 thinking block（stop_reason=max_tokens）→ 提额重试，第二次成功"""
        provider._client.messages.create.side_effect = [
            _makeAnthropicResponse([_anthropicThinkingBlock()], "max_tokens"),
            _makeAnthropicResponse(
                [_anthropicThinkingBlock(), _anthropicTextBlock("重试成功")], "end_turn",
            ),
        ]

        result = await provider.requestReply(**_KWARGS)

        assert result == "重试成功"
        assert provider._client.messages.create.await_count == 2
        # 第二次调用 max_tokens 翻倍
        secondKwargs = provider._client.messages.create.await_args_list[1].kwargs
        assert secondKwargs["max_tokens"] == 2048

    async def test_no_text_block_end_turn_also_retries(self, provider):
        """stop_reason=end_turn 但无 text block（中转丢块形态）→ 同样提额重试"""
        provider._client.messages.create.side_effect = [
            _makeAnthropicResponse([], "end_turn"),
            _makeAnthropicResponse([_anthropicTextBlock("恢复")], "end_turn"),
        ]

        result = await provider.requestReply(**_KWARGS)

        assert result == "恢复"
        assert provider._client.messages.create.await_count == 2

    async def test_retry_still_truncated_raises(self, provider):
        """重试后仍截断 → 抛 RuntimeError，信息含提额后的 max_tokens"""
        provider._client.messages.create.return_value = _makeAnthropicResponse(
            [_anthropicThinkingBlock()], "max_tokens",
        )

        with pytest.raises(RuntimeError, match="提额后仍无文本"):
            await provider.requestReply(**_KWARGS)

        assert provider._client.messages.create.await_count == 2

    async def test_max_tokens_capped(self, provider):
        """提额封顶 LLM_MAX_TOKENS_HARD_CAP（首额已超大时不冲破 cap）"""
        provider._client.messages.create.side_effect = [
            _makeAnthropicResponse([_anthropicThinkingBlock()], "max_tokens"),
            _makeAnthropicResponse([_anthropicTextBlock("ok")], "end_turn"),
        ]

        await provider.requestReply(**{**_KWARGS, "maxTokens": LLM_MAX_TOKENS_HARD_CAP})

        secondKwargs = provider._client.messages.create.await_args_list[1].kwargs
        assert secondKwargs["max_tokens"] == LLM_MAX_TOKENS_HARD_CAP

    async def test_thinking_tag_cleanup_after_retry(self, provider):
        """重试成功且响应含 thinking block：`<thinking>` 标签清理照常生效"""
        provider._client.messages.create.side_effect = [
            _makeAnthropicResponse([_anthropicThinkingBlock()], "max_tokens"),
            _makeAnthropicResponse(
                [_anthropicThinkingBlock(), _anthropicTextBlock("<thinking>内部独白</thinking>正文喵")],
                "end_turn",
            ),
        ]

        result = await provider.requestReply(**_KWARGS)

        assert result == "正文喵"

    async def test_whitespace_only_text_counts_as_empty(self, provider):
        """text block 仅空白 → 视为空，走提额重试"""
        provider._client.messages.create.side_effect = [
            _makeAnthropicResponse([_anthropicTextBlock("   \n")], "end_turn"),
            _makeAnthropicResponse([_anthropicTextBlock("有内容了")], "end_turn"),
        ]

        result = await provider.requestReply(**_KWARGS)

        assert result == "有内容了"



class TestOpenAICompatTruncationRetry:

    @pytest.fixture
    def provider(self):
        p = OpenAICompatProvider("test-key")
        mockClient = MagicMock()
        mockClient.chat.completions.create = AsyncMock()
        p._client = mockClient
        return p

    async def test_normal_reply_no_retry(self, provider):
        """正常响应：返回文本，只调一次"""
        provider._client.chat.completions.create.return_value = _makeOpenAIResponse("你好", "stop")

        result = await provider.requestReply(**_KWARGS)

        assert result == "你好"
        assert provider._client.chat.completions.create.await_count == 1

    async def test_length_empty_content_then_success(self, provider):
        """finish_reason=length + content 空 → 提额重试，第二次成功"""
        provider._client.chat.completions.create.side_effect = [
            _makeOpenAIResponse("", "length"),
            _makeOpenAIResponse("重试成功", "stop"),
        ]

        result = await provider.requestReply(**_KWARGS)

        assert result == "重试成功"
        assert provider._client.chat.completions.create.await_count == 2
        secondKwargs = provider._client.chat.completions.create.await_args_list[1].kwargs
        assert secondKwargs["max_tokens"] == 2048

    async def test_length_none_content_then_success(self, provider):
        """finish_reason=length + content=None（reasoning 吃光的典型形态）→ 同款重试"""
        provider._client.chat.completions.create.side_effect = [
            _makeOpenAIResponse(None, "length"),
            _makeOpenAIResponse("ok", "stop"),
        ]

        result = await provider.requestReply(**_KWARGS)

        assert result == "ok"
        assert provider._client.chat.completions.create.await_count == 2

    async def test_stop_empty_content_returns_empty_no_retry(self, provider):
        """finish_reason=stop + content 空：模型有意空回 → 不重试，返回空串（交上层空检测）"""
        provider._client.chat.completions.create.return_value = _makeOpenAIResponse("", "stop")

        result = await provider.requestReply(**_KWARGS)

        assert result == ""
        assert provider._client.chat.completions.create.await_count == 1

    async def test_retry_still_truncated_raises(self, provider):
        """重试后仍 length+空 → 抛 RuntimeError"""
        provider._client.chat.completions.create.return_value = _makeOpenAIResponse("", "length")

        with pytest.raises(RuntimeError, match="提额后仍无文本"):
            await provider.requestReply(**_KWARGS)

        assert provider._client.chat.completions.create.await_count == 2



class TestGeminiTruncationRetry:

    @pytest.fixture
    def provider(self):
        p = GeminiProvider("test-key")
        mockClient = MagicMock()
        mockClient.aio.models.generate_content = AsyncMock()
        p._client = mockClient
        return p

    async def test_normal_reply_no_retry(self, provider):
        """正常响应：返回文本，只调一次"""
        provider._client.aio.models.generate_content.return_value = _makeGeminiResponse("你好", "STOP")

        result = await provider.requestReply(**_KWARGS)

        assert result == "你好"
        assert provider._client.aio.models.generate_content.await_count == 1

    async def test_max_tokens_empty_text_then_success(self, provider):
        """MAX_TOKENS + text=None → 提额重试，第二次成功"""
        provider._client.aio.models.generate_content.side_effect = [
            _makeGeminiResponse(None, "MAX_TOKENS"),
            _makeGeminiResponse("重试成功", "STOP"),
        ]

        result = await provider.requestReply(**_KWARGS)

        assert result == "重试成功"
        assert provider._client.aio.models.generate_content.await_count == 2
        secondKwargs = provider._client.aio.models.generate_content.await_args_list[1].kwargs
        assert secondKwargs["config"].max_output_tokens == 2048

    async def test_retry_still_truncated_raises(self, provider):
        """重试后仍空 → 抛 RuntimeError"""
        provider._client.aio.models.generate_content.return_value = _makeGeminiResponse(None, "MAX_TOKENS")

        with pytest.raises(RuntimeError, match="提额后仍无文本"):
            await provider.requestReply(**_KWARGS)

        assert provider._client.aio.models.generate_content.await_count == 2
