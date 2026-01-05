import asyncio
import random
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes


from config import QUOTES_DIR
from utils.nyaQuoteManager import loadQuoteFile , getRandomQuote
from utils.logger import logAction


# 多条消息之间的发送间隔（秒，精确到一位小数）
MESSAGE_DELAY = round(random.uniform(1 , 3) , 2)




# 响应来自 Telegram 的 /nya 指令，随机从语录中挑一句发送出去
async def sendNya(update: Update, context: ContextTypes.DEFAULT_TYPE):
    quotes = loadQuoteFile()

    if not quotes:
        await update.message.reply_text(
            "呜喵……？说不出话来……\n"
            "ご主人様快来——咱找不到咱的脚本啦……\n"
        )
        await logAction(None, "来自 Telegram 的 /nya", "quotes 缺失喵……", "withOneChild")
        return

    # getRandomQuote 返回消息列表
    messages: list = getRandomQuote()

    if not messages:
        await update.message.reply_text(
            "呜喵……？说不出话来……\n"
            "ご主人様——快来修修你的群猫……\n"
            "@ZincPhos"
        )
        await logAction(None, "来自 Telegram 的 /nya", "chosenQuote 缺失喵……", "withOneChild")
        return

    # 发送消息（可能是单条或多条）
    for i, msg in enumerate(messages):
        if msg.strip():  # 跳过空消息
            await update.message.reply_text(msg)

            # 如果还有后续消息，等待一段时间再发送
            if i < len(messages) - 1:
                await asyncio.sleep(MESSAGE_DELAY)




def register():
    return {
        "handlers": [CommandHandler("nya", sendNya)],
        "name": "Nya 语录",
        "description": "随机发送 ZincNya 语录",
    }
