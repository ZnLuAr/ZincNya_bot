import re
from telegram import Bot

from handlers.cli import parseArgsTokens
from utils.whitelistManager import userOperation , whitelistUIRenderer
from utils.logger import logAction




async def execute(app , args):

    bot: Bot = app.bot

    parsed = {
        "add": None,
        "del": None,
        "sus": None,
        "list": None,
        "comment": None
    }

    parsed = parseArgsTokens(parsed , args)
    
    addedTarget = parsed["add"]
    deletedTarget = parsed["del"]
    suspendedTarget = parsed["sus"]
    commentTarget = parsed["comment"]
    listMatch = parsed["list"]

    # 参数验证
    # 当四个参数中出现超过一个（设定的规则上不能同时使用）时，报错并返回
    if sum(bool(x) for x in [
        addedTarget,
        deletedTarget,
        suspendedTarget,
        listMatch
    ]) > 1:
        print("もー、/whitelist 只能有一种参数喵——\n")
        return
    

        



'''
    if sum(bool(x) for x in [addedUser , deletedUser , suspendedUser , listMatch]) > 1:
        print("もー、/whitelist 只能有一种参数喵——\n")
        return
    
    # 当独立使用 -c/--comment 参数时（在 args 中 comment 会作为额外参数出现）
    if commentTarget and not addedUser:
        # 此处 commentTarget 实际上是用户 ID
        userID = commentTarget
        # 提取 comment 内容至 ["-c" , "<userID>" , "<comment>"] 格式
        try:
            idx = args.index("-c") if "-c" in args else args.index("--comment")
        except ValueError:
            print("\n参数解析失败喵……\n")
            return
        try:
            # comment 为 -c "<ID>" 之后的内容
            comment = " ".join(args[idx + 2:])
        except:
            comment = None
        if not comment:
            print("もー，备注内容不能为空的喵——\n")
            return
        ok = userOperation("setComment" , userID , comment)
        action = f"为 {userID} 添加备注 '{comment}' ……"
        result = f"OK喵——" if ok else f"备注添加失败喵……"
        await logAction("Console" , action , result , "withChild")
        return

    # 以下是包含了-a、-d、-s 的使用情况
    if addedUser or deletedUser or suspendedUser or listMatch:
        if addedUser:
            operation = "addUser"
            action = f"将 {addedUser} 加入白名单……"
            result = ["OK喵" , f"{addedUser} 已经在白名单了喵——"]
            if commentTarget:
            # -a 和 -c 同时使用的情况下
                # Step 1：添加用户 ID 进入白名单
                ok = userOperation(operation , addedUser)
                if not ok:
                    await logAction("Console" , action , result[1] , "withChild" , False)
                    return
                await logAction("Console" , action , result[0] , "withChild")
                # Step 2：添加备注
                comment = commentTarget
                result = f"已为 {addedUser} 添加备注 {comment} 喵——" if ok else f"备注添加失败喵……"
                ok = userOperation("setComment" , addedUser , comment)
                await logAction("Console" , action , result , "lastChild")
                return
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
            whitelistUIRenderer(data)
            return


        userID = addedUser or deletedUser or suspendedUser
        ok = userOperation(operation , userID)
        finalResult = result[0] if ok else result[1]
        await logAction("Console" , action , finalResult , "withOneChild")
        return

    print("もー、参数错误喵——")
    print("使用 '/help whitelist' 查看 /whitelist 的详细用法哦——\n")
'''



def getHelp():
    return {

        "name": "/whitelist",
        
        "description": "管理 ZincNya bot 使用的白名单",

        "usage": (
            "/whitelist [-a/--add <id>] [-d/--del <id>] [-s/--sus <id>] [-l/--list] [-c/--comment <id> <comment>]\n"
            "用户或群聊的ID需要在 Telegram 的 @myidbot 中获取哦。"
        ),

        "example": (
            "添加用户至白名单：/whitelist -a 12345678\n"
            "为白名单内的用户添加备注：/whitelist -c 12345678 我做东方鬼畜音mad，好吗？"
            "从白名单移除用户：/whitelist -d 12345678\n"
            "暂停用户的访问权限：/whitelist -s 12345678\n"
            "查看白名单列表：/whitelist -l"
            "添加用户至白名单并给予备注：/whitelist -a 12345678 -c 我做东方鬼畜音mad，好吗？"
        ),
        
    }