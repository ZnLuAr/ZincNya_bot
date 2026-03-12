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
