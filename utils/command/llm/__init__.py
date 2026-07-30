"""
utils/command/llm/

LLM 功能控制台命令包。对外暴露：
    - execute / getHelp：/llm 控制台命令入口（由 handlers/cli 动态调用）
    - handleChatScreenReviewCommand / handleChatScreenEditSubmit：chatScreen 内审核命令
      （由 utils/chatScreen/mainLoop 调用）

内部子模块：
    - _dispatch：主分发 + 状态汇总 + 帮助文本
    - memoryCmd / knowledgeCmd：对应子命令处理器
    - review/consoleReview / review/chatScreenReview：两套审核交互入口
"""




from ._dispatch import execute, getHelp
from .review.chatScreenReview import (
    handleChatScreenEditSubmit,
    handleChatScreenReviewCommand,
)




__all__ = [
    "execute",
    "getHelp",
    "handleChatScreenReviewCommand",
    "handleChatScreenEditSubmit",
]
