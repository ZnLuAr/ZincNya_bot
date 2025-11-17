from telegram import Update
from telegram.ext import CommandHandler, ContextTypes


from config import QUOTES_DIR
from utils.command.nya import JsonOperation , WeitghtedRandom
from utils.logger import logAction


# 响应来自 Telegram 的 /nya 指令，随机从语录中挑一句发送出去
async def sendNya(update:Update , context:ContextTypes.DEFAULT_TYPE):
    quotes = JsonOperation.loadQuotesFromJson()

    if not quotes:
        await update.message.reply_text(
            "呜喵……？说不出话来……\n"
            "ご主人様快来——咱找不到咱的脚本啦……\n"
        )
        await logAction(None , "来自 Telegram 的 /nya" , "quotes 缺失喵……" , "withOneChild")
        return
    
    chosenQuote = WeitghtedRandom.cdfCalc(quotes)
    if not chosenQuote:
        await update.message.reply_text(
            "呜喵……？说不出话来……\n"
            "ご主人様——快来修修你的群猫……\n"
        )
        await logAction(None , "来自 Telegram 的 /nya" , "chosenQuote 缺失喵……" , "withOneChild")

    msg = chosenQuote.get("text" , "").replace("\\n" , "\n")
    await update.message.reply_text(msg)




def register():
    return [CommandHandler("nya" , sendNya)]