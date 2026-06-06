"""
tests/utils/llm/client/test_generate.py

测试 LLM 回复生成编排（utils/llm/client/_generate.py）的 system message 构建。

重点：includeContext=True 时应在 MEMORY_ACTION_INSTRUCTIONS 之后追加
OPS_FEEDBACK_INSTRUCTIONS（本批新增，为补充反馈功能提供指令）。
"""

from unittest.mock import patch

from utils.llm.client._generate import _buildSystemMessages
from utils.llm.client._guardrails import (
    SYSTEM_GUARDRAILS,
    MEMORY_ACTION_INSTRUCTIONS,
    OPS_FEEDBACK_INSTRUCTIONS,
)


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
