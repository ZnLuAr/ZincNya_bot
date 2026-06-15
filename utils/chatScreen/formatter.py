"""
utils/chatScreen/formatter.py

消息格式化工具。

提供纯函数：从 Telegram Message 提取展示文本、格式化为行列表等。
只负责格式化，不负责打印（打印归 history.py）。
"""

from datetime import datetime




def getSenderName(message) -> str:
    """提取消息发送者名称，缺失时回退为 Unknown。"""
    from_user = getattr(message, "from_user", None)
    if from_user:
        return from_user.username or from_user.first_name or "Unknown"

    chat = getattr(message, "chat", None)
    if chat:
        return getattr(chat, "title", None) or getattr(chat, "full_name", None) or "Unknown"

    return "Unknown"




def extractDisplayText(msg) -> str:
    """
    从 Telegram Message 对象提取展示文本。

    纯文字消息返回 text；带 caption 的媒体返回 caption；
    其余媒体类型返回方括号标注（如 [图片]、[贴纸]）。
    """
    if msg.text:
        return msg.text
    if msg.caption:
        prefix = ""
        if msg.photo:
            prefix = "[图片] "
        elif msg.document:
            prefix = "[文件] "
        elif msg.video:
            prefix = "[视频] "
        elif msg.animation:
            prefix = "[GIF] "
        return prefix + msg.caption
    if msg.photo:
        return "[图片]"
    if msg.sticker:
        emoji = msg.sticker.emoji or ""
        return f"[贴纸] {emoji}".strip()
    if msg.animation:
        return "[GIF]"
    if msg.video:
        return "[视频]"
    if msg.voice:
        return "[语音消息]"
    if msg.video_note:
        return "[视频留言]"
    if msg.audio:
        title = msg.audio.title or ""
        return f"[音频] {title}".strip()
    if msg.document:
        name = msg.document.file_name or ""
        return f"[文件] {name}".strip()
    if msg.contact:
        return "[联系人]"
    if msg.location:
        return "[位置]"
    if msg.poll:
        return f"[投票] {msg.poll.question}"
    return ""




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
        case "selfMessage":
            sender = "ZincNya~"
            text = content

    timestamp = datetime.now().strftime("%H:%M:%S")
    return timestamp, sender, text
