from telegram import Update
from telegram.ext import CommandHandler, ContextTypes
import random
import csv


from config import *
from utils.logger import logAction


def loadQuotes():
    try:
        with open(QUOTES_PATH, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            quotes = [row[0].strip() for row in reader if row]
        return quotes
    except Exception as e:
        logAction("锌酱" , "呜喵？引用语录的时候出现问题了……" , f"报错在这里——{e}")
        return ["呜喵……？\nご主人様——快来修修你的群猫……"]
     

# 提前加载，避免每次命令都读文件
NYA_QUOTES = loadQuotes()




# 响应 /nya 指令，随机从锌猫语录中挑一句
async def sendNya(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not NYA_QUOTES:
        await update.message.reply_text(
            f"呜喵……？说不出话来……\n"
            f"ご主人様——快来修修你的群猫……"
            )
        return
    

    quote = random.choice(NYA_QUOTES)
    quote = quote.replace("\\n" , "\n")
    await update.message.reply_text(quote)




def register():
    return [CommandHandler("nya" , sendNya)]