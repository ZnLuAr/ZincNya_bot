"""
tests/utils/llm/review/test_cards.py

测试 Telegram 审核卡的文字与键盘（utils/llm/review/cards.py）：
    - renderReviewCard：回复审核卡的文字、转义、截断与键盘
    - renderMemoryReviewCard：记忆操作审核卡的字段行、提示与键盘
    - displayBlocks：结构化展示块按消息配对渲染，空块退回原始文本
"""

from telegram import InlineKeyboardMarkup

from utils.llm.models import DisplayBlocks
from utils.llm.review.cards import renderMemoryReviewCard, renderReviewCard




# ===========================================================================
# 回复审核卡片渲染
# ===========================================================================

class TestRenderReviewCard:
    def test_returns_text_and_markup(self):
        text, markup = renderReviewCard("原始消息", "回复内容", 12345)
        assert isinstance(text, str)
        assert isinstance(markup, InlineKeyboardMarkup)

    def test_card_contains_label_and_content(self):
        text, _ = renderReviewCard("用户的提问", "锌酱的回答", 1)
        assert "待审核" in text
        assert "<b>" in text  # HTML 化：标题加粗
        assert "<blockquote>" in text  # 原始消息走 blockquote
        assert "用户的提问" in text
        assert "锌酱的回答" in text

    def test_card_contains_edit_and_fb_hints(self):
        """同时含 :edit 与 :fb 提示；:fb 带空格（与 startswith(':fb ') 对齐）"""
        text, _ = renderReviewCard("msg", "reply", 1)
        assert ":edit" in text
        assert ":fb " in text

    def test_suffix_appended(self):
        text, _ = renderReviewCard("msg", "reply", 1, suffix="\n\n⚠️ 警告")
        assert text.endswith("⚠️ 警告")

    def test_overlong_reply_within_tg_limit(self):
        text, _ = renderReviewCard("msg", "x" * 10000, 1)
        assert len(text) <= 4096

    def test_original_msg_merges_into_single_blockquote_when_reply_to(self):
        """originalMsg 含引用与当前消息标记时，合并为一个双小节 blockquote。"""
        originalMsg = (
            "[引用的消息]\n<@zincnya_test_bot> 原引用内容\n\n"
            "[当前用户消息]\n<@ZincPhos> 当前用户说的"
        )
        text, _ = renderReviewCard(originalMsg, "回复", 1)
        assert text.count("<blockquote>") == 1  # 引用与当前同块（宽度不齐问题）
        assert "<b>引用消息</b>" in text
        assert "<b>当前消息</b>" in text
        assert "原引用内容" in text
        assert "当前用户说的" in text

    def test_original_msg_single_blockquote_when_plain(self):
        """普通消息（无引用标记）→ 单个 blockquote"""
        text, _ = renderReviewCard("普通用户消息", "回复", 1)
        assert text.count("<blockquote>") == 1

    def test_dynamic_content_escaped(self):
        """originalMsg/reply 含 < >& → 转义为 HTML 实体，防 BotBadRequest"""
        text, _ = renderReviewCard("<script>x</script>", "a & b < c", 1)
        assert "<script>" not in text  # 原始 < 已转义
        assert "&lt;script&gt;" in text
        assert "&amp;" in text  # & 转义

    def test_keyboard_callback_data_is_4_segments(self):
        """callback_data 4 段 llm:review:{action}:{chatID}（msgID 不编码进 data）"""
        _, markup = renderReviewCard("msg", "reply", 999)
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
        text, markup = renderMemoryReviewCard(self._addAction(), "触发消息", 123)
        assert isinstance(text, str)
        assert isinstance(markup, InlineKeyboardMarkup)

    def test_memory_card_contains_fields(self):
        text, _ = renderMemoryReviewCard(self._addAction(), "触发", 1)
        assert "ADD" in text
        assert "global" in text
        assert "记住这件事" in text
        assert "优先级</b>：0" in text

    def test_update_card_shows_current_and_proposed_values(self):
        action = {
            "action": "update",
            "scopeType": "global",
            "scopeID": "global",
            "memoryID": 9,
            "originalContent": "当前事实",
            "content": "拟议事实",
            "originalMode": "contextual",
            "mode": "pinned",
        }

        text, _ = renderMemoryReviewCard(action, "触发", 1)

        assert "当前内容</b>：当前事实" in text
        assert "新内容</b>：拟议事实" in text
        assert "当前模式</b>：contextual" in text
        assert "新模式</b>：pinned" in text

    def test_memory_card_has_edit_but_no_fb(self):
        """记忆卡片只有 :edit 提示，无 :fb（memory 不支持 :fb）"""
        text, _ = renderMemoryReviewCard(self._addAction(), "触发", 1)
        assert ":edit" in text
        assert ":fb" not in text

    def test_suffix_appended(self):
        text, _ = renderMemoryReviewCard(self._addAction(), "触发", 1, suffix="\n\n尾注")
        assert text.endswith("尾注")

    def test_keyboard_callback_data_is_4_segments(self):
        """记忆 callback_data 4 段 llm:memreview:{action}:{chatID}"""
        _, markup = renderMemoryReviewCard(self._addAction(), "触发", 888)
        for row in markup.inline_keyboard:
            for btn in row:
                parts = btn.callback_data.split(":")
                assert len(parts) == 4
                assert parts[0] == "llm"
                assert parts[1] == "memreview"
                assert parts[3] == "888"




