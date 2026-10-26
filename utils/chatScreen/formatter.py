"""
utils/chatScreen/formatter.py

消息格式化工具。

提供纯函数：从 Telegram Message 提取展示文本、格式化为行列表等。
只负责格式化，不负责打印（打印归 history.py）。
"""

from datetime import datetime

from config import BOT_DISPLAY_NAME

from utils.telegramHelpers import (
    extractTelegramMessageText as extractDisplayText,
    getTelegramSenderName as getSenderName,
    getTelegramMessageTime,
)




def formatMessageLines(timestamp: str, sender: str, text: str) -> list[str]:
    """按统一规则将单条消息格式化为行列表，支持多行内容。"""
    sender = sender or "Unknown"
    text = text or ""

    if '\n' in text:
        lines = text.split('\n')
        result = [f"[{timestamp}] <{sender}> {lines[0]}"]
        indent = "          | "
        for line in lines[1:]:
            result.append(f"{indent}{line}")
        return result
    else:
        return [f"[{timestamp}] <{sender}> {text}"]




def formatMessage(mode, content) -> tuple[str, str, str]:
    """
    格式化单条消息，返回 (timestamp, sender, text)。
    供 UI 层与 print 层共用。

    参数:
        mode: "incomingMessage" 或 "selfMessage"
        content: incomingMessage 时为 Telegram Message 对象，selfMessage 时为字符串
    """
    match mode:
        case "incomingMessage":
            sender = getSenderName(content)
            text = extractDisplayText(content)
            timestamp = getTelegramMessageTime(content)
        case "selfMessage":
            sender = BOT_DISPLAY_NAME
            text = content
            timestamp = datetime.now().strftime("%H:%M:%S")

    return timestamp, sender, text
