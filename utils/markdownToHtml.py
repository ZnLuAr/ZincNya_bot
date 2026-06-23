"""
utils/markdownToHtml.py

Markdown → Telegram HTML 转换工具

功能：
- 将 LLM 输出的 Markdown 格式转换为 Telegram 支持的 HTML
- 严格匹配 Markdown 语法，避免误判（如 2*3*5、file_name.txt）
- 对非 Markdown 部分做 HTML 转义（防止注入）
"""

import html
import re


def convertMarkdownToHtml(text: str) -> str:
    """
    将 Markdown 格式转换为 Telegram HTML

    处理流程（占位符策略）：
    1. 提取代码块 ```...```，替换为占位符 __CODE_BLOCK_N__
    2. 提取行内代码 `...`，替换为占位符 __INLINE_CODE_N__
    3. 转换粗体 **...**  → <b>...</b>（正则直接替换）
    4. 转换标题 # ...    → <b>...</b>（正则直接替换）
    5. 对剩余文本做 html.escape()（转义 <>&，包括步骤 3-4 生成的标签）
    6. 恢复占位符为实际 HTML 标签
    7. 恢复步骤 3-4 生成的 <b> / <code> / <pre> 标签（反转义）

    关键：
    - 步骤 1-2 提取的内容不经过步骤 5 的转义
    - 代码块内的内容单独做 html.escape(quote=False)（保证安全）
    - 粗体内的内容需要转义（防止 **<script>** 注入）

    安全说明：
    - 代码块使用 html.escape(quote=False)，不转义引号以保持可读性
    - ⚠️ 警告：返回值仅用于 HTML 文本内容，不要放入 HTML 属性
    - 如需用于属性，请额外调用 html.escape(quote=True)

    Args:
        text: LLM 原始回复（Markdown 格式）

    Returns:
        转换后的 HTML 字符串（仅用于 Telegram send_message 的 text 参数）

    示例：
        >>> convertMarkdownToHtml("这是 **重要** 内容，详见 `config.py`")
        "这是 <b>重要</b> 内容，详见 <code>config.py</code>"

        >>> convertMarkdownToHtml("计算 2*3*5 的结果")
        "计算 2*3*5 的结果"  # * 不在 ** 配对内，原样保留
    """
    # 1. 提取代码块
    codeBlocks = []

    def saveCodeBlock(match):
        lang = match.group(1) or ""  # 可选的语言标识
        code = match.group(2)
        # 代码块内容需要转义（防止注入）
        # （虽说引号不转义理论上是有注入面的吧，但是我们的代码并没有把用户输入放入 HTML 的属性
        # 不过要注意的是，用到这个函数的 LLM 模块，LLM 本身有可能被用户引导输出危险内容
        # 所以，返回值应仅用于 HTML 文本内容，不要放入 HTML 属性
        # 如需用于属性，应该额外调用 html.escape(quote=True)
        escaped = html.escape(code, quote=False)
        codeBlocks.append(f"<pre><code>{escaped}</code></pre>")
        return f"__CODE_BLOCK_{len(codeBlocks)-1}__"

    text = re.sub(r"```(\w+)?\n(.*?)\n```", saveCodeBlock, text, flags=re.DOTALL)

    # 2. 提取行内代码
    inlineCodes = []

    def saveInlineCode(match):
        code = match.group(1)
        # 行内代码也需要转义（但引号不需要转义）
        escaped = html.escape(code, quote=False)
        inlineCodes.append(f"<code>{escaped}</code>")
        return f"__INLINE_CODE_{len(inlineCodes)-1}__"

    text = re.sub(r"`([^`\n]+)`", saveInlineCode, text)

    # 3. 转换粗体（非贪婪匹配，不跨行，不包含 *）
    text = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", text)

    # 4. 转换标题（行首的 # 号）
    text = re.sub(r"^(#{1,3})\s+(.+)$", r"<b>\2</b>", text, flags=re.MULTILINE)

    # 5. 转义剩余文本中的 HTML 特殊字符
    # 注意：这会把 <b>、<code> 等已生成的标签也转义，所以需要步骤 6
    text = html.escape(text)

    # 6. 恢复占位符
    for i, block in enumerate(codeBlocks):
        text = text.replace(html.escape(f"__CODE_BLOCK_{i}__"), block)
    for i, code in enumerate(inlineCodes):
        text = text.replace(html.escape(f"__INLINE_CODE_{i}__"), code)

    # 7. 恢复步骤 3-4 生成的标签（它们在步骤 5 被转义了）
    text = text.replace("&lt;b&gt;", "<b>").replace("&lt;/b&gt;", "</b>")
    text = text.replace("&lt;code&gt;", "<code>").replace("&lt;/code&gt;", "</code>")
    text = text.replace("&lt;pre&gt;", "<pre>").replace("&lt;/pre&gt;", "</pre>")

    return text