# ===========================================================================
# displayBlocks 结构化渲染（数据源拆分——引用/当前配对 + 小标题）
# ===========================================================================

def _blocks(prefix="", pairs=None):
    return DisplayBlocks(prefix=prefix, pairs=pairs or [])


class TestDisplayBlocksRendering:
    def test_structured_single_pair_merged_block(self):
        """单消息带引用：合并为一个双小节 blockquote（宽度不齐问题的解法）"""
        blocks = _blocks(pairs=[{"replyLine": "<@rep> 被引用的话", "currentText": "当前说的"}])
        text, _ = renderReviewCard("原始", "回复", 1, displayBlocks=blocks)

        assert text.count("<blockquote>") == 1
        assert "<b>引用消息</b>" in text
        assert "<b>当前消息</b>" in text
        assert "被引用的话" in text
        assert "当前说的" in text

    def test_structured_multi_pair_pairing_preserved(self):
        """多消息批次：每条消息一个块、块内引用/当前配对（旧 partition 只切第一个的 bug 修复）"""
        blocks = _blocks(pairs=[
            {"replyLine": "<@rep> 引用一", "currentText": "消息一"},
            {"replyLine": "", "currentText": "消息二"},
            {"replyLine": "<@rep> 引用三", "currentText": "消息三"},
        ])
        text, _ = renderReviewCard("原始", "回复", 1, displayBlocks=blocks)

        assert text.count("<blockquote>") == 3          # 每条消息一块
        assert text.count("<b>引用消息</b>") == 2
        assert text.count("<b>当前消息</b>") == 3

    def test_structured_no_reply_single_block_with_heading(self):
        """无引用消息：单个当前块（带小节标题）"""
        blocks = _blocks(pairs=[{"replyLine": "", "currentText": "纯消息"}])
        text, _ = renderReviewCard("原始", "回复", 1, displayBlocks=blocks)

        assert text.count("<blockquote>") == 1
        assert "<b>引用</b>" not in text
        assert "<b>当前消息</b>" in text

    def test_structured_prefix_line(self):
        """prefix（图片标注/URL 摘要）渲染为普通文本行（不加粗，视觉弱化）"""
        blocks = _blocks(prefix="[附带 2 张图片]", pairs=[{"replyLine": "", "currentText": "看图"}])
        text, _ = renderReviewCard("原始", "回复", 1, displayBlocks=blocks)

        assert "[附带 2 张图片]" in text
        assert "<b>[附带 2 张图片]</b>" not in text

    def test_structured_dynamic_escaped(self):
        """结构化路径同样转义动态内容（截断→转义→拼铁律）"""
        blocks = _blocks(pairs=[{"replyLine": "<@rep> <script>alert(1)</script>", "currentText": "a & b < c"}])
        text, _ = renderReviewCard("原始", "回复", 1, displayBlocks=blocks)

        assert "<script>" not in text
        assert "&lt;script&gt;" in text
        assert "&amp;" in text

    def test_structured_empty_pairs_falls_back_to_original(self):
        """pairs 为空：回退原始字符串单 blockquote"""
        blocks = _blocks(pairs=[])
        text, _ = renderReviewCard("原始消息", "回复", 1, displayBlocks=blocks)

        assert text.count("<blockquote>") == 1
        assert "原始消息" in text

    def test_memory_card_uses_structured_blocks(self):
        """记忆审核卡同样走结构化合并渲染"""
        blocks = _blocks(pairs=[{"replyLine": "<@rep> 引用", "currentText": "触发记忆的消息"}])
        action = {"action": "add", "scopeType": "global", "scopeID": "global", "content": "内容"}
        text, _ = renderMemoryReviewCard(action, "原始", 1, displayBlocks=blocks)

        assert text.count("<blockquote>") == 1
        assert "<b>引用消息</b>" in text
        assert "触发记忆的消息" in text
