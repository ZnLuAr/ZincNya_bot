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
    "看看这个", "帮我看", "帮看看", "帮忙看", "帮看一下",
    "给我看看", "让我看看", "打开看看",
    # 总结 / TLDR
    "总结一下", "总结", "概括", "归纳", "摘要", "提纲", "提炼", "简述",
    "一句话总结", "一句话概括", "长话短说", "太长不看", "懒得看", "没空看", "不想看",
    "帮我总结", "帮忙总结", "简单说说", "简单讲讲", "大概说说", "大概讲讲",
    "说个大概", "讲个大概", "给我概括", "精简一下",
    # 分析 / 解读
    "分析一下", "分析", "解释一下", "解释", "解读一下", "解读",
    "拆解", "展开讲讲", "点评", "评价", "评估", "研判",
    "怎么看", "你怎么看", "你觉得呢", "你觉得怎么样",
    "什么看法", "有什么看法", "怎么评价",
    # 可靠性 / 真伪
    "靠不靠谱", "靠谱吗", "可信吗", "是否可靠", "真假", "真伪", "核实", "验证", "辟谣",
    "有什么问题", "有没有问题", "有什么坑", "有没有坑", "优缺点", "利弊", "风险",
    "是真的吗", "是假的吗", "可靠吗", "准确吗", "靠谱不",
    "是不是智商税", "是不是骗人", "有没有猫腻", "是不是忽悠",
    # 翻译
    "翻译一下", "翻译", "译一下", "译成", "翻成",
    "翻成中文", "译成中文", "中文翻译", "翻成英文", "译成英文", "英文翻译",
    "中译英", "英译中",
    "给我翻译", "帮我翻译", "翻一下",
    # 提取 / 整理
    "提取一下", "提取", "抽取", "摘取", "找出", "列出", "整理一下", "整理", "汇总",
    "提取要点", "提取关键词", "关键词", "关键字",
    "提取标题", "提取作者", "提取日期", "提取引用", "提取链接", "提取代码", "提取表格",
    "帮我整理", "帮忙整理", "列个清单",
    # QA
    "讲了什么", "讲了啥", "说了什么", "说了啥", "写了什么", 
    "内容是什么", "主要内容", "核心观点", "关键信息", "重点内容",
    "要点", "结论", "主旨", "大意", "梗概",
    "什么意思", "什么含义", "怎么个事",
    "聊了什么", "聊了啥", "讨论了什么", "讨论了啥",
    "在说啥", "说的啥", "写的啥", "讲的啥",
    # 文档 / 教程
    "怎么用", "如何使用", "用法", "怎么安装", "如何安装", "怎么配置", "如何配置",
    "怎么部署", "如何部署", "怎么搭建", "如何搭建",
    # 玩梗 / 口语化 / 抽象话
    "这是什么鬼", "什么鬼", "啥玩意", "什么玩意", "什么东西",
    "搞笑吗", "有多离谱", "离了大谱", "绷不住了", "绷不住", "蚌埠住了",
    "啥情况", "什么情况", "怎么回事", "咋回事", "搞什么",
    "来解释解释", "解释一下是什么情况",
    "能看看这个不", "帮我瞅瞅", "给我瞅瞅",
    "吃瓜", "带我吃瓜", "前因后果", "来龙去脉",
    "给我整个活", "整个活",
    "你品", "你细品", "你品品", "品一品",
    "帮我看看这啥", "这啥", "这什么",
    "有意思吗", "有趣吗", "好玩吗", "好笑吗",
    "太长了", "好长", "好多字", "字太多",
    "一句话说明白", "给我划重点", "划重点", "划个重点", "敲黑板",
    # 抽象网络用语
    "什么成分", "成分如何", "什么含金量", "含金量多少",
    "什么水平", "什么级别", "什么段位",
    "有无懂哥", "懂哥来", "有懂的吗", "懂的说说", "有无吊大的",
    "什么勾巴", "什么几把",
    "什么抽象", "抽象玩意",
    "什么批事", "什么批东西",
    "什么烂活", "什么烂东西", "什么烂玩意",
    "什么几把", "什么几把玩意",
    "什么批登", "什么批站",
    "什么鬼东西", "什么鬼玩意",
    "什么寄吧", "什么寄巴",
    "什么勾八", "什么勾巴玩意",
    "什么批样", "什么批玩意",
    "什么批内容", "什么批文章",
    "什么批链接", "什么批网站",
    # 二次元 / ACG
    "有无懂哥", "有无大佬", "大佬来", "大佬解释下",
    "什么番", "什么作品", "什么动画",
    "什么梗", "这什么梗", "什么活", "整什么活",
    "什么节目效果", "节目效果", "效果拉满",
    # 吃瓜 / 八卦
    "什么瓜", "吃什么瓜", "什么大瓜", "瓜是什么",
    "什么料", "什么猛料", "什么爆料",
    "什么黑料", "什么实锤", "实锤了吗",
    "什么反转", "有无反转", "反转了吗",
    "什么剧情", "什么走向", "什么发展",
    # 震惊体 / 标题党
    "什么震惊", "震惊了", "什么惊天",
    "什么重磅", "重磅消息", "什么大新闻",
    "什么爆炸", "什么炸裂", "炸了吗",
    # 质疑 / 反讽
    "什么含量", "什么纯度", "纯度多少",
    "什么浓度", "浓度多少",
    "什么纯度", "纯度几何",
    "什么货色", "什么玩意儿",
    "什么批站", "什么批网站",
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
    "have a look", "peek at", "glance at",
    # 总结 / TLDR
    "summarize", "summarise", "summary", "recap", "overview", "abstract", "digest",
    "give me a summary", "give me the gist", "gist",
    "key points", "main points", "highlights", "takeaways", "key takeaways",
    "bottom line", "in short", "one sentence summary", "short version",
    "too long didn't read", "too long; didn't read", "tl;dr", "tldr",
    "cliff notes", "cliffnotes", "spark notes", "sparknotes",
    "eli5", "explain like i'm 5", "explain like im 5", "explain like i'm five",
    "give me the rundown", "rundown", "quick rundown",
    "what's the deal", "what's the scoop", "give me the lowdown",
    "ain't reading all that", "not reading all that",
    # 分析 / 解释
    "analyze", "analyse", "analysis", "explain", "interpret", "break down", "walk me through",
    "review", "evaluate", "assess", "comment on", "critique",
    "fact check", "fact-check", "verify",
    "what do you think", "your thoughts", "your take", "thoughts on this",
    "opinion on this", "how do you feel about",
    "is this legit", "is this bs", "is this real", "is this cap", "is this fake",
    "is this a scam", "is this bait",
    # 翻译
    "translate", "translation",
    "into chinese", "to chinese", "in chinese",
    "into english", "to english", "in english",
    "into japanese", "to japanese", "in japanese",
    # 提取
    "extract", "pull out", "list out", "find out", "pick out", "identify",
    "key ideas", "key message", "metadata",
    "title is", "author is", "published date",
    "give me the links", "list the references", "find the code",
    # QA
    "what is this about", "what's this about", "what is it about", "what's it about",
    "what does this say", "what does it say",
    "what does this mean", "what does it mean",
    "what is this", "what's this", "main idea", "main point", "main argument",
    "is this reliable", "is this trustworthy", "is this true", "pros and cons", "risks",
    "what happened", "what's going on", "what's happening",
    "what's the context", "give me context", "backstory", "lore",
    "what's the story", "story behind this",
    # Based on
    "based on this", "based on the link", "based on the article", "based on the page",
    "according to this", "according to the article", "according to the page",
    "in this article", "in this page", "does it mention", "does this mention",
    # Docs
    "how to use", "how do i use", "usage", "how to install", "installation",
    "how to configure", "configuration", "docs say", "documentation says",
    "how to set up", "setup guide", "getting started",
    # 玩梗 / 口语 / 网络用语
    "wtf is this", "what the hell", "what in the world", "what the actual",
    "is this for real", "no way", "bruh", "bro", "bruv",
    "spill the tea", "give me the tea", "the tea", "what's the tea",
    "lmao what", "lol what", "bro what", "bruh what",
    "fr fr", "no cap", "on god", "deadass",
    "this shit", "this stuff", "this thing",
    "what kinda", "what kind of", "what sorta",
    # 抽象 / meme
    "what's the vibe", "vibe check", "vibes",
    "based or cringe", "is this based", "is this cringe",
    "ratio", "L take", "W take", "mid or nah",
    "is this mid", "this mid", "this fire", "this slaps",
    "bussin or nah", "is this bussin",
    "what's the lore", "lore drop", "drop the lore",
    "context pls", "context please", "need context",
    "sauce", "source", "gimme sauce", "give sauce",
    # 质疑 / 反讽
    "copium", "hopium", "is this copium",
    "cap or no cap", "sounds like cap",
    "press x to doubt", "doubt",
    "sus", "kinda sus", "seems sus", "is this sus",
    "yikes", "oof", "big yikes",
    "clown shit", "clown take",
    # 震惊 / 夸张
    "holy shit", "holy fuck", "jesus christ",
    "no fucking way", "nfw",
    "insane", "wild", "crazy", "nuts",
    "unhinged", "unreal", "absurd",
    # 八卦 / drama
    "what's the drama", "drama alert", "spill",
    "what's the beef", "beef alert",
    "receipts", "show receipts", "where's the proof",
    "exposed", "callout", "call out",
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
    "随便看看", "随手转", "随手发", "存个档", "备忘",
    "不用读", "别读了", "不用看", "别看了",
]

_GLOBAL_SUPPRESS_EN = [
    "just sharing", "sharing only", "for reference only",
    "fyi only", "for your information only",
    "no action needed", "no need to reply",
    "do not reply", "don't reply", "dont reply",
    "do not respond", "don't respond", "dont respond",
    "never mind", "nevermind", "nvm",
    "just fyi", "bookmark", "bookmarking", "saving for later",
    "ignore this", "skip this", "not for you",
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
