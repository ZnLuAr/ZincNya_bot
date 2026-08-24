"""
tests/utils/chatScreen/test_statusBar.py

状态栏文案锁（utils/chatScreen/statusBar.py）——集中管理的意义就是文案
不再漂移；本文件把四个函数的实际输出钉住。这轮文档审计抓到的「状态栏
文案三处虚构」正是没有这层锁的后果。
"""

from utils.chatScreen.statusBar import (
    getDefaultStatus,
    getEditModeStatus,
    getHistoryBrowsingStatus,
    getReviewQueueStatus,
)



class TestGetDefaultStatus:
    def test_contains_all_shortcuts_and_chat_id(self):
        s = getDefaultStatus("-100123456")
        assert "Ctrl+S" in s and "Esc" in s and "Ctrl+X" in s
        assert "Alt+↑↓" in s and "Alt+←→" in s
        assert "-100123456" in s

    def test_no_leading_trailing_space_shape(self):
        s = getDefaultStatus("x")
        assert s == s.strip()     # 现状无首尾空格（与审核/编辑态不同）



class TestGetReviewQueueStatus:
    def test_with_hint(self):
        s = getReviewQueueStatus(3, "当前操作的是：[回复] 你好…")
        assert "待审核: (3 条)" in s
        assert ":ra" in s and ":re" in s and ":rr" in s and ":rc" in s
        assert "当前操作的是" in s

    def test_without_hint(self):
        s = getReviewQueueStatus(0)
        assert "待审核: (0 条)" in s
        assert s.rstrip().endswith("|")     # 无 hint 时尾部分隔符干净收尾



class TestGetEditModeStatus:
    def test_llm_item(self):
        assert "LLM 生成消息" in getEditModeStatus("llm")
        assert "Ctrl+S" in getEditModeStatus("llm") and "Esc" in getEditModeStatus("llm")

    def test_memory_item(self):
        assert "记忆内容" in getEditModeStatus("memory")



class TestGetHistoryBrowsingStatus:
    def test_basic(self):
        s = getHistoryBrowsingStatus(5, 0, "777")
        assert "[历史浏览]" in s and "5" in s and "777" in s

    def test_pending_messages_shown(self):
        s = getHistoryBrowsingStatus(2, 3, "777")
        assert "3 条新消息" in s

    def test_no_pending_clean(self):
        assert "新消息" not in getHistoryBrowsingStatus(2, 0, "777")
