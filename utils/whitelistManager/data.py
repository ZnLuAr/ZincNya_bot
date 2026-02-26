"""
白名单数据层

IO 操作与业务逻辑：ensure/load/save、权限检查、CRUD 操作
"""

import os
import json
from typing import Optional

from config import WHITELIST_DIR
from utils.logger import logAction


def ensureWhitelistFile():
    os.makedirs(os.path.dirname(WHITELIST_DIR) , exist_ok=True)
    if not os.path.exists(WHITELIST_DIR):
        with open(WHITELIST_DIR , "w" , encoding="utf-8") as f:
            json.dump({"allowed": {} , "suspended": {}} , f , ensure_ascii=False , indent=2)


def loadWhitelistFile():
    from utils.core.fileCache import getWhitelistCache
    ensureWhitelistFile()
    cache = getWhitelistCache()
    return cache.get()


def saveWhitelistFile(data):
    from utils.core.fileCache import getWhitelistCache
    cache = getWhitelistCache()
    cache.set(data)


def whetherAuthorizedUser(userID: int | str) -> bool:
    data = loadWhitelistFile()
    userID = str(userID)
    return userID in data.get("allowed" , {}) and userID not in data.get("suspended" , {})


def userOperation(operation , userID:str|None=None , comment=None) -> bool | dict:

    data = loadWhitelistFile()
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

        case "unsuspendUser":
            if userID in data["suspended"]:
                data["allowed"][userID] = data["suspended"].pop(userID)
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
    user = update.effective_user
    userID = str(user.id)
    userName = user.username or "Unknown"
    name = f"{user.first_name} {user.last_name or ''}".strip()

    if not whetherAuthorizedUser(userID):
        print ("ご、ご主人様——")
        await logAction(None , f"有不认识的人尝试访问咱了……" , f"直接拒绝喵：{name}(@{userName} / ID：{userID})" , "withOneChild")
        return

    await update.message.reply_text("欢迎回来喵——")