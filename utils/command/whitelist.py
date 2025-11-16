import re

from utils.whitelistManager import userOperation
from utils.logger import logAction




async def execute(app , args):

    joined = " ".join(args)

    addMatch = re.search(r'--?(?:a|add)\s+(?:"([^"]+)"|(\S+))', joined)
    delMatch = re.search(r'--?(?:d|del)\s+(?:"([^"]+)"|(\S+))', joined)
    susMatch = re.search(r'--?(?:s|sus)\s+(?:"([^"]+)"|(\S+))', joined)
    listMatch = re.search(r'--?(?:l|list)', joined)
    commentMatch = re.search(r'--?(?:c|comment)\s+(?:"([^"]+)"|(\S+))', joined)

    def _extract(match):
        if not match:
            return None
        return match.group(1) or match.group(2)
    
    addedUser = _extract(addMatch)
    deletedUser = _extract(delMatch)
    suspendedUser = _extract(susMatch)
    commentTarget = _extract(commentMatch)

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




def whitelistUIRenderer(whitelistData: dict):
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    import keyboard
    import os
    import time

    console = Console()

    entries = []
    for uid , data in whitelistData.get("allowed" , {}).items():
        entries.append(
            {
                "uid": uid,
                "status": "Allowed",
                "comment": data.get("comment" , ""),
                "collapsed": True
            }
        )

    for uid , data in whitelistData.get("suspended" , {}).items():
        entries.append(
            {
                "uid": uid,
                "status": "Suspended",
                "comment": data.get("comment" , ""),
                "collapsed": True
            }
        )
    
    entries.sort(key=lambda x: (x["status"] , x["uid"]))

    selected = 0

    def render():
        os.system("cls" if os.name == "nt" else "clear")

        table = Table(title="正在查看白名单喵——\n（↑↓ 移动，Enter 展开/折叠，q 退出）")
        table.add_column("No." , justify="right")
        table.add_column("UID" , justify= "left")
        table.add_column("状态" , justify="left")
        table.add_column("备注" , justify="left")


        for i , e in enumerate(entries):
            commentPreview = ""
            if e["comment"].strip() == "":
                commentPreview = ""
            else:
                if e["collapsed"]:
                    # 过长的备注只显示 “ > ”，较短的备注直接显示
                    preview = e["comment"]
                    if len(preview) > 15:
                        commentPreview = ">"
                    else:
                        commentPreview = preview
                else:
                    # 展开状态完整显示
                    commentPreview = e["comment"]
            
            if i == selected:
                # 给选中项加高亮
                table.add_row(
                    f"[bold yellow]{i+1}[/]",
                    f"[bold yellow]{e['uid']}[/]",
                    f"[bold yellow]{e['status']}[/]",
                    f"[bold yellow]{commentPreview}[/]"
                )
            else:
                table.add_row(
                    str(i+1),
                    e["uid"],
                    e["status"],
                    commentPreview
                )

        console.print(table , "\n\n")


    # UI渲染函数主循环
    render()

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