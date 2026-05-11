"""
utils/llm/urlIntent.py

URL 读取意图判断：
    - NFKC 归一化
    - 中英文关键词 / 正则模板
    - 全局抑制（出现即拒）
    - 近邻否定（仅在 intent 词附近 window 内生效）

对外只暴露：hasURLReadIntent(text) -> bool
"""




import re
import unicodedata




# ============================================================================
# 触发词（中文 substring 匹配）
# ============================================================================

_INTENT_KEYWORDS_ZH = [
    # 读取 / 访问
    "读一下", "读读", "读取", "阅读", "通读",
    "打开", "点开", "点进去",
    "访问", "浏览", "查看", "查看一下", "看一下", "看下", "看看", "瞅瞅", "瞧瞧", "康康",
    "加载", "拉取", "获取", "抓取", "爬取", "解析", "处理", "识别",
    # 总结 / TLDR
    "总结一下", "总结", "概括", "归纳", "摘要", "提纲", "提炼", "简述",
    "一句话总结", "一句话概括", "长话短说", "太长不看", "懒得看", "没空看", "不想看",
    # 分析 / 解读
    "分析一下", "分析", "解释一下", "解释", "解读一下", "解读",
    "拆解", "展开讲讲", "点评", "评价", "评估", "研判",
    # 可靠性 / 真伪
    "靠不靠谱", "靠谱吗", "可信吗", "是否可靠", "真假", "真伪", "核实", "验证", "辟谣",
    "有什么问题", "有没有问题", "有什么坑", "有没有坑", "优缺点", "利弊", "风险",
    # 翻译
    "翻译一下", "翻译", "译一下", "译成", "翻成",
    "翻成中文", "译成中文", "中文翻译", "翻成英文", "译成英文", "英文翻译",
    "中译英", "英译中",
    # 提取 / 整理
    "提取一下", "提取", "抽取", "摘取", "找出", "列出", "整理一下", "整理", "汇总",
    "提取要点", "提取关键词", "关键词", "关键字",
    "提取标题", "提取作者", "提取日期", "提取引用", "提取链接", "提取代码", "提取表格",
    # QA
    "讲了什么", "讲了啥", "说了什么", "说了啥", "写了什么",
    "内容是什么", "主要内容", "核心观点", "关键信息", "重点内容",
    "要点", "结论", "主旨", "大意", "梗概",
    "什么意思", "什么含义",
    # 文档 / 教程
    "怎么用", "如何使用", "用法", "怎么安装", "如何安装", "怎么配置", "如何配置",
]




# ============================================================================
# 中文意图正则（无明确动词的常见问法）
# ============================================================================

_INTENT_PATTERNS_ZH = [
    # 帮我 / 请 + 动作
    re.compile(
        r"(?:帮我|帮忙|麻烦|请|劳驾|能不能|可以|可否).{0,16}"
        r"(?:读|读取|阅读|看|打开|访问|浏览|抓取|爬取|解析|处理|"
        r"总结|概括|归纳|分析|解释|解读|翻译|提取|整理|汇总)"
    ),
    # 这个 / 这篇 + 讲 / 说 + 什么
    re.compile(
        r"(?:这个|这篇|这条|该|链接|网页|页面|文章|帖子|新闻|论文|文档).{0,24}"
        r"(?:讲|说|写|提到|讨论|介绍).{0,12}(?:什么|啥|哪些|内容|观点|结论)"
    ),
    # 里面 / 文中 + 有没有
    re.compile(
        r"(?:里面|文中|文章里|网页里|页面里|链接里|文档里).{0,16}"
        r"(?:有没有|是否|有无|提没提|说没说)"
    ),
    # 根据 / 基于 + 任务
    re.compile(
        r"(?:根据|基于|参考|按|依照).{0,20}"
        r"(?:这个|这篇|链接|网页|页面|文章|文档|内容).{0,30}"
        r"(?:回答|判断|分析|解释|总结|翻译|提取|整理|说明)"
    ),
]




# ============================================================================
# 英文触发词（按短语列表生成 word-boundary regex）
# ============================================================================

