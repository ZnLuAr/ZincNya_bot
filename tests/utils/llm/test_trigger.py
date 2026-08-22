"""
tests/utils/llm/test_trigger.py

测试 utils/llm/trigger.py（群聊触发判断策略——原 handlers/llm.py 下沉）。
覆盖 shouldTriggerLLM（私聊恒触发 / entity 命中 / keyword 模式）与
matchesGroupTriggerKeyword（空文本 / 子串命中 / 空关键词列表）。

约定：
    - message / entity 用 SimpleNamespace 构造，避免 MagicMock truthy 陷阱
"""

from types import SimpleNamespace
from unittest.mock import patch

from utils.llm.trigger import matchesGroupTriggerKeyword, shouldTriggerLLM



# ===========================================================================
# 辅助构造
# ===========================================================================

def _entity(etype, text, offset=0, length=None):
    if length is None:
        length = len(text)
    return SimpleNamespace(type=etype, offset=offset, length=length)


def _mentionMessage(text=""):
    """构造 text 中带一个 mention entity 的消息（实体覆盖整段 text）"""
    return SimpleNamespace(
        text=text,
        caption=None,
        entities=[_entity("mention", text)],
        caption_entities=[],
    )



# ===========================================================================
# matchesGroupTriggerKeyword
# ===========================================================================

class TestMatchesGroupTriggerKeyword:
    @patch("utils.llm.trigger.getGroupTriggerKeywords", return_value=["锌酱", "看看"])
    def test_keyword_substring_hit(self, _mock):
        msg = SimpleNamespace(text="大家快来找锌酱玩", caption=None)
        assert matchesGroupTriggerKeyword(msg) is True

    @patch("utils.llm.trigger.getGroupTriggerKeywords", return_value=["锌酱"])
    def test_keyword_miss(self, _mock):
        msg = SimpleNamespace(text="今天天气不错", caption=None)
        assert matchesGroupTriggerKeyword(msg) is False

    @patch("utils.llm.trigger.getGroupTriggerKeywords", return_value=["锌酱"])
    def test_empty_text_returns_false(self, _mock):
        msg = SimpleNamespace(text="", caption=None)
        assert matchesGroupTriggerKeyword(msg) is False

    @patch("utils.llm.trigger.getGroupTriggerKeywords", return_value=[])
    def test_empty_keyword_list(self, _mock):
        msg = SimpleNamespace(text="随便说什么", caption=None)
        assert matchesGroupTriggerKeyword(msg) is False

    @patch("utils.llm.trigger.getGroupTriggerKeywords", return_value=["", "锌酱"])
    def test_blank_keyword_skipped(self, _mock):
        """空字符串关键词不应匹配一切文本"""
        msg = SimpleNamespace(text="无关文本", caption=None)
        assert matchesGroupTriggerKeyword(msg) is False

    @patch("utils.llm.trigger.getGroupTriggerKeywords", return_value=["锌酱"])
    def test_caption_fallback(self, _mock):
        msg = SimpleNamespace(text=None, caption="图片说明里有锌酱")
        assert matchesGroupTriggerKeyword(msg) is True



# ===========================================================================
# shouldTriggerLLM
# ===========================================================================

class TestShouldTriggerLLM:
    def test_private_always_triggers(self):
        msg = SimpleNamespace(text="", caption=None, entities=[], caption_entities=[])
        assert shouldTriggerLLM(msg, "ZincNyaBot", isPrivate=True) is True

    @patch("utils.llm.trigger.getGroupTriggerMode", return_value="mention")
    @patch("utils.llm.trigger.getGroupTriggerKeywords", return_value=["锌酱"])
    def test_mention_mode_requires_entity(self, _kwMock, _modeMock):
        """mention 模式：无 entity 命中即 False（关键词不生效）"""
        msg = SimpleNamespace(text="锌酱你好", caption=None, entities=[], caption_entities=[])
        assert shouldTriggerLLM(msg, "ZincNyaBot", isPrivate=False) is False

    @patch("utils.llm.trigger.getGroupTriggerMode", return_value="keyword")
    def test_keyword_mode_hit(self, _modeMock):
        with patch("utils.llm.trigger.getGroupTriggerKeywords", return_value=["锌酱"]):
            msg = SimpleNamespace(text="找锌酱", caption=None, entities=[], caption_entities=[])
            assert shouldTriggerLLM(msg, "ZincNyaBot", isPrivate=False) is True

    @patch("utils.llm.trigger.getGroupTriggerMode", return_value="keyword")
    @patch("utils.llm.trigger.getGroupTriggerKeywords", return_value=["锌酱"])
    def test_keyword_mode_miss(self, _kwMock, _modeMock):
        msg = SimpleNamespace(text="无关", caption=None, entities=[], caption_entities=[])
        assert shouldTriggerLLM(msg, "ZincNyaBot", isPrivate=False) is False

    def test_entity_mention_triggers(self):
        """@bot 实体精确命中即触发（不读配置）"""
        msg = _mentionMessage("@ZincNyaBot")
        assert shouldTriggerLLM(msg, "ZincNyaBot", isPrivate=False) is True

    def test_entity_mention_case_insensitive(self):
        msg = _mentionMessage("@zincnyabot")
        assert shouldTriggerLLM(msg, "ZincNyaBot", isPrivate=False) is True