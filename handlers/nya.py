from telegram import Update
from telegram.ext import CommandHandler, ContextTypes


from config import QUOTES_DIR
from utils.nyaQuoteManager import loadQuoteFile , getRandomQuote
from utils.logger import logAction




# 响应来自 Telegram 的 /nya 指令，随机从语录中挑一句发送出去
async def sendNya(update:Update , context:ContextTypes.DEFAULT_TYPE):
    quotes = loadQuoteFile()

    if not quotes:
        await update.message.reply_text(
            "呜喵……？说不出话来……\n"
            "ご主人様快来——咱找不到咱的脚本啦……\n"
        )
        await logAction(None , "来自 Telegram 的 /nya" , "quotes 缺失喵……" , "withOneChild")
        return
    
    selectedQuote = getRandomQuote()
    if not selectedQuote:
        await update.message.reply_text(
            "呜喵……？说不出话来……\n"
            "ご主人様——快来修修你的群猫……\n"
            "@ZincPhos"
        )
        await logAction(None , "来自 Telegram 的 /nya" , "chosenQuote 缺失喵……" , "withOneChild")

    msg = selectedQuote.replace("\\n" , "\n") if selectedQuote else ""
    await update.message.reply_text(msg)




def register():
    return {
        "handlers": [CommandHandler("nya", sendNya)],
        "name": "Nya 语录",
        "description": "随机发送 ZincNya 语录",
    }