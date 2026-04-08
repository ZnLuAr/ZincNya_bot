"""
utils/llm/review.py

LLM 审核共享操作。

三个审核入口（Telegram 按钮、chatScreen TUI、独立 CLI）
的 send/retry/cancel 逻辑统一在此，各自只负责 UI 交互和结果展示。
"""

from utils.llm.client import generateReply
from utils.logger import logAction, LogLevel, LogChildType


async def reviewSend(bot, item: dict) -> None:
    """
    发送审核项的 reply 至目标聊天。

    参数:
        bot: Telegram Bot 实例
        item: 审核项（含 chatID, reply, messageID）
    """
    await bot.send_message(
        chat_id=item["chatID"],
        text=item["reply"],
        reply_to_message_id=item.get("messageID"),
    )
    await logAction(
        "System", "LLM 控制台审核：发送",
        f"原文：{item['originalMsg']}", LogLevel.INFO, LogChildType.WITH_ONE_CHILD,
    )


async def reviewRetry(item: dict) -> dict:
    """
    重新生成审核项的 reply，返回更新后的审核项。
    失败时抛出异常，调用方负责将原 item 放回队列。

    参数:
        item: 审核项

    返回:
        更新后的审核项（reply 已替换）
    """
    newReply = await generateReply(
        item["originalMsg"],
        item["chatID"],
        includeContext=bool(item.get("includeContext")),
        userID=item.get("userID"),
    )
    await logAction(
        "System", "LLM 控制台审核：重新生成",
        f"原文：{item['originalMsg']}", LogLevel.INFO, LogChildType.WITH_CHILD,
    )
    await logAction(
        "System", "",
        f"生成的消息：{newReply}", LogLevel.INFO, LogChildType.LAST_CHILD,
    )
    return {**item, "reply": newReply}


async def reviewCancel(item: dict) -> None:
    """
    取消审核项，记录日志。

    参数:
        item: 审核项
    """
    await logAction(
        "System", "LLM 控制台审核：取消",
        f"原文：{item['originalMsg']}", LogLevel.INFO, LogChildType.WITH_ONE_CHILD,
    )


async def reviewEditSubmit(item: dict, editedText: str) -> dict:
    """
    提交编辑后的审核项，记录日志，返回更新后的审核项。
    若编辑内容为空则返回原始 item（不修改）。

    参数:
        item: 原始审核项
        editedText: 编辑后的文本

    返回:
        更新后的审核项
    """
    if editedText.strip():
        editedItem = {**item, "reply": editedText}
        await logAction(
            "System", "LLM 控制台审核：编辑完成",
            f"原文：{item['originalMsg']}", LogLevel.INFO, LogChildType.WITH_CHILD,
        )
        await logAction(
            "System", "",
            f"编辑后：{editedText[:200]}", LogLevel.INFO, LogChildType.LAST_CHILD,
        )
        return editedItem
    return item
