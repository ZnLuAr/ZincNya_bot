"""
utils/command/history.py

用于预览和导出聊天历史记录的命令模块。

提供以下功能：
    - 列出所有有记录的会话（含消息数量和最后消息时间）
    - 预览指定会话的最近 N 条消息
    - 将指定会话（或全部会话）的聊天记录导出为 .txt 文件


================================================================================
命令用法
================================================================================

/history                               # 列出所有有记录的会话
/history -c <chatID>                   # 预览该会话最近 N 条消息（默认 20 条）
/history -c <chatID> -n <数量>         # 预览最近指定数量的消息
/history -c <chatID> --export          # 导出该会话为 txt 文件
/history --export                      # 导出所有会话为 txt 文件

导出文件保存至 data/chatExport/ 目录，文件名格式为：
    chat_<chatID>_<日期时间>.txt（单个或全部导出均按会话分文件）


================================================================================
参数说明
================================================================================

-c / --chat   <chatID>   指定目标会话 ID
-n            <数量>     预览条数（默认使用 config.CHAT_PREVIEW_LIMIT）
-e / --export            执行导出操作（而非预览）

注意：--export 优先级高于 -n，同时指定时 -n 会被忽略（导出始终为全量）。


================================================================================

聊天记录通过 chatHistory 模块进行解密读取。
导出目录由 config.CHAT_EXPORT_DIR 指定，不存在时自动创建。

"""




import os
import re
from datetime import datetime

from config import CHAT_EXPORT_DIR, CHAT_PREVIEW_LIMIT

from handlers.cli import parseArgsTokens

from utils.logger import logAction, LogLevel, LogChildType, logSystemEvent
from utils.chatHistory import (
    loadHistory,
    getChatList,
    getMessageCount,
    iterMessagesWithDateMarkers,
)



async def execute(app, args: list[str]):

    parsed = {
        "chat":   None,
        "n":      None,
        "export": None,
    }

    argAlias = {
        "c": "chat",
        "e": "export",
    }


    parsed = parseArgsTokens(parsed, args, argAlias)

    chatID    = parsed["chat"]
    countArg  = parsed["n"]
    doExport  = parsed["export"]


    # --export 导出模式
    if doExport is not None:
        if chatID and chatID != "NoValue":
            await logAction(
                "System",
                f"/history --export -c {chatID}",
                "开始导出喵",
                LogLevel.INFO,
                LogChildType.WITH_CHILD
            )
            await _exportChat(chatID)
        else:
            await logAction(
                "System",
                "/history --export",
                "开始导出全部会话喵",
                LogLevel.INFO,
                LogChildType.WITH_CHILD
            )
            await _exportAll()
        return

    # -c <chatID> 预览模式
    if chatID and chatID != "NoValue":
        limit = CHAT_PREVIEW_LIMIT
        if countArg and countArg != "NoValue":
            try:
                limit = int(countArg)
                if limit <= 0:
                    print("预览条数必须是正整数喵——\n")
                    return
            except ValueError:
                print(f"无效的条数喵：{countArg}\n")
                return
        _previewChat(chatID, limit)
        return

    # 无参数：列出所有会话
    _listChats()




# ============================================================================
# 功能函数
# ============================================================================

def _listChats():
    """列出所有有记录的会话"""
    import asyncio
    chats = asyncio.run(getChatList())

    if not chats:
        print("（暂无任何聊天记录……）\n")
        return

    print(f"{'─' * 56}")
    print(f"  {'Chat ID':<20}  {'消息数':>8}  {'最后消息时间'}")
    print(f"{'─' * 56}")

    for chat in chats:
        cid   = chat["chat_id"]
        count = chat["message_count"]
        ts    = chat["last_message_time"]
        ts_str = ts.strftime("%Y-%m-%d %H:%M:%S") if ts else "未知"
        print(f"  {cid:<20}  {count:>8}  {ts_str}")

    print(f"{'─' * 56}")
    print(f"  共 {len(chats)} 个会话，使用 -c <chatID> 预览消息喵\n")


def _sanitizeChatID(chatID: str) -> str:
    """清洗 chatID，仅保留数字和前导负号，防止路径穿越"""
    sanitized = re.sub(r"[^\d\-]", "_", chatID)
    return sanitized


def _previewChat(chatID: str, limit: int):
    """预览指定会话的最近 N 条消息"""
    import asyncio
    total = getMessageCount(chatID)

    if total == 0:
        print(f"（Chat {chatID} 暂无任何聊天记录喵……）\n")
        return

    messages = asyncio.run(loadHistory(chatID, limit=limit))

    if not messages:
        print(f"（ChatID {chatID} 有 {total} 条记录但全部解密失败了喵……密钥可能已更换）\n")
        return

    showing = len(messages)
    print(f"\n─────── Chat {chatID} 的最近 {showing} 条消息（共 {total} 条）───────\n")

    # 使用生成器遍历消息，自动插入日期分隔符
    for item_type, item_data in iterMessagesWithDateMarkers(messages):
        if item_type == "date":
            # 日期分隔行
            print(f"  [{item_data}]")
        else:
            # 消息行（时间戳简化为 HH:MM:SS）
            msg = item_data
            ts = msg["timestamp"].strftime("%H:%M:%S") if msg["timestamp"] else "??:??:??"
            sender = msg["sender"] or "Unknown"
            arrow = "→" if msg["direction"] == "outgoing" else "←"
            print(f"  [{ts}] {arrow} <{sender}> {msg['content']}")

    print(f"\n─────── 以上 {showing} 条 ───────")
    if total > showing:
        print(f"  （还有 {total - showing} 条更早的消息，可使用 -n {total} 查看全部）")
    print()


