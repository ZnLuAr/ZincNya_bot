"""
tests/utils/test_telegramHelpers.py

测试 utils.telegramHelpers.truncateText——按码点截断 + 可参数化 suffix。
该函数统一了原 llmReview._truncate / book._truncate / telegramHelpers 内联三处截断，
边界行为（limit ≤ len(suffix)）在此守护。
"""

from utils.telegramHelpers import truncateText




# ===========================================================================
# truncateText
# ===========================================================================

class TestTruncateText:
    def test_under_limit_unchanged(self):
        assert truncateText("短文本", 10) == "短文本"

    def test_over_limit_appends_default_suffix(self):
        """默认 suffix 是单字省略号 …"""
        result = truncateText("a" * 50, 20)
        assert len(result) == 20
        assert result.endswith("…")
        assert result == "a" * 19 + "…"

    def test_custom_suffix_long(self):
        """llmReview 用例：长 suffix「……[内容过长，已截断]」"""
        suffix = "……[内容过长，已截断]"
        result = truncateText("a" * 50, 20, suffix=suffix)
        assert len(result) == 20
        assert result.endswith(suffix)
        assert result == "a" * (20 - len(suffix)) + suffix

    def test_custom_suffix_ascii_dots(self):
        """telegramHelpers prepareMarkdownReply / sendLLMReply 用例：suffix '...' """
        result = truncateText("a" * 50, 20, suffix="...")
        assert len(result) == 20
        assert result.endswith("...")
        assert result == "a" * 17 + "..."

    def test_limit_le_len_suffix_returns_suffix_prefix(self):
        """limit ≤ len(suffix)：返回 suffix 前 limit 字符，避免负切片"""
        suffix = "……[内容过长，已截断]"
        result = truncateText("abcdefghij", 5, suffix=suffix)
        assert len(result) == 5
        assert result == suffix[:5]

    def test_limit_zero_returns_empty(self):
        """limit=0：返回空串（suffix[:0]）"""
        assert truncateText("abc", 0) == ""

    def test_limit_equals_len_suffix(self):
        """limit 恰好等于 suffix 长度：走 ≤ 分支，返回整个 suffix"""
        result = truncateText("abc", 1, suffix="…")
        assert result == "…"
