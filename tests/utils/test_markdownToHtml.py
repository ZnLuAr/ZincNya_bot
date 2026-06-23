"""
tests/utils/test_markdownToHtml.py

测试 Markdown → HTML 转换工具
"""

import pytest
from utils.markdownToHtml import convertMarkdownToHtml


class TestCodeBlocks:
    """测试代码块转换"""

    def test_simple_code_block(self):
        """基本代码块转换"""
        input_text = "看这段代码：\n```python\nif x > 0:\n    print('ok')\n```"
        expected = "看这段代码：\n<pre><code>if x &gt; 0:\n    print('ok')</code></pre>"
        assert convertMarkdownToHtml(input_text) == expected

    def test_code_block_without_language(self):
        """无语言标识的代码块"""
        input_text = "```\ncode here\n```"
        expected = "<pre><code>code here</code></pre>"
        assert convertMarkdownToHtml(input_text) == expected

    def test_code_block_with_html_injection(self):
        """代码块内 HTML 注入防护"""
        input_text = "```html\n<script>alert('xss')</script>\n```"
        expected = "<pre><code>&lt;script&gt;alert('xss')&lt;/script&gt;</code></pre>"
        assert convertMarkdownToHtml(input_text) == expected


class TestInlineCode:
    """测试行内代码转换"""

    def test_simple_inline_code(self):
        """基本行内代码转换"""
        input_text = "详见 `config.py` 文件"
        expected = "详见 <code>config.py</code> 文件"
        assert convertMarkdownToHtml(input_text) == expected

    def test_multiple_inline_codes(self):
        """多个行内代码"""
        input_text = "运行 `npm install` 然后 `npm start`"
        expected = "运行 <code>npm install</code> 然后 <code>npm start</code>"
        assert convertMarkdownToHtml(input_text) == expected

    def test_inline_code_with_special_chars(self):
        """行内代码包含特殊字符"""
        input_text = "使用 `<div>` 标签"
        expected = "使用 <code>&lt;div&gt;</code> 标签"
        assert convertMarkdownToHtml(input_text) == expected


class TestBold:
    """测试粗体转换"""

    def test_simple_bold(self):
        """基本粗体转换"""
        input_text = "这是 **重要** 内容"
        expected = "这是 <b>重要</b> 内容"
        assert convertMarkdownToHtml(input_text) == expected

    def test_multiple_bolds(self):
        """多个粗体"""
        input_text = "**第一** 和 **第二**"
        expected = "<b>第一</b> 和 <b>第二</b>"
        assert convertMarkdownToHtml(input_text) == expected

    def test_bold_with_special_chars(self):
        """粗体内包含特殊字符"""
        input_text = "**<重要>**"
        expected = "<b>&lt;重要&gt;</b>"
        assert convertMarkdownToHtml(input_text) == expected

    def test_single_asterisk_not_bold(self):
        """单个 * 不触发粗体"""
        input_text = "计算 2*3*5 的结果"
        expected = "计算 2*3*5 的结果"
        assert convertMarkdownToHtml(input_text) == expected


class TestHeaders:
    """测试标题转换"""

    def test_h1_header(self):
        """一级标题"""
        input_text = "# 这是标题"
        expected = "<b>这是标题</b>"
        assert convertMarkdownToHtml(input_text) == expected

    def test_h2_header(self):
        """二级标题"""
        input_text = "## 子标题"
        expected = "<b>子标题</b>"
        assert convertMarkdownToHtml(input_text) == expected

    def test_h3_header(self):
        """三级标题"""
        input_text = "### 小标题"
        expected = "<b>小标题</b>"
        assert convertMarkdownToHtml(input_text) == expected

    def test_header_in_middle_of_text(self):
        """文本中间的标题"""
        input_text = "前言\n\n# 标题\n\n内容"
        expected = "前言\n\n<b>标题</b>\n\n内容"
        assert convertMarkdownToHtml(input_text) == expected


class TestSpecialCases:
    """测试特殊情况"""

    def test_underscore_not_converted(self):
        """下划线不被误判"""
        input_text = "编辑 file_name.txt"
        expected = "编辑 file_name.txt"
        assert convertMarkdownToHtml(input_text) == expected

    def test_html_tags_escaped(self):
        """HTML 标签被转义"""
        input_text = "用 <div> 标签"
        expected = "用 &lt;div&gt; 标签"
        assert convertMarkdownToHtml(input_text) == expected

    def test_ampersand_escaped(self):
        """& 符号被转义"""
        input_text = "A & B"
        expected = "A &amp; B"
        assert convertMarkdownToHtml(input_text) == expected

    def test_mixed_format(self):
        """混合格式"""
        input_text = "这是 **重要** 内容，详见 `config.py`，计算 2*3*5"
        expected = "这是 <b>重要</b> 内容，详见 <code>config.py</code>，计算 2*3*5"
        assert convertMarkdownToHtml(input_text) == expected

    def test_empty_string(self):
        """空字符串"""
        assert convertMarkdownToHtml("") == ""

    def test_plain_text(self):
        """纯文本无转换"""
        input_text = "这是普通文本，没有任何格式"
        expected = "这是普通文本，没有任何格式"
        assert convertMarkdownToHtml(input_text) == expected


class TestEdgeCases:
    """测试边缘情况"""

    def test_unclosed_bold(self):
        """未闭合的粗体"""
        input_text = "这是 **未闭合"
        expected = "这是 **未闭合"
        assert convertMarkdownToHtml(input_text) == expected

    def test_unclosed_code(self):
        """未闭合的代码"""
        input_text = "这是 `未闭合"
        expected = "这是 `未闭合"
        assert convertMarkdownToHtml(input_text) == expected

    def test_bold_across_lines(self):
        """跨行粗体（不支持）"""
        input_text = "**第一行\n第二行**"
        expected = "**第一行\n第二行**"
        assert convertMarkdownToHtml(input_text) == expected

    def test_nested_asterisks(self):
        """嵌套星号"""
        input_text = "**a**b**c**"
        expected = "<b>a</b>b<b>c</b>"
        assert convertMarkdownToHtml(input_text) == expected

    def test_code_with_backticks_inside(self):
        """代码内包含反引号（不支持嵌套）"""
        input_text = "`code with ` inside`"
        # 第一个配对会匹配 `code with `
        result = convertMarkdownToHtml(input_text)
        assert "<code>" in result

    def test_placeholder_collision(self):
        """占位符冲突（极端情况）"""
        input_text = "文本中包含 __CODE_BLOCK_0__ 字符串"
        # 不应该崩溃，可能显示异常但不影响功能
        result = convertMarkdownToHtml(input_text)
        assert isinstance(result, str)