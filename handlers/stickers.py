import os
import time
import asyncio
import threading
from typing import Optional, Any
from telegram import Update , InlineKeyboardButton , InlineKeyboardMarkup
from telegram.ext import (
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

from utils.operators import getOperatorsWithPermission
from utils.logger import logAction, LogLevel, LogChildType
from utils.core.errorDecorators import handleTelegramErrors
from utils.downloader import createStickerZip, deleteMessageLater, registerFileCleanup, getActiveGifJobs
from config import (
    CACHE_TTL,
    DELETE_DELAY,
    DEFAULT_READ_TIMEOUT,
    DEFAULT_WRITE_TIMEOUT,
    GIF_QUEUE_ALERT_THRESHOLD,
    GIF_ALERT_COOLDOWN,
    Permission,
)




# ============================================================================
# 贴纸缓存（模块内管理，带 TTL）
# ============================================================================

_MAX_STICKER_CACHE = 50
_stickerCache: dict[str, tuple[Any, float]] = {}
_stickerLock = threading.RLock()

_lastGifAlertTime: float = 0.0  # GIF 过载告警时间戳（防告警风暴）


def getCachedSticker(setName: str) -> Optional[Any]:
    """获取缓存的贴纸集，过期返回 None"""
    with _stickerLock:
        if setName in _stickerCache:
            data, timestamp = _stickerCache[setName]
            if time.time() - timestamp < CACHE_TTL:
                return data
            del _stickerCache[setName]
        return None


def setCachedSticker(setName: str, stickerSet: Any):
    """缓存贴纸集"""
    with _stickerLock:
        now = time.time()

        # 清理过期条目
        expired = [
            k for k, (_, ts) in _stickerCache.items()
            if now - ts > CACHE_TTL
        ]
        for k in expired:
            del _stickerCache[k]

        # 超过上限时移除最旧的条目
        if len(_stickerCache) >= _MAX_STICKER_CACHE:
            oldest = min(_stickerCache, key=lambda k: _stickerCache[k][1])
            del _stickerCache[oldest]

        _stickerCache[setName] = (stickerSet, now)





@handleTelegramErrors(errorReply="诶……？被缠住了……没法动身去找……")
async def findSticker(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await logAction(
        update.effective_user,
        "使用 /findsticker 寻找表情包",
        "OK喵",
        LogLevel.INFO,
        LogChildType.WITH_CHILD
    )

    msg = update.message
    if not msg.reply_to_message or not msg.reply_to_message.sticker:
        await update.message.reply_text("？喵\n要用/findsticker的话，得回复一个表情哦——")
        await logAction(
            update.effective_user,
            "",
            "但不是以回复的方式使用指令",
            LogLevel.WARNING,
            LogChildType.LAST_CHILD
        )
        return

    sticker = update.message.reply_to_message.sticker
    setName = sticker.set_name
    if not setName:
        await update.message.reply_text("ごめんなさいニャー……\n😭没有找到所属的表情包呢……")
        await logAction(
            "System",
            "",
            "没有找到所属的表情包……",
            LogLevel.WARNING,
            LogChildType.LAST_CHILD
        )
        return

    try:
        stickerSet = await context.bot.get_sticker_set(setName)
        setCachedSticker(setName , stickerSet)
    except Exception as e:
        await update.message.reply_text(
            f"呜……获取表情包失败了……\n"
            f"可能是网络问题或表情包不存在喵……"
        )
        await logAction(
            "System",
            f"获取表情包 {setName} 失败",
            f"{type(e).__name__}: {str(e)}",
            LogLevel.ERROR,
            LogChildType.LAST_CHILD
        )
        return


    # 构建用户互动界面（信息和按钮）
    text = (
        "找到了喵！\n"
        f"表情包名：{stickerSet.title}\n"
        f"表情代号：{setName}\n"
        f"表情数量：{len(stickerSet.stickers)}\n\n"
        "点下面的按钮，就可以下载哦——"
    )

    keyboardFoundSticker = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("存为 .webp 喵" , callback_data=f"sticker:{setName}:webp"),
            InlineKeyboardButton("存为   .gif 喵" , callback_data=f"sticker:{setName}:gif_confirm"),
        ]
    ])

    # 点击 "存为 .gif" 时，提示用户下载的表情包质量会不可避免地劣化
    # 要求再一次确认，所以第一次选择，使用 gif_confirm 作为 callback_data
    # 下方是再一次确认的按钮。若不选择继续则退出下载


    await logAction(
        "System",
        "成功找到表情包",
        f"表情包名称：{setName}",
        LogLevel.INFO,
        LogChildType.LAST_CHILD_WITH_CHILD
    )
    sent = await update.message.reply_text(text , reply_markup=keyboardFoundSticker)

    # 发出 DELETE_DELAY(秒) 后删除消息
    asyncio.create_task(
        deleteMessageLater(context, sent.chat_id, sent.message_id, DELETE_DELAY)
    )




