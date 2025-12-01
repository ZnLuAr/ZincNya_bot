"""
utils/whitelistManager.py

是用于响应外部对 data/whitelist.json 的操作（如调用、添加项）并对后者进行管理的模块

模块大致可分为两部分——内部函数与外部函数


================================================================================
内部函数，面向 data/whitelist.json 的操作
包含 ensureWhitelistFile、loadWhitelistFile、saveWhitelistFile 三个功能


当涉及到对 whitelist.json 的操作时，先调用 ensureWhitelistFile 检查文件是否存在。
  若路径不存在则初始化为默认格式：
    {
        "allowed": {},
        "suspended": {}
    }

loadWhitelistFile() 用于读取并返回白名单文件的  *完整字典结构* ，形如：

    {
        "allowed": {
            "123": {"comment": "..."}
        },
        "suspended": {}
    }

saveWhitelistFile() 用于保存完整的白名单结构到 whitelist.json
    ** data 必须是一个包含 allowed / suspended 字段的字典 **


================================================================================
外部函数，面向命令模块和 Bot
包含：
    whetherAuthorizedUser、userOperation、
    checkChatAvailable、collectWhitelistViewModel、whitelistUIRenderer
五个功能。
其中，第二行的三个函数主要面向外部对 whitelist.json 的可视化操作，即 UI 相关操作。


whetherAuthorizedUser() 接受输入 userID: int | str ，**并返回一个 bool 值**
    输入函数的参数，无论是 int 还是 str，最终都按照字符串进行比较。


userOperation() 是统一的白名单操作入口。
    函数分别接受
        - 操作的类型（operation: str）
        - 对象 uuid（userID: str | None），默认为 None
        - 对象备注（comment: str），默认为 None

    operation 可能的取值有：
        - "addUser":        添加用户到 allowed
        - "deleteUser":     将用户从 allowed 中移除
        - "suspendUser":    将用户从 allowed 移入 suspended，体现为用户权限被挂起
        - "listUser":       返回完整的白名单结构（ -> dict ）
        - "setComment":     添加 / 修改用户备注

    函数的返回值视操作而定：
        - bool: True / False    表示操作是否成功
        - dict                  在 listUser 时返回白名单完整结构


UI 相关的 3 个函数——

checkChatAvailable() 用于检测白名单中的用户是否可以进行聊天。
    它接受 bot: Bot 和内部或外部输入的 uid: str，
    通常被 collectWhitelistViewModel() 调用并异步运行。
    函数视情况返回两种类型的值：
        - 当可以向用户发送信息时，返回布尔值 True
        - 当遇见 Forbidden、BadRequest 及其它原因时返回 Exception: str
    返回值通常被 collectWhitelistViewModel() 调用，来判断与用户的聊天界面是否可用


collectWhitelistViewModel() 一般用于构建 whitelistUIRenderer 所需的数据类型，
    并返回一个 entries  **列表** ，形如：
    {
        "uid":          "12345",
        "status":       "Allowed" 或 "Suspended",
        "comment":      "备注文本",
        "available":    True / False
    }
    ——用于渲染白名单列表界面。


whitelistUIRenderer() 将 collectWhitelistViewModel 生成的 entries 渲染为 Rich 表格
    显示的内容包括：
        - 序号
        - UID           当 Available 时高亮，其他情况下为灰色
        - 状态          Allowed / Suspended
        - 备注预览

    函数接受通常来自 collectWhitelistViewModel() 的 entries: list

"""




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




# 内部函数，面向 data/whitelist.json 的操作
# =============================================================================
def ensureWhitelistFile():
    '''
    当涉及到对 whitelist.json 的操作时，先调用 ensureWhitelistFile 检查文件是否存在。
        若路径不存在则初始化为默认格式：

        {
            "allowed": {},
            "suspended": {}
        }
    '''

    os.makedirs(os.path.dirname(WHITELIST_DIR) , exist_ok=True)
    if not os.path.exists(WHITELIST_DIR):
        saveWhitelistFile({
            "allowed": {},
            "suspended": {}
        })




def loadWhitelistFile():
    '''
    loadWhitelistFile() 用于读取并返回白名单文件的  *完整字典结构* ，形如：

        {
            "allowed": {
                "123": {"comment": "..."}
            },
            "suspended": {}
        }
    '''

    ensureWhitelistFile()
    with open(WHITELIST_DIR , "r" , encoding="utf-8") as f:
        return json.load(f)




def saveWhitelistFile(data):
    '''
    saveWhitelistFile() 用于保存完整的白名单结构到 whitelist.json
    ** data 必须是一个包含 allowed / suspended 字段的字典 **
    '''

    with open(WHITELIST_DIR , "w" , encoding="utf-8") as f:
        json.dump(data , f , ensure_ascii=False , indent=2)




