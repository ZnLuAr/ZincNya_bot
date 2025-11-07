import re

from utils.whitelistManager import userOperation
from utils.logger import logAction




async def execute(app , args):

    joined = " ".join(args)

    addMatch = re.search(r'--?(?:a|add)\s+(?:"([^"]+)"|(\S+))', joined)
    delMatch = re.search(r'--?(?:d|del)\s+(?:"([^"]+)"|(\S+))', joined)
    susMatch = re.search(r'--?(?:s|sus)\s+(?:"([^"]+)"|(\S+))', joined)
    listMatch = re.search(r'--?(?:l|list)', joined)

    def _extract(match):
        if not match:
            return None
        return match.group(1) or match.group(2)
    
    addedUser = _extract(addMatch)
    deletedUser = _extract(delMatch)
    suspendedUser = _extract(susMatch)

    if sum(bool(x) for x in [addedUser , deletedUser , suspendedUser , listMatch]) > 1:
        print("もー、/whitelist 只能有一种参数喵——\n")
        return

    if addedUser or deletedUser or suspendedUser or listMatch:
        if addedUser:
            operation = "addUser"
            action = f"将 {addedUser} 加入白名单……"
            result = ["OK喵" , f"{addedUser} 已经在白名单了喵——"]

        elif deletedUser:
            operation = "deleteUser"
            action = f"将 {deletedUser} 移出白名单……"
            result = ["OK喵" , f"{deletedUser} 不在白名单喵——"]
        elif suspendedUser:
            operation = "suspendUser"
            action = f"暂停 {suspendedUser} 的权限……"
            result = ["OK喵" , f"{suspendedUser} 已经是暂停状态了喵……"]
        elif listMatch:
            data = userOperation("listUsers")
            allowedUser = data["allowed"]
            suspendedUser = data["suspended"]
            whitelistUIRenderer(allowedUser , suspendedUser)
            return


        userID = addedUser or deletedUser or suspendedUser
        ok = userOperation(operation , userID)
        finalResult = result[0] if ok else result[1]
        await logAction("Console" , action , finalResult , "withOneChild")
        return

    print("もー、参数错误喵——")
    print("使用 '/help whitelist' 查看 /whitelist 的详细用法哦——\n")




def whitelistUIRenderer(allowedUser , suspendedUser):
    print("\n当前有——")
    
    if not allowedUser and not suspendedUser:
        print("\n     啊……没有呢喵……")
        return
    
    # 计算最大长度，保证表头对齐
    maxLen = max(len(allowedUser) , len(suspendedUser))
    headerLeft = "白名单"
    headerRight = "暂停名单"

    leftMax = max((len(u) for u in allowedUser) , default=0)
    rightMax = max((len(u) for u in suspendedUser) , default=0)
    colWidthLeft = max(len(headerLeft) , leftMax) + 4
    colWidthRight = max(len(headerRight) , rightMax) + 4

    print(f"{'·':<5}{headerLeft:<{colWidthLeft - 3}}┃ {headerRight:<{colWidthRight}}")
    print("—" * (colWidthLeft + colWidthRight + 10))

    for i in range(maxLen):
        left = allowedUser[i] if i < len(allowedUser) else ""
        right = suspendedUser[i] if i < len(suspendedUser) else ""
        print(f"{i+1:<5}{left:<{colWidthLeft}}┃ {right:<{colWidthRight}}")
    
    print("\n\n\n以上喵——\n\n")



def getHelp():
    return {

        "name": "/whitelist",
        "description": "管理 ZincNya bot 使用的白名单",
        "usage": "/whitelist [-a/--add <id>] [-d/--del <id>] [-s/--sus <id>] [-l/--list]\n用户或群聊的ID需要在 Telegram 的 @myidbot 中获取哦。",
        "example": (
            "添加用户至白名单：/whitelist -a 12345678\n"
            "从白名单移除用户：/whitelist -d 12345678\n"
            "暂停用户的访问权限：/whitelist -s 12345678\n"
            "查看白名单列表：/whitelist -l"
        ),
        
    }