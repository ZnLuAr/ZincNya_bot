"""
tests/utils/llm/test_state.py

测试 LLM 运行时状态（utils/llm/state.py）的审核提示格式化。

重点：_formatReviewHint 对 content/reply 中的换行符做转义（content.replace
('\\n', '\\\\n')），避免多行内容把控制台底边栏撑断 —— 本批修复的回归点。
"""

from utils.llm.state import _formatReviewHint


class TestFormatReviewHintNewlineEscape:
    """_formatReviewHint：换行转义 + 截断"""

    def test_reply_newline_escaped(self):
        """回复含换行：预览里换行被转义为字面 \\n"""
        hint = _formatReviewHint({"kind": "reply", "reply": "第一行\n第二行"})

        assert "\n第二行" not in hint           # 不含真实换行
        assert "第一行\\n第二行" in hint          # 含转义后的字面 \n

    def test_reply_no_newline_unchanged(self):
        """回复无换行：原样展示"""
        hint = _formatReviewHint({"kind": "reply", "reply": "单行回复"})
        assert "单行回复" in hint

    def test_reply_truncated_on_escaped_length(self):
        """截断基于转义后字符串：>16 字符附加省略号"""
        hint = _formatReviewHint({"kind": "reply", "reply": "一二三四五六七八九十一二三四五六七"})
        assert "…" in hint

    def test_memory_content_newline_escaped(self):
        """记忆操作 content 含换行：同样被转义"""
        item = {"kind": "memory", "action": {"action": "add", "content": "甲\n乙"}}
        hint = _formatReviewHint(item)

        assert "\n乙" not in hint
        assert "甲\\n乙" in hint

    def test_memory_fallback_to_memory_id(self):
        """记忆操作无 content 时回退到 #memoryID"""
        item = {"kind": "memory", "action": {"action": "delete", "memoryID": 42}}
        hint = _formatReviewHint(item)
        assert "#42" in hint
