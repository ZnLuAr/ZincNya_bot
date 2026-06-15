"""
utils/chatScreen/history.py

历史记录加载与显示。

负责从加密数据库加载历史聊天记录、格式化为行列表，以及在控制台打印。
"""

from utils.chatHistory import loadHistory, iterMessagesWithDateMarkers

from .formatter import formatMessage, formatMessageLines




def _printFormattedMessage(timestamp: str, sender: str, text: str):
    """按统一规则打印单条消息（兼容现有调用点）。"""
    for line in formatMessageLines(timestamp, sender, text):
        print(line)




def printMessage(mode, content):
    """打印消息到屏幕（兼容现有调用点）。"""
    timestamp, sender, text = formatMessage(mode, content)
    _printFormattedMessage(timestamp, sender, text)




async def buildHistoryLines(targetChatID) -> list[str]:
    """将历史聊天记录格式化为行列表，不打印。"""
    history = await loadHistory(targetChatID)
    lines: list[str] = []
    if history:
        lines.append("─────── 历史记录 ───────")
        lines.append("")
        for item_type, item_data in iterMessagesWithDateMarkers(history):
            if item_type == "date":
                lines.append(f"[{item_data}]")
            else:
                msg = item_data
                ts = msg["timestamp"].strftime("%H:%M:%S") if msg["timestamp"] else "??:??:??"
                sender = msg["sender"] or "Unknown"
                content = msg["content"]
                lines.extend(formatMessageLines(ts, sender, content))
        lines.append(f"<以上 {len(history)} 条>")
        lines.append("")
        lines.append("─────── 实时聊天 ───────")
        lines.append("")
    return lines




async def displayHistory(targetChatID):
    """显示历史聊天记录（兼容现有调用点）。"""
    for line in await buildHistoryLines(targetChatID):
        print(line)
