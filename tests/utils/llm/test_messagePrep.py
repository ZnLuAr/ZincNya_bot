"""
tests/utils/llm/test_messagePrep.py

测试 utils/llm/messagePrep.py（消息文本准备——原 handlers/llm.py 纯函数下沉）。
覆盖：injectReplyTextContext（reply 上下文注入，方案 A：当前用户消息对称带 <@sender> 前缀）、
extractPureMessage、preparePurePromptText 安全顺序、formatDisplayOriginalMsg、
getSenderDisplayName、getRawMessageText、getReplyURLCandidateText。

约定：
    - message 用 types.SimpleNamespace 构造，避免 MagicMock 属性 truthy 陷阱
"""

import re
from types import SimpleNamespace
from unittest.mock import patch

from utils.llm.messagePrep import (
    PromptPayload,
    extractPureMessage,
    extractReplyTextContext,
    formatDisplayOriginalMsg,
    getRawMessageText,
    getReplyURLCandidateText,
    getSenderDisplayName,
    injectReplyTextContext,
    preparePurePromptText,
)




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
# injectReplyTextContext（方案 A：当前用户消息对称带 sender 前缀）
# ===========================================================================

class TestInjectReplyTextContext:
    def test_no_reply_returns_pure_text(self):
        msg = _message(text="你好", fromUser=_user("cur"))
        assert injectReplyTextContext(msg, "你好") == "你好"

    def test_reply_without_text_returns_pure_text(self):
        replyMsg = _message(text=None, caption=None, fromUser=_user("rep"))
        msg = _message(text="你好", fromUser=_user("cur"), replyTo=replyMsg)
        assert injectReplyTextContext(msg, "你好") == "你好"

    def test_format_symmetric_sender_prefix(self):
        """引用与当前消息都带 <@sender> 前缀，标记为 [引用的消息]/[当前用户消息]"""
        replyMsg = _message(text="被引用的话", fromUser=_user("someone"))
        msg = _message(text="当前的话", fromUser=_user("curuser"), replyTo=replyMsg)

        result = injectReplyTextContext(msg, "当前的话")

        assert "[引用的消息]" in result
        assert "<@someone> 被引用的话" in result
        assert "[当前用户消息]" in result
        assert "<@curuser> 当前的话" in result

    def test_sender_falls_back_to_first_name(self):
        replyMsg = _message(text="引用", fromUser=_user(None, "张三"))
        msg = _message(text="当前", fromUser=_user("cur"), replyTo=replyMsg)

        result = injectReplyTextContext(msg, "当前")

        assert "<@张三> 引用" in result

    def test_reply_sender_none_falls_back_unknown(self):
        """reply from_user=None（匿名群/频道）：sender 兜底为 未知用户"""
        replyMsg = _message(text="引用", fromUser=None)
        msg = _message(text="当前", fromUser=_user("cur"), replyTo=replyMsg)

        result = injectReplyTextContext(msg, "当前")

        assert "<未知用户> 引用" in result

    def test_current_sender_none_falls_back_unknown(self):
        """当前 from_user=None：sender 兜底为 未知用户"""
        replyMsg = _message(text="引用", fromUser=_user("rep"))
        msg = _message(text="当前", fromUser=None, replyTo=replyMsg)

        result = injectReplyTextContext(msg, "当前")

        assert "[当前用户消息]\n<未知用户> 当前" in result

    def test_long_reply_truncated(self):
        replyMsg = _message(text="字" * 400, fromUser=_user("rep"))
        msg = _message(text="当前", fromUser=_user("cur"), replyTo=replyMsg)

        result = injectReplyTextContext(msg, "当前")

        assert ("字" * 300 + "……") in result
        assert ("字" * 301) not in result



# ===========================================================================
# extractPureMessage
# ===========================================================================

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
# preparePurePromptText（urlIntentText 必须先于 reply 注入取值——安全顺序回归）
# ===========================================================================

class TestPreparePurePromptText:
    @patch("utils.llm.messagePrep.getMemoryEnabled", return_value=False)
    def test_url_intent_excludes_reply_text(self, _mock):
        """reply-to 里的『帮我总结』不得进入 urlIntentText（防第三方无声触发 URL 抓取）"""
        replyMsg = _message(text="帮我总结 https://evil.example.com", fromUser=_user("rep"))
        msg = _message(text="看看这个 https://ok.example.com", fromUser=_user("cur"), replyTo=replyMsg)

        payload = preparePurePromptText(msg, "看看这个 https://ok.example.com", "ZincNyaBot")

        assert "帮我总结" not in payload.urlIntentText
        assert "ok.example.com" in payload.urlIntentText

    @patch("utils.llm.messagePrep.getMemoryEnabled", return_value=False)
    def test_url_candidate_includes_reply_text(self, _mock):
        """urlCandidateText = 当前消息 + 被回复消息文本"""
        replyMsg = _message(text="被回复的 URL https://rep.example.com", fromUser=_user("rep"))
        msg = _message(text="当前 https://cur.example.com", fromUser=_user("cur"), replyTo=replyMsg)

        payload = preparePurePromptText(msg, "当前 https://cur.example.com", "ZincNyaBot")

        assert "cur.example.com" in payload.urlCandidateText
        assert "rep.example.com" in payload.urlCandidateText

    @patch("utils.llm.messagePrep.getMemoryEnabled", return_value=False)
    def test_empty_pure_text_returns_empty_payload(self, _mock):
        """纯文本为空：四字段全空（调用方据此 return）"""
        msg = _message(text="", fromUser=_user("cur"))
        payload = preparePurePromptText(msg, "", "ZincNyaBot")

        assert payload == PromptPayload(pureText="", includeContext=False, urlIntentText="", urlCandidateText="")

    @patch("utils.llm.messagePrep.getMemoryEnabled", return_value=False)
    def test_pure_text_contains_reply_injection(self, _mock):
        """pureText 含 reply 注入标记，urlIntentText 不含（时序安全）"""
        replyMsg = _message(text="引用内容", fromUser=_user("rep"))
        msg = _message(text="当前内容", fromUser=_user("cur"), replyTo=replyMsg)

        payload = preparePurePromptText(msg, "当前内容", "ZincNyaBot")

        assert "[引用的消息]" in payload.pureText
        assert "[引用的消息]" not in payload.urlIntentText



