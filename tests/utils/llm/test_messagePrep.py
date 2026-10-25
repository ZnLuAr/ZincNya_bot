"""验证消息提取保留身份、正文、完整引用及记忆轮次。"""

from types import SimpleNamespace
from unittest.mock import patch

from utils.llm.memory.types import MemoryTurn
from utils.llm.messagePrep import (
    extractPureMessage, getRawMessageText, getSenderDisplayName, prepareMessageInput,
)


def _user(username=None, firstName=None):
    return SimpleNamespace(id=42, username=username, first_name=firstName)


def _message(text=None, caption=None, fromUser=None, replyTo=None):
    return SimpleNamespace(
        chat_id=100, message_id=9, text=text, caption=caption,
        from_user=fromUser, reply_to_message=replyTo,
    )


class TestExtractPureMessage:
    @patch("utils.llm.messagePrep.getMemoryEnabled", return_value=False)
    def test_context_marker_at_start(self, _mock):
        """#context 在开头：命中标记、剥离后返回 True"""
        pure, include = extractPureMessage("#context 帮我记一下", "ZincNyaBot")
        assert pure == "帮我记一下"
        assert include is True

    @patch("utils.llm.messagePrep.getMemoryEnabled", return_value=False)
    def test_context_marker_not_at_start_not_matched(self, _mock):
        """#context 不在开头：不命中，跟随全局配置"""
        pure, include = extractPureMessage("记得 #context 这个", "ZincNyaBot")
        assert "#context" in pure
        assert include is False

    @patch("utils.llm.messagePrep.getMemoryEnabled", return_value=True)
    def test_no_marker_follows_global_config(self, _mock):
        """无标记：includeContext 跟随 getMemoryEnabled()"""
        pure, include = extractPureMessage("你好", "ZincNyaBot")
        assert pure == "你好"
        assert include is True



# ===========================================================================

class TestGetSenderDisplayName:
    def test_username_preferred(self):
        msg = _message(text="x", fromUser=_user("alice", "Alice"))
        assert getSenderDisplayName(msg, 42) == "alice"

    def test_fallback_first_name(self):
        msg = _message(text="x", fromUser=_user(None, "Alice"))
        assert getSenderDisplayName(msg, 42) == "Alice"

    def test_fallback_user_id(self):
        msg = _message(text="x", fromUser=_user(None, None))
        assert getSenderDisplayName(msg, 42) == "42"



class TestGetRawMessageText:
    def test_text_preferred(self):
        msg = _message(text="文本", caption="说明")
        assert getRawMessageText(msg) == "文本"

    def test_fallback_caption(self):
        msg = _message(text=None, caption="说明")
        assert getRawMessageText(msg) == "说明"

    def test_both_empty(self):
        msg = _message(text=None, caption=None)
        assert getRawMessageText(msg) == ""



class TestPrepareMessageInput:
    @patch("utils.llm.messagePrep.getMemoryEnabled", return_value=False)
    def test_preserves_full_reply_and_identity(self, _mock):
        reply = _message(caption="引" * 400, fromUser=_user("bob"))
        message = _message(text="@ZincNyaBot 当前", fromUser=_user("alice"), replyTo=reply)
        result = prepareMessageInput(message, message.text, "ZincNyaBot")
        assert result.origin.chatID == "100"
        assert result.origin.userID == 42
        assert result.origin.messageID == 9
        assert result.origin.sender == "alice"
        assert result.text == "当前"
        assert result.replyText == "引" * 400
        assert result.replySender == "bob"
        assert result.memoryTurn == MemoryTurn(
            currentText="当前", replyText="引" * 400,
            currentSender="@alice", replySender="@bob",
        )

    def test_empty_text_does_not_collect_reply_or_memory_turn(self):
        message = _message(replyTo=_message(text="引用"))
        result = prepareMessageInput(message, "@ZincNyaBot", "ZincNyaBot")
        assert result.text == result.replyText == ""
        assert result.memoryTurn is None

    def test_unknown_senders(self):
        message = _message(text="当前", replyTo=_message(text="引用"))
        result = prepareMessageInput(message, message.text, "ZincNyaBot")
        assert result.origin.sender == result.replySender == "未知用户"
        assert result.memoryTurn.currentSender == result.memoryTurn.replySender == "未知用户"

    def test_without_reply_keeps_current_memory_turn(self):
        message = _message(text="当前", fromUser=_user("alice"))
        result = prepareMessageInput(message, message.text, "ZincNyaBot")
        assert result.memoryTurn == MemoryTurn(currentText="当前", currentSender="@alice")
