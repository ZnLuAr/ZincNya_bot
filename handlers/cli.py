from telegram import Update , Bot
import re
import asyncio
import shlex
import importlib
import inspect
import os

from config import COMMAND_DIR
from utils.logger import logAction




async def handleConsoleCommand(app , cmdLine: str):

    args = shlex.split(cmdLine)
    
    commandName = args[0].lstrip("/")  #去掉前缀 “/” 和后面跟着的东西
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
            if inspect.isawaitable(result):
                result = await result
            return result
        else:
            print(f"❌ やばいー/{commandName} 文件中没有定义 execute 函数喵！\n")
    except Exception as e:
        await logAction("Console" , f"/{commandName}" , f"执行时出错了喵：{e}" , "withOneChild")




def parseArgsTokens(parsed: dict , tokens: list[str]):
    """
    通用参数解析函数
    支持格式:
      --flag value
      -f value
      --flag="value with spaces"
      --flag "value with spaces"
      重复参数（自动追加列表）例如 --id 123 --id 456
    """

    i = 0
    while i < len(tokens):
        t = tokens[i]

        # “不是我认识的‘-’开头，直接跳过”
        if not t.startswith("-"):
            i += 1
            continue

        key = t.lstrip("-")

        # 如果用户只写了 -f 或 --flag（没有 value ），允许 flag=None，继续
        # 若下一个 token 不存在或以 - 开头，则认为没有参数值
        value = None
        if i + 1 <len(tokens) and not tokens[i+1].startswith("-"):
            value = tokens[i+1]
            i += 1  # 吞掉参数值

        if key not in parsed:
            i += 1
            continue

        # 重复参数自动列表追加
        if isinstance(parsed[key] , list):
            if value is not None:
                parsed[key].append(value)

        else:
            if key is not None and value is None:
                value = "NoValue"
            parsed[key] = value

        i += 1

    return parsed