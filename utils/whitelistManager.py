import os
import json
import asyncio
from rich.table import Table
from rich.console import Console
from telegram import Update , Bot
from telegram.ext import CommandHandler, ContextTypes
from telegram.error import Forbidden , BadRequest

from config import WHITELIST_DIR
from utils.logger import logAction




# 内部函数，面向 Whitelist.json 的操作
# ====================================================================
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
# ====================================================================
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




# 用于渲染白名单列表的函数
# ====================================================================
async def checkChatAvailable(bot: Bot , uid: str):
    try:
        await bot.get_chat(uid)
        return True
    except (Forbidden , BadRequest) as e:
        return e
    except Exception as e:
        return e
        

async def collectWhitelistViewModel(bot: Bot):
    """
    返回一个可直接给 whitelistUIRenderer 使用的 entries 列表：
    [
        {
            "uid": "12345",
            "status": "Allowed",
            "comment": "...",
            "available": True/False
        },
        ...
    ]
    """
    data = loadWhitelistFile()
    entries = []

    raw = []
    for uid , obj in data.get("allowed" , {}).items():
        raw.append((uid , "Allowed" , obj.get("comment" , "")))

    for uid , obj in data.get("suspended" , {}).items():
        raw.append((uid , "Suspended" , obj.get("comment" , "")))

    uids = [x[0] for x in raw]
    results = await asyncio.gather(*[
        checkChatAvailable(bot , uid) for uid in uids
    ])

    for (uid , status , comment), available in zip(raw , results):
        entries.append({
            "uid": uid,
            "status": status,
            "comment": comment,
            "available": available,
        })

    return entries


def whitelistUIRenderer(entries: list):
    console = Console()

    table = Table(title="正在查看白名单喵——\n")
    table.add_column("No." , justify="right")
    table.add_column("UID" , justify= "left")
    table.add_column("状态" , justify="left")
    table.add_column("备注" , justify="left")

    for i , e in enumerate(entries , 1):
        uid = e["uid"]
        colour = "green" if e["available"] else "grey50"
        uidRendered = f"[{colour}]{uid}[/]"

        comment = e["comment"] or ""
        if comment.strip() == "":
            commentPreview = ""
        else:
            # 过长的备注只显示前15个字，较短的备注直接显示
            if len(comment) > 15:
                commentPreview = f"{comment[:15]}..."
            else:
                commentPreview = comment

        table.add_row(
            str(i),
            uidRendered,
            e["status"],
            commentPreview
        )

    console.print(table , "\n\n")


'''
    因能力不足（懒）而不得以实现的、体现着彼时雄心的 tui 主循环……
    while True:
        if keyboard.is_pressed("down"):
            selected = min(selected + 1, len(entries) - 1)
            render()
            time.sleep(0.15)

        elif keyboard.is_pressed("up"):
            selected = max(selected - 1, 0)
            render()
            time.sleep(0.15)

        elif keyboard.is_pressed("enter"):
            entries[selected]["collapsed"] = not entries[selected]["collapsed"]
            render()
            time.sleep(0.15)

        elif keyboard.is_pressed("q"):
            break
'''