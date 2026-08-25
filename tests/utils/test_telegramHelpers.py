"""
tests/utils/test_telegramHelpers.py

测试 utils.telegramHelpers：
    - truncateText——按码点截断 + 可参数化 suffix；统一了原 llmReview._truncate /
      book._truncate / telegramHelpers 内联三处截断，边界行为（limit ≤ len(suffix)）在此守护
    - isMentionedByEntity——mention entity 精确匹配（不做子串匹配，@notmybot 不误中）
    - sendLLMReply——发送成功（含 HTML 解析失败降级重发）后写聊天历史（issue #1）
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from telegram.error import BadRequest, NetworkError

from config import BOT_DISPLAY_NAME
from utils.telegramHelpers import isMentionedByEntity, sendLLMReply, truncateText




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



# ===========================================================================
# isMentionedByEntity（mention entity 精确匹配，不做子串匹配）
# ===========================================================================

def _entity(etype, text, offset=0, length=None):
    if length is None:
        length = len(text)
    return SimpleNamespace(type=etype, offset=offset, length=length)


def _msg(text="", caption=None, entities=None, captionEntities=None):
    return SimpleNamespace(
        text=text,
        caption=caption,
        entities=entities or [],
        caption_entities=captionEntities or [],
    )


class TestIsMentionedByEntity:
    def test_exact_mention_hits(self):
        msg = _msg(text="@ZincNyaBot", entities=[_entity("mention", "@ZincNyaBot")])
        assert isMentionedByEntity(msg, "ZincNyaBot") is True

    def test_case_insensitive(self):
        msg = _msg(text="@zincnyabot", entities=[_entity("mention", "@zincnyabot")])
        assert isMentionedByEntity(msg, "ZincNyaBot") is True

    def test_prefix_bot_not_hit(self):
        """@notmybot 不命中——entity 精确匹配的关键差异"""
        msg = _msg(text="@ZincNyaBotOther", entities=[_entity("mention", "@ZincNyaBotOther")])
        assert isMentionedByEntity(msg, "ZincNyaBot") is False

    def test_no_entities_not_hit(self):
        msg = _msg(text="提到 @ZincNyaBot 但没有 entity")
        assert isMentionedByEntity(msg, "ZincNyaBot") is False

    def test_caption_entity_hits(self):
        msg = _msg(caption="@ZincNyaBot 看图", captionEntities=[_entity("mention", "@ZincNyaBot")])
        assert isMentionedByEntity(msg, "ZincNyaBot") is True

    def test_non_mention_entity_ignored(self):
        msg = _msg(text="@ZincNyaBot", entities=[_entity("bold", "@ZincNyaBot")])
        assert isMentionedByEntity(msg, "ZincNyaBot") is False


# ===========================================================================
# sendLLMReply（发送成功后写聊天历史——recordBotMessage 咽喉）
# ===========================================================================

class TestSendLLMReplyHistory:

    @patch("utils.core.logger.logAction", new_callable=AsyncMock)
    @patch("utils.telegramHelpers.recordBotMessage", new_callable=AsyncMock)
    async def test_success_records_history(self, mockRecord, mockLog):
        """发送成功 → recordBotMessage 恰一次（chatID 归一 str、记原始 Markdown reply）"""
        bot = AsyncMock()
        await sendLLMReply(bot=bot, chatID=123, reply="**回复**内容")

        bot.send_message.assert_awaited_once()
        mockRecord.assert_awaited_once_with("123", "**回复**内容")

    @patch("utils.core.logger.logAction", new_callable=AsyncMock)
    @patch("utils.telegramHelpers.recordBotMessage", new_callable=AsyncMock)
    async def test_degraded_resend_still_records(self, mockRecord, mockLog):
        """HTML 解析失败降级纯文本重发成功 → 仍恰写一次"""
        bot = AsyncMock()
        bot.send_message.side_effect = [BadRequest("Bad Request: can't parse entities"), None]

        await sendLLMReply(bot=bot, chatID="123", reply="坏格式 <b>回复")

        assert bot.send_message.await_count == 2
        mockRecord.assert_awaited_once_with("123", "坏格式 <b>回复")

    @patch("utils.core.logger.logAction", new_callable=AsyncMock)
    @patch("utils.telegramHelpers.recordBotMessage", new_callable=AsyncMock)
    async def test_other_badrequest_no_record(self, mockRecord, mockLog):
        """非解析类 BadRequest 上抛 → 不写历史"""
        bot = AsyncMock()
        bot.send_message.side_effect = BadRequest("Bad Request: chat not found")

        with pytest.raises(BadRequest):
            await sendLLMReply(bot=bot, chatID="123", reply="回复")

        mockRecord.assert_not_awaited()

    @patch("utils.core.logger.logAction", new_callable=AsyncMock)
    @patch("utils.telegramHelpers.recordBotMessage", new_callable=AsyncMock)
    async def test_networkerror_no_record(self, mockRecord, mockLog):
        """NetworkError 未被捕获直接上抛 → 不写历史"""
        bot = AsyncMock()
        bot.send_message.side_effect = NetworkError("connection reset")

        with pytest.raises(NetworkError):
            await sendLLMReply(bot=bot, chatID="123", reply="回复")

        mockRecord.assert_not_awaited()
