import asyncio
import random

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from utils.core.errorDecorators import handleTelegramErrors
from utils.logger import logAction, LogLevel, LogChildType
from utils.nyaQuoteManager.data import getRandomQuote


# 多条消息之间的发送间隔范围（秒）
MESSAGE_DELAY_MIN = 1
MESSAGE_DELAY_MAX = 3




# 响应来自 Telegram 的 /nya 指令，随机从语录中挑一句发送出去
@handleTelegramErrors(errorReply="左脑缺失运行库，右脑缺失指令集……一说话就丢包了喵……")
async def sendNya(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # getRandomQuote 返回消息列表
    messages: list = getRandomQuote()

    if not messages:
        await update.message.reply_text(
            "呜喵……？说不出话来……\n"
            "ご主人様——快来修修锌酱……\n"
            "@ZincPhos"
        )
        await logAction(
            update.effective_user,
            "来自 Telegram 的 /nya",
            "语录库缺失，无法发送",
            LogLevel.WARNING,
            LogChildType.WITH_ONE_CHILD
        )
        return

    # 发送消息（可能是单条或多条）
    for i, msg in enumerate(messages):
        if msg.strip():  # 跳过空消息
            await update.message.reply_text(msg)

            # 如果还有后续消息，等待一段时间再发送
            if i < len(messages) - 1:
                delay = round(random.uniform(MESSAGE_DELAY_MIN , MESSAGE_DELAY_MAX) , 2)
                await asyncio.sleep(delay)




def register():
    return {
        "handlers": [CommandHandler("nya", sendNya)],
        "name": "Nya 语录",
        "description": "随机发送 ZincNya 语录",
    }
