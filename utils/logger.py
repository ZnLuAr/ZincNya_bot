import os
from datetime import datetime
import asyncio
from config import LOG_DIR , LOG_FILE_TEMPLATE


_CURRENT_LOG_PATH = None




def getTodayLogPath():
    # 生成当天的日志文件路径，并计算本次开机为本日第几次开机
    
    os.makedirs(LOG_DIR , exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")

    # 找出今天已经有多少个日志
    existingLogs = [
        f for f in os.listdir(LOG_DIR) if f.startswith(f"log_{today}")
    ]
    index = len(existingLogs) + 1    # 在已有的日志数量上+1
    logFileName = LOG_FILE_TEMPLATE.format(date=today , index=index)
    return os.path.join(LOG_DIR , logFileName)




def initLogger():
    # 用于初始化日志系统（主程序启动时调用）
    
    global _CURRENT_LOG_PATH
    _CURRENT_LOG_PATH = getTodayLogPath()

    with open(_CURRENT_LOG_PATH , "a" , encoding="utf-8") as f:
        f.write(f"[{datetime.now().strftime('%H:%M:%S')}] ZincNya Bot—— 喵的一声，就启动啦——\n")

    print (f"\n\n锌酱、现在就要创建日志了喵——✍\n   · {_CURRENT_LOG_PATH}，\nこれからですよにゃー\n")





async def logAction(user , action , result="OK喵" , child="False"):

    # 当不存在日志时，初始化
    if _CURRENT_LOG_PATH is None:
        initLogger()

    timestamp = datetime.now().strftime("%H:%M:%S")
    userName = exetractUserName(user)

    match child:
        case "False":
            consoleText = f"[{timestamp}] @{userName}：{action}\n               └─┤ {result}\n" 
        case "withChild":
            consoleText = f"[{timestamp}] @{userName}：{action}\n               └─┤ {result}"
        case "withOneChild":
            consoleText = f"[{timestamp}] @{userName}：{action}\n               └─┤ {result}\n"
        case "childWithChild":
            consoleText = f"                   └─┤ {action}\n                         └─┤ {result}"
        case "lastChildWithChild":
            consoleText = f"                   └─┤ {action}\n                         └─┤ {result}\n"
        case "lastChild":
            consoleText = f"                   └─┤ {result}\n"    
        case "True":
            consoleText = f"                   └─┤ {result}"

    logLine = f"[{timestamp}] @{userName}：{action}\n[{timestamp}] @锌酱：Result：{result}\n"

    try:
        await asyncio.to_thread(writeLogSync , consoleText , logLine)
    except Exception as e:
        print(f"[{timestamp}] @锌酱：咦？日志写入失败了喵……？\n             └─┤ 报错在这里——{e}")




def writeLogSync(consoleText , logLine):
    with open (_CURRENT_LOG_PATH , "a" , encoding="utf-8") as f:
        f.write(logLine)
    print (consoleText)




# 添加自定义用户名支持（一般都是锌酱）
def exetractUserName(user):
    if user is None:
        return "Bot"
    if isinstance(user , str):
        return user.strip() or "Unknown"
    if isinstance(user , dict):
        return user.get("username") or user.get("first_name") or "Unknown"

    userName = getattr(user , "username" , None)
    if userName:
        return userName
    
    first = getattr(user , "first_name" , None)
    if first:
        return first
    
    return "Unknown"