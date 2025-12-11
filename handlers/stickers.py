from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
import asyncio
import os
import subprocess

from utils.downloader import createStickerZip, deleteLater
from utils.logger import logAction
from config import *


# 保存 sticker 信息临时缓存
stickerCache = {}




async def findSticker(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await logAction(update.effective_user , "使用 /findsticker 寻找表情包" , "OK喵" , "withChild")

    if not update.message.reply_to_message or not update.message.reply_to_message.sticker:
        await update.message.reply_text("？喵\n要用/findsticker的时候，要回复一下才行哦——")
        await logAction(None , None, "但不是以回复的方式使用指令😓……" , "lastChild")
        return
    
    sticker = update.message.reply_to_message.sticker
    setName = sticker.set_name

    if not setName:
        await update.message.reply_text("ごめんなさいニャー……\n😭没有找到所属的表情包呢……")
        await logAction(None , None , "没有找到所属的表情包喵……" , "lastChild")
        return
    
    stickerSet = await context.bot.get_sticker_set(setName)
    stickerCache[setName] = stickerSet


    # 构建用户互动界面（信息和按钮）
    messageText = (
        "找到了喵！\n"
        f"表情包名：{stickerSet.title}\n"
        f"表情代号：{setName}\n"
        f"表情数量：{len(stickerSet.stickers)}\n\n"
        "点下面的按钮，就可以下载哦——"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("こ ↑ こ ↓ ですよニャー！" , callback_data=f"download|{setName}")]
    ])

    sent = await update.message.reply_text(messageText , reply_markup=keyboard)
    await logAction(None , "成功找到喵——" , f"找到表情包 {setName}" , "lastChildWithChild")

    # 发出3分钟后删除
    asyncio.create_task(deleteLater(context , sent.chat_id , sent.message_id , None , DELETE_DELAY))

    # 哎我超这findSticker()怎么这么难.jpg
    pass




async def onDownloadPressed(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    if not query.data.startswith("download|"):
        return
    
    await logAction(update.effective_user , "下载按钮被点击，尝试下载完整表情包……" , "OK喵" , "withChild")
    setName = query.data.split("|")[1]
    stickerSet = stickerCache.get(setName)

    if not stickerSet:
        await query.edit_message_text(
            f"emmm……找找……\n"
            f"找、找不到了喵😰——\n"
            f"……试试再用/findsticker让咱再试一次吧……\n"
            f"お家を帰る——.jpg"
        )
        await logAction(None , None , f"找、找不到表情包 {setName} 了喵——" , "lastChild")
        return
    

    await query.edit_message_text(
        f"收到——\n"
        f"表情包“{stickerSet.title}”，\n"
        f"现在就给 @{query.from_user.username or query.from_user.first_name}下载喵……"
    )
    await logAction(None , "找到表情包，现在开始下载喵……" , f"开始下载 {setName}" , "childWithChild")
    

    # 打包
    zipPath = await createStickerZip(context.bot , stickerSet , setName)

    sent = await context.bot.send_document(
        chat_id=query.message.chat.id,
        document=open(zipPath , "rb"),
        caption=f"@{query.from_user.username or query.from_user.first_name} 様——\n表情包 {setName} 就发出来啦，请查收喵——"
    )
    await logAction(None , "表情包，成功发出了喵——" , f"成功发出 {setName}" , "lastChildWithChild")

    # 删除3分钟前的相关信息
    asyncio.create_task(deleteLater(context, sent.chat_id, sent.message_id, zipPath, DELETE_DELAY))


    # 哎我超这onDownloadPressed()怎么这么难.png😡
    pass




def register():
    return [
        CommandHandler("findSticker", findSticker),
        CallbackQueryHandler(onDownloadPressed)
    ]