# 外部函数，面向命令模块和 Bot
# =============================================================================
def whetherAuthorizedUser(userID: int | str) -> bool:
    '''
    whetherAuthorizedUser() 接受输入 userID: int | str ，
    
    **并返回一个 bool 值**
    
    输入函数的参数，无论是 int 还是 str，最终都按照字符串进行比较。
    '''

    data = loadWhitelistFile()
    userID = str(userID)
    return userID in data.get("allowed" , {}) and userID not in data.get("suspended" , {})




def userOperation(operation , userID:str|None=None , comment=None) -> bool | dict:
    '''
    userOperation() 是统一的白名单操作入口。
        函数分别接受
            - 操作的类型（operation: str）
            - 对象 uuid（userID: str | None），默认为 None
            - 对象备注（comment: str），默认为 None

        operation 可能的取值有：
            - "addUser":        添加用户到 allowed
            - "deleteUser":     将用户从 allowed 中移除
            - "suspendUser":    将用户从 allowed 移入 suspended，体现为用户权限被挂起
            - "listUser":       返回完整的白名单结构（ -> dict ）
            - "setComment":     添加 / 修改用户备注

        函数的返回值视操作而定：
            - bool: True / False    表示操作是否成功
            - dict                  在 listUser 时返回白名单完整结构

    '''

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




# UI 相关函数 ↓
async def checkChatAvailable(bot: Bot , uid: str):
    '''
    checkChatAvailable() 用于检测白名单中的用户是否可以进行聊天。
    它接受 bot: Bot 和内部或外部输入的 uid: str，
    通常被 collectWhitelistViewModel() 调用并异步运行。
    函数视情况返回两种类型的值：
        - 当可以向用户发送信息时，返回布尔值 True
        - 当遇见 Forbidden、BadRequest 及其它原因时返回 Exception: str
    返回值通常被 collectWhitelistViewModel() 调用，来判断与用户的聊天界面是否可用
    '''

    try:
        await bot.get_chat(uid)
        return True
    except (Forbidden , BadRequest) as e:
        return e
    except Exception as e:
        return e
    



async def collectWhitelistViewModel(bot: Bot):
    '''
    collectWhitelistViewModel() 一般用于构建 whitelistUIRenderer 所需的数据类型，
        并返回一个 entries  **列表** ，形如：
        {
            "uid":          "12345",
            "status":       "Allowed" 或 "Suspended",
            "comment":      "备注文本",
            "available":    True / False
        }
        ——用于渲染白名单列表界面。
    '''

    whitelistData = loadWhitelistFile()
    entries = []

    raw = []
    for uid , obj in whitelistData.get("allowed" , {}).items():
        raw.append((uid , "Allowed" , obj.get("comment" , "")))

    for uid , obj in whitelistData.get("suspended" , {}).items():
        raw.append((uid , "Suspended" , obj.get("comment" , "")))

    # 从一堆 ('<ID>' , 'Status' , <Comment>)中取得前面的 IDs
    uids = [x[0] for x in raw]
    results = await asyncio.gather(*[
        checkChatAvailable(bot , uid) for uid in uids
    ])

    for (uid , status , comment) , available in zip(raw , results):
        entries.append({
            "uid": uid,
            "status": status,
            "comment": comment,
            "available": available,
        })

    return entries


def whitelistUIRenderer(entries: list):
    '''
    whitelistUIRenderer() 将 collectWhitelistViewModel 生成的 entries 渲染为 Rich 表格
        显示的内容包括：
            - 序号
            - UID           当 Available 时高亮，其他情况下为灰色
            - 状态          Allowed / Suspended
            - 备注预览

    函数接受通常来自 collectWhitelistViewModel() 的 entries: list
    '''

    console = Console()

    table = Table(title="\n正在查看白名单喵——\n")
    table.add_column("No." , justify="right")
    table.add_column("UID" , justify="left")
    table.add_column("状态" , justify="left")
    table.add_column("备注" , justify="left")

    for i , e in enumerate(entries , 1):
        uid = e["uid"]
        colour = "wheat1" if e["available"] is True else "grey70"
        uidRendered = f"[{colour}]{uid}[/]"

        comment = e["comment"] or ""
        if comment.strip() == "":
            commentPreview = ""
        else:
            commentPreview = comment[:15] + ("..." if len(comment) > 15 else "")

        table.add_row(
            str(i),
            uidRendered,
            e["status"],
            commentPreview
        )

    console.print(table , "\n\n")




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
    