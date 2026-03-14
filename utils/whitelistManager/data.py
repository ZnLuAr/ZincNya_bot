"""
白名单数据层

IO 操作与业务逻辑：ensure/load/save、权限检查、CRUD 操作、/start 鉴权与通知
"""

import os
import json
import time
from typing import Optional

from config import WHITELIST_PATH, Permission
from utils.operators import getOperatorsWithPermission
from utils.logger import logAction, LogLevel, LogChildType


# /start 通知冷却：同一用户 10 分钟内只通知一次
_NOTIFY_COOLDOWN = 600
_MAX_NOTIFY_CACHE = 4096    # 安全上限，正常情况不触发
_lastNotifyTime: dict[str, float] = {}




def ensureWhitelistFile():
    os.makedirs(os.path.dirname(WHITELIST_PATH) , exist_ok=True)
    if not os.path.exists(WHITELIST_PATH):
        with open(WHITELIST_PATH , "w" , encoding="utf-8") as f:
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
        # 冷却期内不重复通知 operator（防止刷 /start 轰炸）
        now = time.monotonic()
        lastTime = _lastNotifyTime.get(userID, 0)

        if now - lastTime > _NOTIFY_COOLDOWN:
            # 清理过期条目（语义上等同于"从未记录"，删除后若再触发会正确重新通知）
            expired = [k for k, t in _lastNotifyTime.items() if now - t > _NOTIFY_COOLDOWN]
            for k in expired:
                del _lastNotifyTime[k]
            # 安全上限兜底（正常情况不触发）
            if len(_lastNotifyTime) >= _MAX_NOTIFY_CACHE:
                del _lastNotifyTime[next(iter(_lastNotifyTime))]
            _lastNotifyTime[userID] = now
            notifyText = (
                f"有不认识的人碰到锌酱了喵——\n\n"
                f"用户：{name}\n"
                f"用户名：@{userName}\n"
                f"ID：{userID}"
            )
            for opID in getOperatorsWithPermission(Permission.NOTIFY):
                try:
                    await context.bot.send_message(chat_id=int(opID), text=notifyText)
                except Exception:
                    pass

        await logAction(
            update.effective_user,
            "未授权访问",
            f"{name}(@{userName} / {userID})",
            LogLevel.WARNING,
            LogChildType.WITH_ONE_CHILD
        )
        return

    await update.message.reply_text("欢迎回来喵——")