"""
utils/telegramHelpers.py

Telegram 消息操作的公共工具函数。
"""

from telegram.error import BadRequest


async def safeEditMessage(message, text: str, **kwargs) -> bool:
    """
    安全地编辑消息，抑制 "Message is not modified" 错误。

    当用户快速点击按钮时，可能触发多次编辑请求，
    如果内容相同，Telegram 会抛出 BadRequest。

    返回:
        True  - 编辑成功
        False - 内容未变化（静默忽略）
    """
    try:
        await message.edit_text(text, **kwargs)
        return True
    except BadRequest as e:
        if "Message is not modified" in str(e):
            return False
        raise


def isMentioned(message, botUsername: str) -> bool:
    """
    检测消息是否 @ 了 bot（纯 Telegram 语义）。

    同时检查 message.text 和 message.caption，
    以支持图片消息在 caption 中 @bot 的场景。

    参数:
        message: telegram.Message 对象
        botUsername: bot 的用户名（如 "MyZincNyaBot"）

    返回:
        True 如果消息中包含 @botUsername
    """
    mention = f"@{botUsername}".lower()
    text = message.text or ""
    caption = message.caption or ""
    return mention in text.lower() or mention in caption.lower()


def removeMention(text: str, botUsername: str) -> str:
    """
    去除消息中的 @bot 前缀，返回纯文本。

    参数:
        text: 消息文本
        botUsername: bot 的用户名

    返回:
        去除 @bot 后的纯文本
    """
    import re
    pattern = re.compile(re.escape(f"@{botUsername}"), re.IGNORECASE)
    return pattern.sub("", text).strip()
