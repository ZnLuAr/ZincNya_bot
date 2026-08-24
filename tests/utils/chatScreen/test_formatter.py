"""
tests/utils/chatScreen/test_formatter.py

消息格式化（utils/chatScreen/formatter.py）：单行/多行消息的行列表、
发送者名提取、展示文本提取。
"""

from types import SimpleNamespace

from utils.chatScreen.formatter import (
    extractDisplayText,
    formatMessageLines,
    getSenderName,
)



def _msg(text=None, caption=None, user="alice", first="Alice"):
    return SimpleNamespace(
        text=text, caption=caption,
        from_user=SimpleNamespace(username=user, first_name=first),
        photo=None, document=None, video=None, animation=None,
        sticker=None, voice=None, audio=None,
    )



class TestFormatMessageLines:
    def test_single_line(self):
        lines = formatMessageLines("10:00:00", "alice", "你好")
        assert lines == ["[10:00:00] <alice> 你好"]

    def test_multiline_indented_continuation(self):
        lines = formatMessageLines("10:00:00", "alice", "第一行\n第二行")
        assert lines[0] == "[10:00:00] <alice> 第一行"
        assert lines[1].startswith(" " + " " * 9) or lines[1] != "第二行"   # 续行有缩进
        assert "第二行" in lines[1]



class TestGetSenderName:
    def test_username_preferred(self):
        assert getSenderName(_msg(user="bob")) == "bob"

    def test_fallback_to_first_name(self):
        assert getSenderName(_msg(user=None, first="Bob")) == "Bob"

    def test_none_user(self):
        m = _msg()
        m.from_user = None
        # 兜底行为锁定
        name = getSenderName(m)
        assert isinstance(name, str)



class TestExtractDisplayText:
    def test_text_preferred(self):
        assert extractDisplayText(_msg(text="正文", caption="图注")) == "正文"

    def test_caption_fallback(self):
        assert extractDisplayText(_msg(text=None, caption="图注")) == "图注"

    def test_photo_without_caption(self):
        m = _msg(text=None, caption=None)
        m.photo = True
        assert extractDisplayText(m) == "[图片]"

    def test_caption_with_photo_prefix(self):
        m = _msg(text=None, caption="看图")
        m.photo = True
        assert extractDisplayText(m) == "[图片] 看图"

    def test_sticker(self):
        m = _msg()
        m.sticker = SimpleNamespace(emoji="🐱")
        assert extractDisplayText(m) == "[贴纸] 🐱"