async def _exportChat(chatID: str):
    """将指定会话导出为 txt 文件"""
    total = getMessageCount(chatID)

    if total == 0:
        await logSystemEvent(
            "",
            f"ChatID {chatID} 暂无任何聊天记录，无需导出喵……",
            LogLevel.INFO,
            LogChildType.LAST_CHILD
        )
        return

    messages = await loadHistory(chatID)

    if not messages:
        await logSystemEvent(
            f"记录全部解密失败了喵……可能是密钥已经更换",
            f"ChatID {chatID}： {total} 条记录全部解密失败",
            LogLevel.ERROR,
            LogChildType.LAST_CHILD_WITH_CHILD
        )
        return

    os.makedirs(CHAT_EXPORT_DIR, exist_ok=True)

    timestamp  = datetime.now().strftime("%Y%m%d_%H%M%S")
    safeChatID = _sanitizeChatID(chatID)
    filename   = f"chat_{safeChatID}_{timestamp}.txt"
    filepath   = os.path.join(CHAT_EXPORT_DIR, filename)

    if await _writeExportFile(filepath, chatID, messages):
        await logSystemEvent(
            f"已将 ChatID {chatID} 的 {len(messages)} 条消息导出",
            filepath,
            LogLevel.INFO,
            LogChildType.LAST_CHILD_WITH_CHILD
        )


async def _exportAll():
    """将所有会话导出为各自的 txt 文件"""
    chats = await getChatList()

    if not chats:
        await logSystemEvent(
            "",
            "暂无任何聊天记录，无需导出喵……",
            LogLevel.INFO,
            LogChildType.LAST_CHILD
        )
        return

    os.makedirs(CHAT_EXPORT_DIR, exist_ok=True)

    timestamp  = datetime.now().strftime("%Y%m%d_%H%M%S")
    exported   = 0
    failed     = 0

    await logSystemEvent(
        "",
        f"开始导出全部 {len(chats)} 个会话……",
        LogLevel.INFO,
        LogChildType.ONLY_RESULT
    )

    for chat in chats:
        chatID = chat["chat_id"]
        messages = await loadHistory(chatID)

        if not messages:
            await logSystemEvent(
                "",
                f"ChatID {chatID}：解密失败，跳过喵",
                LogLevel.WARNING,
                LogChildType.ONLY_RESULT
            )
            failed += 1
            continue

        safeChatID = _sanitizeChatID(chatID)
        filename = f"chat_{safeChatID}_{timestamp}.txt"
        filepath = os.path.join(CHAT_EXPORT_DIR, filename)

        if await _writeExportFile(filepath, chatID, messages):
            await logSystemEvent(
                "",
                f"Chat {chatID}：{len(messages)} 条 → {filename}",
                LogLevel.INFO,
                LogChildType.ONLY_RESULT
            )
            exported += 1
        else:
            failed += 1

    await logSystemEvent(
        f"导出完成喵—— 总计成功 {exported} 个，失败 {failed} 个",
        f"文件保存在：{CHAT_EXPORT_DIR}",
        LogLevel.INFO,
        LogChildType.LAST_CHILD_WITH_CHILD
    )


async def _writeExportFile(filepath: str, chatID: str, messages: list) -> bool:
    """
    将消息列表写入文本文件。

    返回：
        成功返回 True，失败返回 False
    """
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(f"ZincNya Bot — 聊天记录导出\n")
            f.write(f"Chat ID : {chatID}\n")
            f.write(f"导出时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"消息总数: {len(messages)} 条\n")
            f.write("=" * 64 + "\n\n")

            # 使用生成器遍历消息，自动插入日期分隔符
            for item_type, item_data in iterMessagesWithDateMarkers(messages):
                if item_type == "date":
                    # 日期分隔行
                    f.write(f"[{item_data}]\n")
                else:
                    # 消息行（时间戳简化为 HH:MM:SS）
                    msg = item_data
                    ts = msg["timestamp"].strftime("%H:%M:%S") if msg["timestamp"] else "??:??:??"
                    sender = msg["sender"] or "Unknown"
                    arrow = "→" if msg["direction"] == "outgoing" else "←"
                    f.write(f"[{ts}] {arrow} <{sender}>\n")
                    f.write(f"  {msg['content']}\n\n")

        return True

    except Exception as e:
        await logSystemEvent(
            "",
            f"写入文件失败喵：{str(e)}",
            LogLevel.ERROR,
            LogChildType.LAST_CHILD,
            exception=e
        )
        return False




def getHelp():
    return {

        "name": "/history",

        "description": "预览或导出聊天历史记录",

        "usage": (
            "/history\n"
            "/history -c <chatID> (-n <数量>)\n"
            "/history (-c <chatID>) --export"
        ),

        "example": (
            "列出所有会话：/history\n"
            "预览最近 20 条：/history -c '1234567'\n"
            "预览最近 50 条：/history -c '1234567' -n 50\n"
            "导出指定会话：/history -c '1234567' --export\n"
            "导出全部会话：/history --export"
        ),

    }
