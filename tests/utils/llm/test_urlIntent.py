"""
tests/utils/llm/test_urlIntent.py

URL 读取意图判定（utils/llm/urlIntent.py）——这是「裸链接不触发抓取」安全边界的
判定核心，四层逻辑（显式 marker → 全局抑制 → 意图信号 → 近邻否定）各自的
判定顺序与边界由本文件锁定。

安全上下文：reply-to 消息的文本不进 intentText（handler 侧保证），本文件只测
intentText 本身的判定；伪造场景（抑制词混入/否定词绕过）是这里的重点用例。
"""

import pytest

from utils.llm.urlIntent import hasURLReadIntent



class TestExplicitMarker:
    def test_url_marker(self):
        assert hasURLReadIntent("https://example.com #url") is True

    def test_readurl_marker(self):
        assert hasURLReadIntent("https://example.com #readurl") is True

    def test_marker_beats_suppress(self):
        """显式 marker 优先级最高，压过全局抑制"""
        assert hasURLReadIntent("只是分享 #url") is True

    def test_marker_partial_word_not_matched(self):
        """#urlxyz 不是 marker（子串要求 '#url' 后无边界约束——按实现是子串匹配，锁定现状）"""
        # 实现是 "#url" in textLower，因此 #urlxyz 也命中——这是现状，锁定防止无意识变更
        assert hasURLReadIntent("#urlxyz") is True



class TestGlobalSuppress:
    def test_zh_sharing_only(self):
        assert hasURLReadIntent("只是分享 https://example.com 总结一下") is False

    def test_zh_no_need_reply(self):
        assert hasURLReadIntent("https://example.com 不用回复 帮我总结") is False

    def test_en_fyi(self):
        assert hasURLReadIntent("just sharing https://example.com summarize") is False

    def test_en_nevermind(self):
        assert hasURLReadIntent("https://example.com nvm read this") is False

    def test_suppress_beats_intent(self):
        """抑制在意图信号之前判定"""
        assert hasURLReadIntent("帮我读一下 忽略") is False



class TestIntentSignal:
    def test_zh_keyword(self):
        assert hasURLReadIntent("https://example.com 帮我总结一下") is True

    def test_zh_tldr_phrase(self):
        assert hasURLReadIntent("https://example.com 太长不看") is True

    def test_en_keyword(self):
        assert hasURLReadIntent("https://example.com summarize this") is True

    def test_en_tl_dr(self):
        assert hasURLReadIntent("https://example.com tl;dr") is True

    def test_zh_pattern_this_article_says_what(self):
        """「这篇文章讲了什么」句式（正则模板，不在词表）"""
        assert hasURLReadIntent("https://example.com 这篇文章讲了什么") is True

    def test_en_pattern_can_you_read(self):
        assert hasURLReadIntent("https://example.com can you read it") is True

    def test_en_pattern_what_does_it_say(self):
        assert hasURLReadIntent("https://example.com what does it say") is True

    def test_no_intent_bare_url(self):
        """裸链接无任何意图词 → False（安全边界的根基）"""
        assert hasURLReadIntent("https://example.com") is False

    def test_no_intent_plain_text(self):
        assert hasURLReadIntent("今天天气不错") is False

    def test_en_word_boundary(self):
        """'read' 不命中 'already'（word-boundary regex 的存在理由）"""
        assert hasURLReadIntent("https://example.com already done") is False

    def test_en_substring_in_longer_word(self):
        assert hasURLReadIntent("https://example.com breadth first search") is False



class TestNegationNearIntent:
    def test_zh_negation_before_intent(self):
        """否定词紧跟意图词 → 拦截"""
        assert hasURLReadIntent("https://example.com 不要总结") is False

    def test_zh_negation_far_from_intent(self):
        """否定词与意图词距离超过 window → 不拦截（否定窗口的意义）"""
        # window 默认值从 config 读取；构造远距离文本（中间隔长句）
        filler = "这一段完全无关的填充文字说得特别长特别啰嗦特别冗余" * 3
        assert hasURLReadIntent(f"不要{filler}https://example.com 总结一下") is True

    def test_zh_negation_then_other_intent(self):
        """「不要只总结，帮我分析」——否定只拦总结，分析仍成立"""
        assert hasURLReadIntent("https://example.com 不要只总结，帮我分析一下") is True

    def test_en_negation_before_intent(self):
        assert hasURLReadIntent("https://example.com don't read this") is False

    def test_en_negation_far(self):
        filler = "this is a long unrelated filler sentence about nothing at all " * 3
        assert hasURLReadIntent(f"don't {filler} https://example.com summarize") is True



class TestNormalization:
    def test_fullwidth_to_halfwidth(self):
        """NFKC：全角拉丁/符号归一化后命中英文意图词"""
        assert hasURLReadIntent("https://example.com ｓｕｍｍａｒｉｚｅ") is True

    def test_fullwidth_hash_marker(self):
        """全角 ＃ｕｒｌ 归一化成 #url 后命中显式 marker"""
        assert hasURLReadIntent("ｈｔｔｐｓ://example.com ＃ｕｒｌ") is True

    def test_empty_text(self):
        assert hasURLReadIntent("") is False

