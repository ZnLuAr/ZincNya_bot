import os
import json
import asyncio
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from config import WHITELIST_DIR
from utils.logger import logAction




# 内部函数，面向 Whitelist.json 的操作

def ensureWhitelistFile():
    os.makedirs(os.path.dirname(WHITELIST_DIR) , exist_ok=True)
    if not os.path.exists(WHITELIST_DIR):
        saveWhitelistFile({
            "allowed": {},
            "suspended": {}
        })
            

def loadWhitelistFile():
    ensureWhitelistFile()
    with open(WHITELIST_DIR , "r" , encoding="utf-8") as f:
        return json.load(f)


def saveWhitelistFile(data):
    with open(WHITELIST_DIR , "w" , encoding="utf-8") as f:
        json.dump(data , f , ensure_ascii=False , indent=2)




# 外部函数，面向命令模块或 bot 调用
def whetherAuthorizedUser(userID: int | str) -> bool:
    data = loadWhitelistFile()
    userID = str(userID)
    return userID in data["allowed"] and userID not in data["suspended"]




def userOperation(operation , userID:str|None=None , comment=None) -> bool | dict:
    data =loadWhitelistFile()
    userID = str(userID) if userID else None

    match operation:
        case "addUser":
            if userID not in data["allowed"]:
                data["allowed"][userID] = {"comment": ""}
                saveWhitelistFile(data)
                return True
            return False
        
        case "deleteUser":
            if userID in data["allowed"]:
                data["allowed"].pop(userID)
                saveWhitelistFile(data)
                return True
            return False
        
        case "suspendUser":
            if userID in data["allowed"] and userID not in data["suspended"]:
                data["suspended"][userID] = data["allowed"].pop(userID)
                saveWhitelistFile(data)
                return True
            return False
        
        case "listUsers":
            return dict(data)
        
        case "setComment":
            if userID in data["allowed"]:
                data["allowed"][userID]["comment"] = comment
            elif userID in data["suspended"]:
                data["suspended"][userID]["comment"] = comment
            else:
                return False
            saveWhitelistFile(data)
            return True
        
        case _:
            raise ValueError(f"未知的操作类型喵：{operation}")



async def handleStart(update , context):
# Telegram /start 入口
    user = update.effective_user
    userID = str(user.id)
    userName = user.username or "Unknown"
    name = f"{user.first_name} {user.last_name or ''}".strip()

    if not whetherAuthorizedUser(userID):
        print ("ご、ご主人様——")
        await logAction(None , f"有不认识的人尝试访问咱了……" , f"直接拒绝喵：{name}(@{userName} / ID：{userID})" , "withOneChild")
        return
    await update.message.reply_text("欢迎回来喵——")
    