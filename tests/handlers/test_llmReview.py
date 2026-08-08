"""
tests/handlers/test_llmReview.py

测试 Telegram 端 LLM 审核卡片渲染（handlers/llmReview.py）——覆盖「callback_data 5 段→4 段 +
render 合并」的护栏：

    - _renderReviewCard / _renderMemoryReviewCard：(text, markup) 拼接 + suffix
    - callback_data 4 段格式（msgID 不再编码进 data，由 query.message.message_id 取）
    - :edit / :fb 提示文案（:fb 带空格，与 handleFeedbackRetry 的 startswith(':fb ') 对齐）

约定：
    - 只测纯渲染函数，不驱动 PTB handler（Update/Context 交互脆弱）

_injectReplyTextContext 的测试在 tests/handlers/test_llm.py（函数定义在 handlers/llm.py）。
"""

from telegram import InlineKeyboardMarkup

from handlers.llmReview import (
    _renderMemoryReviewCard,
    _renderReviewCard,
)




# ===========================================================================
# 回复审核卡片渲染
# ===========================================================================

class TestRenderReviewCard:
    def test_returns_text_and_markup(self):
        text, markup = _renderReviewCard("原始消息", "回复内容", 12345)
        assert isinstance(text, str)
        assert isinstance(markup, InlineKeyboardMarkup)

    def test_card_contains_label_and_content(self):
        text, _ = _renderReviewCard("用户的提问", "锌酱的回答", 1)
        assert "待审核" in text
        assert "<b>" in text  # HTML 化：标题加粗
        assert "<blockquote>" in text  # 原始消息走 blockquote
        assert "用户的提问" in text
        assert "锌酱的回答" in text

    def test_card_contains_edit_and_fb_hints(self):
        """同时含 :edit 与 :fb 提示；:fb 带空格（与 startswith(':fb ') 对齐）"""
        text, _ = _renderReviewCard("msg", "reply", 1)
        assert ":edit" in text
        assert ":fb " in text

    def test_suffix_appended(self):
        text, _ = _renderReviewCard("msg", "reply", 1, suffix="\n\n⚠️ 警告")
        assert text.endswith("⚠️ 警告")

    def test_overlong_reply_within_tg_limit(self):
        text, _ = _renderReviewCard("msg", "x" * 10000, 1)
        assert len(text) <= 4096

    def test_original_msg_splits_into_two_blockquotes_when_reply_to(self):
        """originalMsg 含 [引用的消息]/[当前用户消息] 标记（_injectReplyTextContext 输出）→ 拆两个 blockquote"""
        originalMsg = (
            "[引用的消息]\n<@zincnya_test_bot> 原引用内容\n\n"
            "[当前用户消息]\n<@ZincPhos> 当前用户说的"
        )
        text, _ = _renderReviewCard(originalMsg, "回复", 1)
        assert text.count("<blockquote>") == 2  # 引用 + 当前各一个
        assert "原引用内容" in text
        assert "当前用户说的" in text

    def test_original_msg_single_blockquote_when_plain(self):
        """普通消息（无引用标记）→ 单个 blockquote"""
        text, _ = _renderReviewCard("普通用户消息", "回复", 1)
        assert text.count("<blockquote>") == 1

    def test_dynamic_content_escaped(self):
        """originalMsg/reply 含 < >& → 转义为 HTML 实体，防 BotBadRequest"""
        text, _ = _renderReviewCard("<script>x</script>", "a & b < c", 1)
        assert "<script>" not in text  # 原始 < 已转义
        assert "&lt;script&gt;" in text
        assert "&amp;" in text  # & 转义

    def test_keyboard_callback_data_is_4_segments(self):
        """callback_data 4 段 llm:review:{action}:{chatID}（msgID 不编码进 data）"""
        _, markup = _renderReviewCard("msg", "reply", 999)
        for row in markup.inline_keyboard:
            for btn in row:
                parts = btn.callback_data.split(":")
                assert len(parts) == 4
                assert parts[0] == "llm"
                assert parts[1] == "review"
                assert parts[3] == "999"




# ===========================================================================
# 记忆审核卡片渲染
# ===========================================================================

class TestRenderMemoryReviewCard:
    def _addAction(self):
        return {
            "action": "add",
            "scopeType": "global",
            "scopeID": "global",
            "content": "记住这件事",
            "tags": [],
            "priority": 0,
        }

    def test_returns_text_and_markup(self):
        text, markup = _renderMemoryReviewCard(self._addAction(), "触发消息", 123)
        assert isinstance(text, str)
        assert isinstance(markup, InlineKeyboardMarkup)

    def test_memory_card_contains_fields(self):
        text, _ = _renderMemoryReviewCard(self._addAction(), "触发", 1)
        assert "ADD" in text
        assert "global" in text
        assert "记住这件事" in text

    def test_memory_card_has_edit_but_no_fb(self):
        """记忆卡片只有 :edit 提示，无 :fb（memory 不支持 :fb）"""
        text, _ = _renderMemoryReviewCard(self._addAction(), "触发", 1)
        assert ":edit" in text
        assert ":fb" not in text

    def test_suffix_appended(self):
        text, _ = _renderMemoryReviewCard(self._addAction(), "触发", 1, suffix="\n\n尾注")
        assert text.endswith("尾注")

    def test_keyboard_callback_data_is_4_segments(self):
        """记忆 callback_data 4 段 llm:memreview:{action}:{chatID}"""
        _, markup = _renderMemoryReviewCard(self._addAction(), "触发", 888)
        for row in markup.inline_keyboard:
            for btn in row:
                parts = btn.callback_data.split(":")
                assert len(parts) == 4
                assert parts[0] == "llm"
                assert parts[1] == "memreview"
                assert parts[3] == "888"