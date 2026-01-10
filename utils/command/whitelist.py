from telegram import Bot

from handlers.cli import parseArgsTokens
from utils.whitelistManager import (
    userOperation,
    whitelistUIRenderer,
    collectWhitelistViewModel,
    whitelistMenuController)
from utils.logger import logAction




async def execute(app , args):

    bot: Bot = app.bot

    parsed = {
        "add": None,
        "del": None,
        "sus": None,
        "list": None,
        "comment": [],       # --comment 有可能出现 -c <ID> <text> 情况，多元素需要使用列表
    }

    # Whitelist 中将缩写对应全称的字典
    argAlias = {
    "a": "add",
    "d": "del",
    "s": "sus",
    "l": "list",
    "c": "comment",
    }


    parsed = parseArgsTokens(parsed , args , argAlias)
    
    addUID = parsed["add"]
    delUID = parsed["del"]
    susUID = parsed["sus"]
    listFlag = parsed["list"]
    commentList: list = parsed["comment"]

    # 参数验证
    # 当四个参数中出现超过一个（设定的规则上不能同时使用）时，报错并返回
    if sum(bool(x) for x in [addUID , delUID , susUID , listFlag]) > 1:
        print(
                "もー、/whitelist 的参数不能这样用喵——\n",
                "详情要用 /help whitelist 来查看哦"
            )
        return


    def _extractCommentList(commentList):
        '''
        解析 -c 传入的内容。
            -c 所支持的格式包括：
                -c <'ID'> <'comment text...'>
                -a <'ID'> -c <'comment text'>
                -c (NoValue)

        因此，为了能让 parseArgsTokens() 能够正常读取，
            需要以列表的形式将 key=comment 传入 parseArgsTokens()\n
        parseArgsTokens() 返回的 "comment" 字典项也是列表形式。\n
        函数用于将 commentUID 和 commentText 从 commentList 分别解析出来
        '''

        if not commentList or commentList[0] == "NoValue":
            return None , None
        
        # 值得注意的是，当 -a 与 -c 合用时，-c 的参数值只有一项
        # 即 commentList = ['<obj>']，其中 obj 将被视为 commentText
        # 也就是说，列表的第一项，即 commentList[0]，将被赋值为 commentText
        commentUID = commentList[0] if (addUID and commentList) != "NoValue" else None
        if commentUID is None:
            commentText = commentList[0]
            return commentUID , commentText
        commentText = " ".join(commentList[1:]) if len(commentList) > 1 else None
        return commentUID , commentText


    if listFlag is not None or listFlag == "NoValue":
        # 使用交互式选择器管理白名单
        await whitelistMenuController(bot , app , mode="manage")
        return
    

    commentUID , commentText = _extractCommentList(commentList)
    
    # 当独立使用 -c/--comment 参数时
    if commentUID and not (addUID or delUID or susUID):
        if commentText is None:
            print("ムリー，备注内容不能为空喵——\n")
            return
        ok = userOperation("setComment" , commentUID , commentText)
        action = f"为 {commentUID} 添加备注 '{commentText}' ……"
        result = "OK喵——" if ok else "备注添加失败喵……"
        await logAction("Console" , action , result ,"withOneChild")
        return

    # 以下是包含了-a、-d、-s 的使用情况
    if (addUID or delUID or susUID or listFlag) and (addUID and delUID and susUID and listFlag) != "NoValue":
        if addUID:
            operation = "addUser"
            action = f"将 {addUID} 加入白名单……"
            if commentText:
            # -a 和 -c 同时使用的情况
            # 在这个分支就结束操作并返回，不到达下面的汇总操作
                # Step 1：添加用户 ID 进入白名单
                ok = userOperation(operation , addUID)
                result = "OK喵" if ok else f"{addUID} 已经在白名单了喵——"
                if not ok:
                    # 当添加 UID 不成功时，返回“已经在白名单了”——这种无意义的消息可以不出现在日志中
                    await logAction("Console" , action , result , "withChild" , False)
                    return
                await logAction("Console" , action , result , "withChild")
                # Step 2：添加备注
                # 所以要使 comment
                ok = userOperation("setComment" , addUID , commentText)
                result = f"已为 {addUID} 添加备注 {commentText} 喵——" if ok else f"备注添加失败喵……"
                await logAction("Console" , action , result , "lastChild")
                return
            result = ["OK喵" , f"{addUID} 已在白名单喵——"]

        elif delUID:
            operation = "deleteUser"
            action = f"将 {delUID} 移出白名单……"
            result = ["OK喵" , f"{delUID} 不在白名单喵——"]

        elif susUID:
            operation = "suspendUser"
            action = f"暂停 {susUID} 的权限……"
            result = ["OK喵" , f"{susUID} 已经是暂停状态了喵……"]

        elif listFlag:
            # 当操作为 --list 时，不进入汇总操作，而是调用渲染函数后返回
            whitelistData: dict = userOperation("listUsers")
            whitelistUIRenderer(whitelistData)
            return
        
        # 汇总操作
        userID = addUID or delUID or susUID
        ok = userOperation(operation , userID)
        finalResult = result[0] if ok else result[1]
        await logAction("Console" , action , finalResult , "withOneChild")
        return

    print("やばいー、参数错误喵——")
    print("使用 '/help whitelist' 查看 /whitelist 的详细用法哦——\n")




def getHelp():
    return {

        "name": "/whitelist",
        
        "description": "管理 ZincNya bot 使用的白名单",

        "usage": (
            "/whitelist [-a/--add <id>] [-d/--del <id>] [-s/--sus <id>] [-l/--list] [-c/--comment <id> <comment>]\n"
            "用户或群聊的ID需要在 Telegram 的 @myidbot 中获取哦。\n"
            "不过要注意——\n"
            "--add、--del、--sus、--list 参数不能同时存在的……！"
        ),

        "example": (
            "添加用户至白名单：/whitelist -a 12345678\n"
            "为白名单内的用户添加备注：/whitelist -c 12345678 我做东方鬼畜音mad，好吗？\n"
            "从白名单移除用户：/whitelist -d 12345678\n"
            "暂停用户的访问权限：/whitelist -s 12345678\n"
            "查看白名单列表：/whitelist -l\n"
            "添加用户至白名单并给予备注：/whitelist -a 12345678 -c 我做东方鬼畜音mad，好吗？"
        ),
        
    }