"""将 Telegram 消息提取为 MessageInput；此处不拼接 prompt 或审核展示文本。"""

import re

from utils.llm.config import getMemoryEnabled
from utils.llm.memory.types import MemoryTurn
from utils.llm.models import MessageInput, MessageOrigin
from utils.telegramHelpers import removeMention




def getRawMessageText(message) -> str:
    "从原消息中获取消息文本。"
    return message.text or message.caption or ""


def getSenderDisplayName(message, userID: int | str) -> str:
    """优先使用用户名、姓名，缺失时使用用户 ID 或未知用户。"""
    user = message.from_user
    name = (user.username or user.first_name) if user else ""
    return name or str(userID or "未知用户")


def getMessageOrigin(message) -> MessageOrigin:
    """从 Telegram 消息集中提取聊天、发送者和消息定位信息。"""
    user = message.from_user
    userID = user.id if user else ""
    return MessageOrigin(
        chatID=str(message.chat_id),
        messageID=message.message_id,
        userID=userID,
        sender=getSenderDisplayName(message, userID),
    )


def extractPureMessage(text: str, botUsername: str) -> tuple[str, bool]:
    """去除机器人提及及开头的 #context；返回正文和本条消息的上下文开关。"""
    text = removeMention(text, botUsername)
    contextMatch = re.match(r"#context\b\s*(.*)", text, re.DOTALL)
    if contextMatch:
        return contextMatch.group(1).strip(), True
    return text, getMemoryEnabled()




def prepareMessageInput(
    message,
    rawText: str,
    botUsername: str,
    *,
    origin: MessageOrigin | None = None,
) -> MessageInput:
    """提取清理后的正文、完整引用和上下文开关，并附上统一的消息来源。"""
    text, includeContext = extractPureMessage(rawText, botUsername)
    origin = origin or getMessageOrigin(message)
    sender = origin.sender
    replyText = ""
    replySender = ""
    reply = message.reply_to_message
    if text and reply:
        replyText = getRawMessageText(reply)
        if replyText:
            replySender = getSenderDisplayName(reply, "")

    return MessageInput(
        origin=origin,
        text=text,
        replyText=replyText,
        replySender=replySender,
        includeContext=includeContext,
        memoryTurn=MemoryTurn(
            currentText=text,
            replyText=replyText,
            currentSender=f"@{sender}" if sender != "未知用户" else sender,
            replySender=(f"@{replySender}" if replySender != "未知用户" else replySender) if replyText else "",
        ) if text else None,
    )