@handleTelegramErrors(errorReply="诶……？没法抓到图片……")
async def onDownloadPressed(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    # 解析 callback_data，防止 ValueError
    parts = query.data.split(":" , 2)
    if len(parts) != 3:
        await query.edit_message_text("无效的回调数据喵……")
        return

    _ , setName , action = parts
    stickerSet = getCachedSticker(setName)

    if not stickerSet:
        await query.edit_message_text(
            "フム……找找……\n"
            "找、找不到了喵😰——\n"
            "……试试再用/findsticker，\n"
            "让咱再试一次吧……\n\n"

            "お家を帰る——.jpg"
        )
        await logAction(
            "System",
            "",
            f"找不到表情包：{setName}",
            LogLevel.WARNING,
            LogChildType.LAST_CHILD
        )
        return
    
    if action == "gif_confirm":
        text = (
            "喵？\n"
            "真的要存为 GIF 吗……？\n\n"

            "如果存为 GIF，体积会显著增大，\n"
            "同时质量会不可避免地劣化哦……\n\n"

            "如果能接受，再继续吧——"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("存为 .webp" , callback_data=f"sticker:{setName}:webp"),
                InlineKeyboardButton("存为  .gif" , callback_data=f"sticker:{setName}:gif"),
            ]
        ])
        await update.callback_query.edit_message_text(text , reply_markup=keyboard)
        return
    
    # 校验 action 合法性
    _VALID_FORMATS = {"webp", "gif"}
    if action not in _VALID_FORMATS:
        await query.edit_message_text("无效的格式喵……")
        return


    # 开始下载
    stickerSuffix = action

    # GIF 过载检测与告警
    gifQueueNote = ""
    if stickerSuffix == "gif":
        activeJobs = getActiveGifJobs()
        if activeJobs >= GIF_QUEUE_ALERT_THRESHOLD:
            gifQueueNote = (
                f"\n\n啊、堆起来了……\n"
                f"目前有 {activeJobs} 个 GIF 任务正在进行，\n"
                "可能需要等一会儿哦……💦"
                )
            global _lastGifAlertTime
            nowAlert = time.monotonic()
            if (nowAlert - _lastGifAlertTime) > GIF_ALERT_COOLDOWN:
                _lastGifAlertTime = nowAlert
                alertText = f"⚠️ GIF 转换队列堆积，当前有 {activeJobs} 个任务正在进行"
                for opID in getOperatorsWithPermission(Permission.NOTIFY):
                    try:
                        await context.bot.send_message(chat_id=int(opID), text=alertText)
                    except Exception:
                        pass


    await query.edit_message_text(
        f"收到——\n"
        f"表情包 {stickerSet.title}，\n"
        f"现在就给 @{query.from_user.username or query.from_user.first_name}下载喵……"
        + gifQueueNote
    )
    await logAction(
        query.from_user,
        "下载按钮被点击",
        f"开始下载 {setName} ({stickerSuffix})",
        LogLevel.INFO,
        LogChildType.WITH_CHILD
    )


    zipPath = None
    try:
        zipPath = await createStickerZip(
            context.bot,
            stickerSet,
            setName,
            stickerSuffix
        )

        with open(zipPath , "rb") as f:
            sent = await context.bot.send_document(
                read_timeout=DEFAULT_READ_TIMEOUT,
                write_timeout=DEFAULT_WRITE_TIMEOUT,
                chat_id=query.message.chat.id,
                document=f,
                caption=(
                    f"@{query.from_user.username or query.from_user.first_name} 様——\n"
                    f"表情包 {setName}，\n"
                    "就发出来啦，请查收喵——\n"
                ),
            )

        await logAction(
            "System",
            f"表情包 {setName} 成功发出喵——",
            f"as {stickerSuffix} to {query.from_user}",
            LogLevel.INFO,
            LogChildType.LAST_CHILD_WITH_CHILD
        )

        # 文件清理注册到 resourceManager（关机时保证执行）
        registerFileCleanup(zipPath)
        # 消息延时删除
        asyncio.create_task(
            deleteMessageLater(context, sent.chat_id, sent.message_id, DELETE_DELAY)
        )


    except Exception as e:
        await query.edit_message_text(
            f"啊、下载失败了……\n"
            f"请稍后再试，或者联系管理员喵……"
        )
        await logAction(
            "System",
            f"表情包 {setName} 下载失败",
            f"{type(e).__name__}: {str(e)}",
            LogLevel.ERROR,
            LogChildType.LAST_CHILD
        )
        # 清理可能残留的 zip 文件
        if zipPath and os.path.exists(zipPath):
            try:
                os.remove(zipPath)
            except Exception:
                pass




def register():
    return {
        "handlers": [
            CommandHandler("findSticker", findSticker),
            CallbackQueryHandler(onDownloadPressed, pattern=r'^sticker:')
        ],
        "name": "表情包下载",
        "description": "查找并下载 Telegram 表情包",
    }