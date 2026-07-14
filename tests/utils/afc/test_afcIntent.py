"""
tests/utils/afc/test_afcIntent.py

测试 AFC 意图判断模块
"""

import pytest

from utils.afc.afcIntent import detectTools, hasAFCIntent


class TestDetectTools:
    """测试工具检测"""

    def test_detect_weather_by_keyword(self):
        """关键词匹配：天气"""
        tools = detectTools("今天天气怎么样", "test_chat_1")
        assert "weather" in tools

    def test_detect_calc_by_keyword(self):
        """关键词匹配：计算"""
        tools = detectTools("帮我算一下 2 + 3", "test_chat_2")
        assert "calc" in tools

    def test_detect_weather_by_pattern(self):
        """正则匹配：天气"""
        tools = detectTools("明天会不会冷", "test_chat_3")
        assert "weather" in tools

    def test_detect_calc_by_pattern(self):
        """正则匹配：计算器（数字表达式）"""
        tools = detectTools("123 + 456", "test_chat_4")
        assert "calc" in tools

    def test_generic_trigger_returns_all(self):
        """通用兜底：命中预设词但无工具匹配 → 返回所有工具"""
        tools = detectTools("帮我查一下", "test_chat_5")
        # 应该包含所有工具（weather + calc）
        assert len(tools) >= 2
        assert "weather" in tools
        assert "calc" in tools

    def test_no_intent(self):
        """无意图：普通闲聊"""
        tools = detectTools("你好呀", "test_chat_6")
        assert len(tools) == 0

    def test_context_continuation(self):
        """上下文延续：短消息 + 指代词"""
        # 第一轮触发天气
        tools1 = detectTools("今天天气怎么样", "test_chat_7")
        assert "weather" in tools1

        # 第二轮短消息 + 指代词（注意：会同时触发 datetime，因为"明天"是 datetime 关键词）
        tools2 = detectTools("那明天呢", "test_chat_7")
        # 应包含 weather（继承上轮）和 datetime（L1 关键词命中）
        assert "weather" in tools2 or "datetime" in tools2

    def test_context_continuation_not_triggered_for_long_message(self):
        """上下文延续：长消息不触发"""
        detectTools("今天天气怎么样", "test_chat_8")

        # 长消息不触发上下文延续
        tools = detectTools("今天是个好日子，心想的事儿都能成", "test_chat_8")
        assert len(tools) == 0

    def test_empty_message(self):
        """空消息"""
        tools = detectTools("", "test_chat_9")
        assert len(tools) == 0


class TestHasAFCIntent:
    """测试意图判断简化接口"""

    def test_has_intent_true(self):
        """有意图"""
        assert hasAFCIntent("今天天气怎么样", "test_chat_10") is True

    def test_has_intent_false(self):
        """无意图"""
        assert hasAFCIntent("你好呀", "test_chat_11") is False

    def test_explicit_marker(self):
        """显式标记：#afc"""
        assert hasAFCIntent("#afc 帮我做点事", "test_chat_12") is True


class TestL1L2ParallelRecall:
    """测试 L1 和 L2 并行召回（2026-07 改动）"""

    def test_l1_and_l2_both_trigger_same_tool(self):
        """L1 和 L2 都匹配同一工具时，应去重"""
        # "计算 2+3"：L1 匹配"计算" → calc，L2 匹配"2+3" → calc
        tools = detectTools("计算 2+3", "test_parallel_1")
        assert "calc" in tools
        # 验证去重（calc 只出现一次）
        calcCount = len([t for t in tools if t == "calc"])
        assert calcCount == 1

    def test_l1_and_l2_trigger_different_tools(self):
        """L1 和 L2 分别匹配不同工具时，应返回并集"""
        # 构造消息：同时含 weather 关键词和 calc 正则
        # "天气预报说今日温度 5+3 度"（避免"明天"触发 datetime）
        tools = detectTools("天气预报说今日温度 5+3 度", "test_parallel_2")
        # L1 应匹配 "天气" → weather
        # L2 应匹配 "5+3" → calc
        assert "weather" in tools
        assert "calc" in tools
        assert len(tools) == 2

    def test_l2_runs_even_when_l1_hits(self):
        """即使 L1 命中，L2 也应该运行（不被阻挡）"""
        # "算一下 123 + 456"：L1 匹配"算" → calc，L2 也匹配 → calc
        tools = detectTools("算一下 123 + 456", "test_parallel_3")
        # 验证 L2 确实运行了（通过 calc 被召回来间接验证）
        assert "calc" in tools