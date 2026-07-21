"""
utils/afc/afcIntent.py

AFC 意图判断

扫描 tools/*/triggers.py 并构建倒排索引，提供四级召回(L1 ~ L4)策略。
"""

import re
import asyncio
import importlib
from pathlib import Path


# ── 索引与缓存 ──────────────────────────────────────

_keywordIndex: dict[str, set[str]] = {}             # {"天气": {"weather"}, "算": {"calc"}}
_patternIndex: list[tuple[re.Pattern, str]] = []    # [(re.compile(...), "weather"), ...]
_allToolNames: set[str] = set()                     # {"weather", "calc", ...}
_lastTriggeredTools: dict[str, set[str]] = {}       # {chatID: {"weather"}}
_indexBuilt = False


# ── 预设兜底词 ────────────────────────────────────

GENERIC_TRIGGERS = [
    "帮我",
    "帮忙",
    "查一下",
    "算一下",
    "搜一下",
    "#afc",
    "#tool",
    "#工具",
]


# ── 指代词 ────────────────────────────────────────

REFERENTIAL_WORDS = [
    "那",
    "呢",
    "它",
    "这个",
    "那个",
    "再",
    "还",
    "也",
]


# L4 上下文延续：仅当消息不超过此长度且含指代词时，才继承上一轮工具集
MAX_CONTEXTUAL_MESSAGE_LEN = 10




def _logBadPattern(toolName: str, pattern: str):
    """
    记录触发词正则编译失败（同步上下文安全）

    _scanTriggers 在 handler 的 async 上下文中被调，但本身是同步函数，
    不能直接 await logSystemEvent。这里用 create_task 后台记，若当前没有
    运行中的事件循环则退化为 loop.run_until_complete 同步写完。
    
    任何日志失败都不应拖垮扫描。
    """
    async def _write():
        try:
            from utils.core.logger import logSystemEvent, LogLevel
            await logSystemEvent(
                "AFC 触发词编译失败",
                f"{toolName}: {pattern}",
                LogLevel.WARNING,
            )
        except Exception:
            pass  # create_task 分支的异常无人收，自行兜底——日志失败不拖垮扫描

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(_write())
        else:
            loop.run_until_complete(_write())
    except Exception:
        pass




def _scanTriggers():
    """启动时扫描 tools/*/triggers.py，构建倒排索引"""
    global _indexBuilt
    if _indexBuilt:
        return

    toolsPath = Path(__file__).parent / "tools"
    if not toolsPath.exists():
        _indexBuilt = True
        return

    for toolDir in toolsPath.iterdir():
        if not toolDir.is_dir() or toolDir.name.startswith(("_", ".")):
            continue

        toolName = toolDir.name

        # 跳过被禁用的工具（不注册触发词）
        from utils.afc.toolManager import isAfcToolEnabled
        if not isAfcToolEnabled(toolName):
            continue

        # 尝试导入 triggers 模块
        try:
            triggersModule = importlib.import_module(
                f"utils.afc.tools.{toolName}.triggers"
            )
        except (ImportError, AttributeError):
            continue

        # 构建关键词倒排索引
        keywords = getattr(triggersModule, "KEYWORDS", [])
        for kw in keywords:
            _keywordIndex.setdefault(kw, set()).add(toolName)

        # 构建正则倒排索引（单个坏 pattern 不拖垮整个扫描）
        patterns = getattr(triggersModule, "PATTERNS", [])
        for pat in patterns:
            try:
                compiled = re.compile(pat, re.IGNORECASE)
            except re.error:
                _logBadPattern(toolName, pat)
                continue
            _patternIndex.append((compiled, toolName))

        _allToolNames.add(toolName)

    _indexBuilt = True




def detectTools(message: str, chatID: str) -> set[str]:
    """
    返回候选工具名集合（空集 = 不注入 AFC）

    四级召回策略（宽松，高召回率）：
    1. 工具关键词匹配（倒排索引）
    2. 工具正则模式匹配（与 L1 并行执行）
    3. 通用兜底：命中预设词但无工具匹配 → 返回所有工具名
    4. 上下文延续：短消息 + 指代词 → 继承上一轮工具集

    L1 和 L2 并行执行，结果取并集（自动去重）。这样即使 L1 已命中，
    L2 的正则也有机会补充遗漏的工具。

    参数:
        message: 用户消息文本
        chatID: 聊天 ID（用于上下文延续）

    返回:
        候选工具名集合（空集表示无意图）
    """
    _scanTriggers()

    if not message:
        return set()

    messageLower = message.lower().strip()
    hits = set()

    # L1: 工具关键词匹配（倒排索引）
    for kw, tools in _keywordIndex.items():
        if kw.lower() in messageLower:
            hits |= tools

    # L2: 工具正则匹配（独立召回，与 L1 并行）
    for pat, toolName in _patternIndex:
        if pat.search(messageLower):
            hits.add(toolName)

    # L3: 通用兜底（命中预设词但无工具匹配 → 返回所有工具）
    if not hits and any(trigger.lower() in messageLower for trigger in GENERIC_TRIGGERS):
        hits = _allToolNames.copy()

    # L4: 上下文延续（短消息 + 指代词 → 继承上一轮）
    if not hits and len(message) <= MAX_CONTEXTUAL_MESSAGE_LEN:
        if any(ref in message for ref in REFERENTIAL_WORDS):
            lastTools = _lastTriggeredTools.get(chatID, set())
            if lastTools:
                hits = lastTools.copy()

    # 更新上下文记录
    if hits:
        _lastTriggeredTools[chatID] = hits

    return hits




def hasAFCIntent(message: str, chatID: str) -> bool:
    """
    判断用户消息是否有 AFC 工具调用意图

    参数:
        message: 用户消息文本
        chatID: 聊天 ID（用于上下文延续）

    返回:
        True 表示有工具调用意图，False 表示无意图
    """
    return bool(detectTools(message, chatID))
