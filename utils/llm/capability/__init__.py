"""
utils/llm/capability/

LLM 自带的能力，每个能力一个文件，说明、校验与送出都在里面：

    - message：发言——按 autoMode 直接发送或送审
    - memory：记忆操作——自动批准并执行，或送审
    - reaction：给触发消息加 emoji reaction——不经审核，直接生效

发言和记忆操作各有「真正送出」与「只收集」两种构造方式，入口按场景选用：Telegram
首次生成用前者，审核重试用后者。reaction 只在 Telegram 首次生成时提供。工具等外部
能力不在这里，它们在各自模块初始化时登记到 utils/llm/operation/capabilities.py。
"""

from .message import (
    MESSAGE_CAPABILITY_NAME,
    deliverMessage,
    makeCollectingMessageCapability,
    makeMessageCapability,
    makeSendingMessageCapability,
)
from .memory import (
    MEMORY_CAPABILITY_NAME,
    makeCollectingMemoryCapability,
    makeMemoryCapability,
    makeSubmittingMemoryCapability,
    submitMemoryAction,
)
from .reaction import (
    REACTION_CAPABILITY_NAME,
    makeReactionCapability,
    sendReaction,
)


__all__ = [
    "MESSAGE_CAPABILITY_NAME",
    "deliverMessage",
    "makeCollectingMessageCapability",
    "makeMessageCapability",
    "makeSendingMessageCapability",
    "MEMORY_CAPABILITY_NAME",
    "makeCollectingMemoryCapability",
    "makeMemoryCapability",
    "makeSubmittingMemoryCapability",
    "submitMemoryAction",
    "REACTION_CAPABILITY_NAME",
    "makeReactionCapability",
    "sendReaction",
]
