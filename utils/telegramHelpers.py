"""
utils/telegramHelpers.py

Telegram 消息操作的公共工具函数。
"""

from html import escape as _htmlEscape

from telegram.constants import MessageEntityType
from telegram.error import BadRequest

from config import TG_MESSAGE_MAX_LEN

from utils.markdownToHtml import convertMarkdownToHtml




def escapeHtml(text: str) -> str:
    """转义 HTML 特殊字符（& < >），用于 TG HTML parse_mode 的动态文本拼接。

    封装标准库 html.escape(quote=False)——只转义 & < >，不转义引号（卡片正文用，
    非属性值）。emoji 等普通 Unicode 字符不受影响，原样保留。
    """
    return _htmlEscape(text, quote=False)




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




def isMentionedByEntity(message, botUsername: str) -> bool:
    """
    按 Telegram mention entity 精确匹配 @botUsername。

    只认 MessageEntityType.MENTION 实体且实体文本与 @botUsername 完全相等
    （子串匹配会让 @notmybot 之类同前缀名误中），同时检查 text 与 caption
    （支持图片 caption @bot 场景）。
    """
    expected = f"@{botUsername}".lower()
    for text, entities in (
        (message.text or "", message.entities or []),
        (message.caption or "", message.caption_entities or []),
    ):
        for entity in entities:
            if entity.type != MessageEntityType.MENTION:
                continue
            mention = text[entity.offset:entity.offset + entity.length].lower()
            if mention == expected:
                return True
    return False




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




def truncateText(text: str, limit: int, *, suffix: str = "…") -> str:
    """
    按 Unicode 码点长度截断文本，超出 limit 时附加 suffix，保证返回长度 ≤ limit。

    limit ≤ len(suffix) 时返回 suffix 前 limit 个字符，避免负切片。
    suffix 默认单字省略号；调用方可传 "……[内容过长，已截断]" / "..." 等自定义提示。

    与 scripts/merge_data.py 的 _truncate（按显示宽度 east_asian_width 截断，做终端表格列对齐）
    语义不同，有意不统一。
    """
    if len(text) <= limit:
        return text
    if limit <= len(suffix):
        return suffix[:limit]
    return text[:limit - len(suffix)] + suffix




def prepareMarkdownReply(reply: str, maxLength: int = TG_MESSAGE_MAX_LEN) -> tuple[str, str]:
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
    htmlText = truncateText(htmlText, maxLength, suffix="...")
    return (htmlText, "HTML")


async def sendLLMReply(
    bot,
    chatID: str | int,
    reply: str,
    replyToMessageID: int | None = None,
    maxLength: int = TG_MESSAGE_MAX_LEN,
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
            truncated = truncateText(reply, maxLength, suffix="...")
            await bot.send_message(
                chat_id=chatID,
                text=truncated,
                reply_to_message_id=replyToMessageID,
            )
        else:
            raise