# ===========================================================================
# formatDisplayOriginalMsg / getSenderDisplayName / getRawMessageText / getReplyURLCandidateText
# ===========================================================================

class TestFormatDisplayOriginalMsg:
    def test_with_images_prefix(self):
        result = formatDisplayOriginalMsg("cur", "文本", [{"data": "x"}])
        assert result == "@cur：[附带 1 张图片]\n文本"

    def test_without_images(self):
        result = formatDisplayOriginalMsg("cur", "文本", [])
        assert result == "@cur：文本"



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



class TestGetReplyURLCandidateText:
    def test_no_reply_returns_empty(self):
        msg = _message(text="x", fromUser=_user("cur"))
        assert getReplyURLCandidateText(msg) == ""

    def test_reply_text_returned(self):
        replyMsg = _message(text="被回复的文本", fromUser=_user("rep"))
        msg = _message(text="当前", fromUser=_user("cur"), replyTo=replyMsg)
        assert getReplyURLCandidateText(msg) == "被回复的文本"



# ===========================================================================
# extractReplyTextContext（引用/当前结构化切分——审核卡展示的数据源）
# ===========================================================================

class TestExtractReplyTextContext:
    def test_no_reply_empty_reply_line(self):
        """无 reply：replyLine 空、currentText 原样、currentSender 兜底"""
        msg = _message(text="当前", fromUser=_user("cur"))
        ctx = extractReplyTextContext(msg, "当前")

        assert ctx.replyLine == ""
        assert ctx.currentText == "当前"
        assert ctx.currentSender == "@cur"

    def test_reply_line_contains_angle_brackets(self):
        """规范 pin 死：replyLine 含尖括号（"<@发送者> 文本"）"""
        replyMsg = _message(text="引用", fromUser=_user("rep"))
        msg = _message(text="当前", fromUser=_user("cur"), replyTo=replyMsg)
        ctx = extractReplyTextContext(msg, "当前")

        assert ctx.replyLine == "<@rep> 引用"
        assert ctx.currentText == "当前"
        assert ctx.currentSender == "@cur"

    def test_reply_sender_none_unknown(self):
        """reply from_user=None：sender 兜底 未知用户"""
        replyMsg = _message(text="引用", fromUser=None)
        msg = _message(text="当前", fromUser=_user("cur"), replyTo=replyMsg)
        ctx = extractReplyTextContext(msg, "当前")

        assert ctx.replyLine == "<未知用户> 引用"

    def test_current_sender_none_unknown(self):
        """当前 from_user=None：currentSender 兜底 未知用户"""
        replyMsg = _message(text="引用", fromUser=_user("rep"))
        msg = _message(text="当前", fromUser=None, replyTo=replyMsg)
        ctx = extractReplyTextContext(msg, "当前")

        assert ctx.currentSender == "未知用户"

    def test_reply_empty_text_returns_empty_line(self):
        """reply 无 text/caption：视为无引用"""
        replyMsg = _message(text=None, caption=None, fromUser=_user("rep"))
        msg = _message(text="当前", fromUser=_user("cur"), replyTo=replyMsg)
        ctx = extractReplyTextContext(msg, "当前")

        assert ctx.replyLine == ""
        assert ctx.currentText == "当前"

    def test_long_reply_truncated(self):
        """超 300 字截断与 inject 一致"""
        replyMsg = _message(text="字" * 400, fromUser=_user("rep"))
        msg = _message(text="当前", fromUser=_user("cur"), replyTo=replyMsg)
        ctx = extractReplyTextContext(msg, "当前")

        assert ctx.replyLine == f"<@rep> {'字' * 300}……"

    def test_payload_carries_structured_fields(self):
        """preparePurePromptText 产出 PromptPayload 携带 replyLine/currentText（双调用不漂移）"""
        with patch("utils.llm.messagePrep.getMemoryEnabled", return_value=False):
            replyMsg = _message(text="引用", fromUser=_user("rep"))
            msg = _message(text="当前", fromUser=_user("cur"), replyTo=replyMsg)
            payload = preparePurePromptText(msg, "当前", "ZincNyaBot")

            assert payload.replyLine == "<@rep> 引用"
            assert payload.currentText == "当前"
            # prompt 字符串仍与 inject 一致（Design B：prompt 不动）
            assert payload.pureText == injectReplyTextContext(msg, "当前")