"""
utils/fileSender.py

智能文件发送模块（自动分卷判断）
"""

import os

from config import (
    TELEGRAM_FILE_SIZE_LIMIT_MB,
    DEFAULT_READ_TIMEOUT,
    DEFAULT_WRITE_TIMEOUT,
)

from utils.core.logger import logAction, LogLevel, LogChildType
from utils.archiver import create7zVolumes




async def sendFileSmart(
    bot,
    chatID: int,
    filePath: str,
    caption: str = "",
    replyToMessageID: int = None,
    deleteAfter: bool = False
) -> list:
    """
    智能（大嘘）发送文件（自动判断是否需要分卷）

    参数：
        bot: Telegram Bot 实例
        chatID: 目标聊天 ID
        filePath: 文件路径
        caption: 说明文字
        replyToMessageID: 回复的消息 ID（可选）
        deleteAfter: 发送后是否删除原文件和分卷

    返回：
        发送的消息列表 [Message, ...]

    示例：
        messages = await sendFileSmart(
            bot=context.bot,
            chatID=123456,
            filePath="/download/stickers.zip",
            caption="表情包来啦喵——",
            deleteAfter=True
        )
    """
    if not os.path.isfile(filePath):
        raise FileNotFoundError(f"文件不存在：{os.path.basename(filePath)}")

    fileSize = os.path.getsize(filePath)
    limitBytes = TELEGRAM_FILE_SIZE_LIMIT_MB * 1024 * 1024

    if fileSize <= limitBytes:
        # 小文件直接发送
        with open(filePath, "rb") as f:
            msg = await bot.send_document(
                chat_id=chatID,
                document=f,
                caption=caption,
                reply_to_message_id=replyToMessageID,
                read_timeout=DEFAULT_READ_TIMEOUT,
                write_timeout=DEFAULT_WRITE_TIMEOUT,
            )

        if deleteAfter:
            await _safeRemove(filePath)

        return [msg]

    # 大文件分卷发送
    return await _sendMultiVolume(
        bot, chatID, filePath, caption, replyToMessageID, deleteAfter
    )




async def _sendMultiVolume(
    bot,
    chatID: int,
    filePath: str,
    caption: str,
    replyToMessageID: int,
    deleteAfter: bool
) -> list:
    """
    分卷压缩并发送（内部辅助函数）

    参数：
        bot: Telegram Bot 实例
        chatID: 目标聊天 ID
        filePath: 原文件路径
        caption: 说明文字（未使用，保留给将来）
        replyToMessageID: 回复的消息 ID
        deleteAfter: 是否删除原文件和分卷

    返回：
        发送的消息列表（不包含说明消息）

    异常：
        RuntimeError: 分卷创建失败
        TelegramError: 发送失败
    """
    baseName = os.path.splitext(os.path.basename(filePath))[0]
    outputDir = os.path.dirname(filePath)
    volumeBase = os.path.join(outputDir, f"{baseName}_7z")

    # 1. 创建分卷
    await logAction(
        "System",
        "文件过大，正在创建分卷",
        f"源文件: {os.path.basename(filePath)} ({os.path.getsize(filePath) / (1024**2):.1f} MB)",
        LogLevel.INFO,
        LogChildType.CHILD_WITH_CHILD
    )

    try:
        volumes = create7zVolumes(filePath, volumeBase)
    except Exception as e:
        await logAction(
            "System",
            "分卷创建失败",
            str(e),
            LogLevel.ERROR,
            LogChildType.LAST_CHILD
        )
        raise

    # 2. 发送解压说明
    instruction = (
        f"文件过大，已分为 {len(volumes)} 个压缩卷喵——\n\n"
        "对于分卷压缩包，解压的方法——\n"
        "1. 下载所有分卷到同一文件夹\n"
        f"2. 右键第一个文件（{os.path.basename(volumes[0])}）\n"
        '3. 选择"解压到此处"\n\n'
        "需要工具：7-Zip / WinRAR / The Unarchiver\n"
        "不过如果是 Windows 11 ，它是内置了分卷解压的支持的哦"
        "（手机端就需要安装解压 App 了，比如 MT Manager、ZArchiver）"
    )

    await bot.send_message(
        chat_id=chatID,
        text=instruction,
        reply_to_message_id=replyToMessageID
    )

    # 3. 发送所有卷（中途失败时清理已生成的分卷）
    messages = []
    try:
        for i, vol in enumerate(volumes, 1):
            volSize = os.path.getsize(vol) / (1024**2)
            with open(vol, "rb") as f:
                msg = await bot.send_document(
                    chat_id=chatID,
                    document=f,
                    caption=f"分卷 {i}/{len(volumes)} ({volSize:.1f} MB)",
                    read_timeout=DEFAULT_READ_TIMEOUT,
                    write_timeout=DEFAULT_WRITE_TIMEOUT,
                )
            messages.append(msg)

        await logAction(
            "System",
            f"分卷发送完成",
            f"共 {len(volumes)} 个文件",
            LogLevel.INFO,
            LogChildType.LAST_CHILD
        )

    except Exception as e:
        # 中途失败，清理已生成的分卷
        if deleteAfter:
            for vol in volumes:
                await _safeRemove(vol)
        raise  # 重新抛出，让上层 handler 处理

    # 4. 清理（仅在全部发送成功后）
    if deleteAfter:
        await _safeRemove(filePath)
        for vol in volumes:
            await _safeRemove(vol)

    return messages




async def _safeRemove(filePath: str) -> None:
    """
    比较安静地删除文件，失败不抛异常，仅记录警告日志

    用于清理临时文件，避免清理失败中断主流程。

    参数：
        filePath: 要删除的文件路径
    """
    if not os.path.exists(filePath):
        return

    try:
        os.remove(filePath)
    except OSError as e:
        await logAction(
            "System",
            "文件清理失败",
            f"{os.path.basename(filePath)}: {type(e).__name__}",
            LogLevel.WARNING,
            LogChildType.LAST_CHILD
        )
