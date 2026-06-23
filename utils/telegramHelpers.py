"""
utils/telegramHelpers.py

Telegram 消息操作的公共工具函数。
"""

from telegram.error import BadRequest

from utils.markdownToHtml import convertMarkdownToHtml


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





def prepareMarkdownReply(reply: str, maxLength: int = 4096) -> tuple[str, str]:
    """
    准备 LLM 回复文本：转换 Markdown → HTML

    功能：
    - 将 LLM 输出的 Markdown 格式转换为 Telegram HTML
    - 自动截断过长消息
    - 返回 (text, parse_mode) 元组，供调用方发送

    参数：
        reply: LLM 原始回复（Markdown 格式）
        maxLength: 最大消息长度（默认 4096）

    返回：
        (text, parse_mode) 元组
        - text: 转换后的 HTML 文本（已截断）
        - parse_mode: "HTML"

    示例：
        >>> prepareMarkdownReply("这是 **重要** 内容")
        ('这是 <b>重要</b> 内容', 'HTML')
    """
    htmlText = convertMarkdownToHtml(reply)
    if len(htmlText) > maxLength:
        htmlText = htmlText[:maxLength - 3] + "..."
    return (htmlText, "HTML")


async def sendLLMReply(
    bot,
    chatID: str | int,
    reply: str,
    replyToMessageID: int | None = None,
    maxLength: int = 4096,
) -> None:
    """
    发送 LLM 回复：转换 Markdown → HTML + 自动错误降级

    功能：
    - 调用 prepareMarkdownReply 转换格式
    - 发送消息
    - 如果 Telegram HTML 解析失败，自动降级为纯文本重新发送

    参数：
        bot: Telegram Bot 实例
        chatID: 目标聊天 ID
        reply: LLM 原始回复（Markdown 格式）
        replyToMessageID: 回复的消息 ID（可选）
        maxLength: 最大消息长度（默认 4096）

    示例：
        await sendLLMReply(
            bot=context.bot,
            chatID=123456,
            reply="这是 **重要** 内容",
            replyToMessageID=789,
        )
    """
    from utils.core.logger import logAction, LogLevel, LogChildType

    text, parse_mode = prepareMarkdownReply(reply, maxLength)

    try:
        await bot.send_message(
            chat_id=chatID,
            text=text,
            parse_mode=parse_mode,
            reply_to_message_id=replyToMessageID,
        )
    except BadRequest as e:
        if "can't parse entities" in str(e).lower():
            # HTML 解析失败，降级为纯文本
            await logAction(
                "System",
                "LLM 回复 HTML 解析失败，降级为纯文本",
                str(e),
                LogLevel.WARNING,
                LogChildType.WITH_ONE_CHILD,
            )
            truncated = reply[:maxLength - 3] + "..." if len(reply) > maxLength else reply
            await bot.send_message(
                chat_id=chatID,
                text=truncated,
                reply_to_message_id=replyToMessageID,
            )
        else:
            raise

