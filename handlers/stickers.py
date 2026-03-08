import time
import asyncio
from telegram import Update , InlineKeyboardButton , InlineKeyboardMarkup
from telegram.ext import (
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

from utils.downloader import createStickerZip, deleteLater
from utils.logger import logAction, LogLevel, LogChildType
from utils.core.stateManager import getStateManager
from config import (
    CACHE_TTL,
    DELETE_DELAY,
    DEFAULT_READ_TIMEOUT,
    DEFAULT_WRITE_TIMEOUT,
)





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

    stickerSet = await context.bot.get_sticker_set(setName)
    setCachedSticker(setName , stickerSet)


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

    await logAction(
        "System",
        "成功找到表情包",
        f"表情包名称：{setName}",
        LogLevel.INFO,
        LogChildType.LAST_CHILD_WITH_CHILD
    )
    sent = await update.message.reply_text(text , reply_markup=keyboardFoundSticker)

    # 发出3分钟后删除
    asyncio.create_task(
        deleteLater(context , sent.chat_id , sent.message_id , None , DELETE_DELAY)
    )

    # 点击 “存为 .gif” 时，提示用户下载的表情包质量会不可避免地劣化
    # 要求再一次确认，所以第一次选择，使用 _gif 作为 callback_data
    # 下方是再一次确认的按钮。若不选择继续则退出下载




async def onDownloadPressed(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    _ , setName , action = query.data.split(":" , 2)
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
    
    # 开始下载
    stickerSuffix = action      # webp / gif


    await query.edit_message_text(
        f"收到——\n"
        f"表情包 “{stickerSet.title}”，\n"
        f"现在就给 @{query.from_user.username or query.from_user.first_name}下载喵……"
    )
    await logAction(
        query.from_user,
        "下载按钮被点击",
        f"开始下载 {setName} ({stickerSuffix})",
        LogLevel.INFO,
        LogChildType.WITH_CHILD
    )

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

    # 延时后删除相关信息
    asyncio.create_task(
        deleteLater(context, sent.chat_id, sent.message_id, zipPath, DELETE_DELAY)
    )




def getCachedSticker(setName: str):
    return getStateManager().getCachedSticker(setName)


def setCachedSticker(setName: str , stickerSet):
    getStateManager().setCachedSticker(setName , stickerSet)




def register():
    return {
        "handlers": [
            CommandHandler("findSticker", findSticker),
            CallbackQueryHandler(onDownloadPressed, pattern=r'^sticker:')
        ],
        "name": "表情包下载",
        "description": "查找并下载 Telegram 表情包",
    }