"""
tests/handlers/test_llm.py

测试 handlers/llm.py 的纯函数（不驱动 PTB handler）。
当前覆盖：_injectReplyTextContext——reply 上下文注入 prompt（方案 A：当前用户消息对称带 <@sender> 前缀）。

约定：
    - message 用 types.SimpleNamespace 构造，避免 MagicMock 属性 truthy 陷阱
"""

from types import SimpleNamespace

from handlers.llm import _injectReplyTextContext


# ===========================================================================
# 辅助：构造 SimpleNamespace 假 message / user
# ===========================================================================

def _user(username=None, firstName=None):
    return SimpleNamespace(username=username, first_name=firstName)


def _message(text=None, caption=None, fromUser=None, replyTo=None):
    return SimpleNamespace(
        text=text,
        caption=caption,
        from_user=fromUser,
        reply_to_message=replyTo,
    )




# ===========================================================================
# _injectReplyTextContext（方案 A：当前用户消息对称带 sender 前缀）
# ===========================================================================

class TestInjectReplyTextContext:
    def test_no_reply_returns_pure_text(self):
        msg = _message(text="你好", fromUser=_user("cur"))
        assert _injectReplyTextContext(msg, "你好") == "你好"

    def test_reply_without_text_returns_pure_text(self):
        replyMsg = _message(text=None, caption=None, fromUser=_user("rep"))
        msg = _message(text="你好", fromUser=_user("cur"), replyTo=replyMsg)
        assert _injectReplyTextContext(msg, "你好") == "你好"

    def test_format_symmetric_sender_prefix(self):
        """引用与当前消息都带 <@sender> 前缀，标记为 [引用的消息]/[当前用户消息]"""
        replyMsg = _message(text="被引用的话", fromUser=_user("someone"))
        msg = _message(text="当前的话", fromUser=_user("curuser"), replyTo=replyMsg)

        result = _injectReplyTextContext(msg, "当前的话")

        assert "[引用的消息]" in result
        assert "<@someone> 被引用的话" in result
        assert "[当前用户消息]" in result
        assert "<@curuser> 当前的话" in result

    def test_sender_falls_back_to_first_name(self):
        replyMsg = _message(text="引用", fromUser=_user(None, "张三"))
        msg = _message(text="当前", fromUser=_user("cur"), replyTo=replyMsg)

        result = _injectReplyTextContext(msg, "当前")

        assert "<@张三> 引用" in result

    def test_reply_sender_none_falls_back_unknown(self):
        """reply from_user=None（匿名群/频道）：sender 兜底为 未知用户"""
        replyMsg = _message(text="引用", fromUser=None)
        msg = _message(text="当前", fromUser=_user("cur"), replyTo=replyMsg)

        result = _injectReplyTextContext(msg, "当前")

        assert "<未知用户> 引用" in result

    def test_current_sender_none_falls_back_unknown(self):
        """当前 from_user=None：sender 兜底为 未知用户"""
        replyMsg = _message(text="引用", fromUser=_user("rep"))
        msg = _message(text="当前", fromUser=None, replyTo=replyMsg)

        result = _injectReplyTextContext(msg, "当前")

        assert "[当前用户消息]\n<未知用户> 当前" in result

    def test_long_reply_truncated(self):
        replyMsg = _message(text="字" * 400, fromUser=_user("rep"))
        msg = _message(text="当前", fromUser=_user("cur"), replyTo=replyMsg)

        result = _injectReplyTextContext(msg, "当前")

        assert ("字" * 300 + "……") in result
        assert ("字" * 301) not in result
