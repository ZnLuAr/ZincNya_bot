"""
utils/llm/trigger.py

群聊触发判断策略：私聊恒触发；群聊按 groupTriggerMode 配置（mention / keyword）。
"""

from utils.llm.config import getGroupTriggerKeywords, getGroupTriggerMode
from utils.telegramHelpers import isMentionedByEntity




def matchesGroupTriggerKeyword(message) -> bool:
    """检查群聊消息是否命中 LLM 触发关键词"""
    text = (message.text or message.caption or "").strip().lower()
    if not text:
        return False
    return any(keyword and keyword in text for keyword in getGroupTriggerKeywords())




def shouldTriggerLLM(message, botUsername: str, isPrivate: bool) -> bool:
    """判断当前消息是否应触发 LLM：私聊始终触发；群聊按配置触发"""
    if isPrivate:
        return True
    if isMentionedByEntity(message, botUsername):
        return True
    if getGroupTriggerMode() == "keyword":
        return matchesGroupTriggerKeyword(message)
    return False