_INTENT_KEYWORDS_EN = [
    # 读取 / 访问
    "read this", "read the article", "read the page", "read the link", "read the post",
    "read the paper", "read the docs", "read the documentation", "read",
    "open this", "open the link", "open the page", "open",
    "fetch", "visit", "browse", "view", "look at", "take a look at", "take a look",
    "check this out", "check out", "check", "click", "load", "grab",
    "scrape", "crawl", "parse", "process", "inspect", "go through",
    # 总结 / TLDR
    "summarize", "summarise", "summary", "recap", "overview", "abstract", "digest",
    "give me a summary", "give me the gist", "gist",
    "key points", "main points", "highlights", "takeaways", "key takeaways",
    "bottom line", "in short", "one sentence summary", "short version",
    "too long didn't read", "too long; didn't read", "tl;dr", "tldr",
    # 分析 / 解释
    "analyze", "analyse", "analysis", "explain", "interpret", "break down", "walk me through",
    "review", "evaluate", "assess", "comment on", "critique",
    "fact check", "fact-check", "verify",
    # 翻译
    "translate", "translation",
    "into chinese", "to chinese", "in chinese",
    "into english", "to english", "in english",
    # 提取
    "extract", "pull out", "list out", "find out", "pick out", "identify",
    "key ideas", "key message", "metadata",
    "title is", "author is", "published date",
    # QA
    "what is this about", "what's this about", "what is it about", "what's it about",
    "what does this say", "what does it say",
    "what does this mean", "what does it mean",
    "what is this", "what's this", "main idea", "main point", "main argument",
    "is this reliable", "is this trustworthy", "is this true", "pros and cons", "risks",
    # Based on
    "based on this", "based on the link", "based on the article", "based on the page",
    "according to this", "according to the article", "according to the page",
    "in this article", "in this page", "does it mention", "does this mention",
    # Docs
    "how to use", "how do i use", "usage", "how to install", "installation",
    "how to configure", "configuration", "docs say", "documentation says",
]




# ============================================================================
# 英文意图正则（没有明确动词的问法）
# ============================================================================

_INTENT_PATTERNS_EN = [
    # can/could/would you + action
    re.compile(
        r"\b(?:can|could|would)\s+you\s+(?:please\s+)?"
        r"(?:read|open|fetch|visit|browse|check|summari[sz]e|analy[sz]e|explain|"
        r"translate|extract|review|parse|scrape|crawl|break\s+down)\b",
        re.IGNORECASE,
    ),
    # please/pls + action
    re.compile(
        r"\b(?:please|pls)\s+"
        r"(?:read|open|fetch|visit|browse|check|summari[sz]e|analy[sz]e|explain|"
        r"translate|extract|review|parse|scrape|crawl|break\s+down)\b",
        re.IGNORECASE,
    ),
    # what does X say/mean
    re.compile(
        r"\bwhat\s+does\s+"
        r"(?:this|it|the\s+(?:link|article|page|post|paper|doc|docs|documentation))"
        r"\s+(?:say|mean|claim|argue)\b",
        re.IGNORECASE,
    ),
    # based on / according to + action
    re.compile(
        r"\b(?:based\s+on|according\s+to|from)\s+"
        r"(?:this|the\s+(?:link|article|page|post|paper|doc|docs|documentation)).{0,80}"
        r"\b(?:answer|explain|summari[sz]e|analy[sz]e|tell\s+me|extract|translate|review)\b",
        re.IGNORECASE,
    ),
    # docs / how-to
    re.compile(
        r"\b(?:how\s+to|how\s+do\s+i|how\s+should\s+i).{0,60}"
        r"(?:use|install|configure|set\s+up|setup)\b",
        re.IGNORECASE,
    ),
]




# ============================================================================
# 全局抑制：出现即拒，不论是否有意图词
# ============================================================================

_GLOBAL_SUPPRESS_ZH = [
    "只是分享", "只分享", "仅分享", "仅作分享", "仅作记录", "仅供参考",
    "不用处理", "无需处理", "不用回复", "无需回复", "别回复", "不要回复",
    "不用管", "别管", "忽略", "跳过",
    "mark一下", "马克一下", "收藏一下", "发出来看看",
]

_GLOBAL_SUPPRESS_EN = [
    "just sharing", "sharing only", "for reference only",
    "fyi only", "for your information only",
    "no action needed", "no need to reply",
    "do not reply", "don't reply", "dont reply",
    "do not respond", "don't respond", "dont respond",
    "never mind", "nevermind", "nvm",
]




