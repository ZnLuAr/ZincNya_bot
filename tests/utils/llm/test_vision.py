"""
tests/utils/llm/test_vision.py

测试 utils/llm/vision.py 的 extractImageRefsForPrompt（先本消息后 reply 回退——
原 handlers/llm.py 的 _extractImageRefsForLLM 下沉）。
底层 extractImageRefs / extractReplyImageRefs 用 patch 隔离，只测回退策略本身。
"""

from types import SimpleNamespace
from unittest.mock import patch

from utils.llm.vision import extractImageRefsForPrompt




class TestExtractImageRefsForPrompt:
    @patch("utils.llm.vision.extractImageRefs", return_value=["ref_current"])
    @patch("utils.llm.vision.extractReplyImageRefs", return_value=["ref_reply"])
    def test_current_message_has_images_no_fallback(self, replyMock, curMock):
        """当前消息有图：不回退 reply"""
        msg = SimpleNamespace(photo=object(), reply_to_message=object())

        refs = extractImageRefsForPrompt(msg)

        assert refs == ["ref_current"]
        curMock.assert_called_once_with(msg)
        replyMock.assert_not_called()

    @patch("utils.llm.vision.extractImageRefs", return_value=[])
    @patch("utils.llm.vision.extractReplyImageRefs", return_value=["ref_reply"])
    def test_no_current_images_falls_back_to_reply(self, replyMock, curMock):
        """当前消息无图：回退 reply_to_message"""
        msg = SimpleNamespace(photo=None, reply_to_message=object())

        refs = extractImageRefsForPrompt(msg)

        assert refs == ["ref_reply"]
        replyMock.assert_called_once_with(msg)

    @patch("utils.llm.vision.extractImageRefs", return_value=[])
    @patch("utils.llm.vision.extractReplyImageRefs", return_value=[])
    def test_both_empty_returns_empty(self, replyMock, curMock):
        """两处皆无图：返回空列表"""
        msg = SimpleNamespace(photo=None, reply_to_message=None)

        refs = extractImageRefsForPrompt(msg)

        assert refs == []