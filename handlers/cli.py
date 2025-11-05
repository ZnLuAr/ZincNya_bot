from telegram import Update , Bot
import re
import asyncio
import shlex
import importlib
import os

from config import COMMAND_DIR
from utils.logger import logAction




async def handleConsoleCommand(app , cmdLine: str):

    args = shlex.split(cmdLine)
    if not args:
        return
    
    commandName = args[0].lstrip("/")  #去掉前缀 “/”
    commandArgs = args[1:]
    
    # 加载对应命令模块
    moudlePath = f"{COMMAND_DIR.replace('/', '.')}.{commandName}"
    if not os.path.exists(os.path.join(COMMAND_DIR , f"{commandName}.py")):
        print(f"\n/{commandName}……喵？\n锌酱……还没见过这条指令喵……？\n\n")
        return
    
    try:
        module = importlib.import_module(moudlePath)
        if hasattr(module , "execute"):
            result = await module.execute(app , commandArgs)
            if result == "SHUTDOWN":
                return "SHUTDOWN"
        else:
            print(f"❌ やばいー/{commandName} 文件中没有定义 execute(app, args) 函数喵！\n")
    except Exception as e:
        await logAction("Console" , f"/{commandName} {commandArgs}" , f"执行时出错了喵：{e}" , "withOneChild")