# ============================================================================
# 前置否定：只在紧跟 intent 词时拦截（像是 "不要只总结，帮我分析" 的情况）
# ============================================================================

_NEGATION_PREFIX_ZH = ["不要", "别", "不用", "无需", "不必", "先别", "暂时别"]
_NEGATION_PREFIX_EN = ["do not", "don't", "dont", "no need to", "no need", "needn't"]

_NEGATION_WINDOW = 32




# ============================================================================
# 核心 helper
# ============================================================================

def _normalizeText(text: str) -> str:
    """NFKC 归一化：全角 → 半角，清理兼容性字符"""
    return unicodedata.normalize("NFKC", text or "")


def _makeEnWordBoundaryPattern(phrase: str) -> re.Pattern:
    """英文短语转 word-boundary regex，避免 'read' 命中 'already'"""
    escaped = re.escape(phrase.lower()).replace(r"\ ", r"\s+")
    return re.compile(r"(?<![a-z0-9])" + escaped + r"(?![a-z0-9])", re.IGNORECASE)


_EN_INTENT_PATTERNS = [_makeEnWordBoundaryPattern(kw) for kw in _INTENT_KEYWORDS_EN]
_EN_SUPPRESS_PATTERNS = [_makeEnWordBoundaryPattern(kw) for kw in _GLOBAL_SUPPRESS_EN]
_EN_NEGATION_PATTERNS = [_makeEnWordBoundaryPattern(kw) for kw in _NEGATION_PREFIX_EN]


def _hasZhKeyword(text: str, keywords: list[str]) -> bool:
    return any(kw in text for kw in keywords)


def _hasEnPattern(textLower: str, patterns: list[re.Pattern]) -> bool:
    return any(p.search(textLower) for p in patterns)


def _hasGlobalSuppress(textNorm: str, textLower: str) -> bool:
    if _hasZhKeyword(textNorm, _GLOBAL_SUPPRESS_ZH):
        return True
    if _hasEnPattern(textLower, _EN_SUPPRESS_PATTERNS):
        return True
    return False


def _hasNegationNearIntent(textNorm: str, textLower: str) -> bool:
    """
    只在否定词后 _NEGATION_WINDOW 字符内出现 intent 词时才拦截。

    避免误杀 "不要只总结，帮我分析一下"。
    """
    # 中文
    for neg in _NEGATION_PREFIX_ZH:
        start = 0
        while True:
            idx = textNorm.find(neg, start)
            if idx < 0:
                break
            nearby = textNorm[idx: idx + _NEGATION_WINDOW]
            if _hasZhKeyword(nearby, _INTENT_KEYWORDS_ZH):
                return True
            start = idx + len(neg)

    # 英文
    for pattern in _EN_NEGATION_PATTERNS:
        for m in pattern.finditer(textLower):
            nearby = textLower[m.start(): m.start() + _NEGATION_WINDOW]
            if _hasEnPattern(nearby, _EN_INTENT_PATTERNS):
                return True

    return False


def _hasIntentSignal(textNorm: str, textLower: str) -> bool:
    if _hasZhKeyword(textNorm, _INTENT_KEYWORDS_ZH):
        return True
    if _hasEnPattern(textLower, _EN_INTENT_PATTERNS):
        return True
    if any(p.search(textNorm) for p in _INTENT_PATTERNS_ZH):
        return True
    if any(p.search(textLower) for p in _INTENT_PATTERNS_EN):
        return True
    return False




def hasURLReadIntent(text: str) -> bool:
    """
    保守判断用户是否有明确 URL 读取意图。

    判断顺序：
        1. #url / #readurl 显式 marker → True
        2. 全局抑制命中 → False
        3. 无意图信号 → False
        4. intent 附近出现前置否定 → False
        5. 其它 → True
    """
    textNorm = _normalizeText(text)
    textLower = textNorm.lower()

    if "#url" in textLower or "#readurl" in textLower:
        return True

    if _hasGlobalSuppress(textNorm, textLower):
        return False

    if not _hasIntentSignal(textNorm, textLower):
        return False

    if _hasNegationNearIntent(textNorm, textLower):
        return False

    return True
