import asyncio
import os
import csv
import importlib
import difflib

from config import HELP_LIST_DIR , COMMAND_DIR
from utils.logger import logAction




async def execute(app , args):
    # 响应 /help 命令

    try:
        commandsList = []

        for file in os.listdir(COMMAND_DIR):
            if file.startswith("__") or not file.endswith(".py"):
                continue
            fileName = file[:-3]
            # （实现命令的）模块的路径，将目录路径 utils/command 转化为 utils.command
            moudlePath = f"{COMMAND_DIR.replace('/' , ".")}.{fileName}"
            try:
                mod = importlib.import_module(moudlePath)
                if hasattr(mod , "getHelp"):
                    # info 包含 getHelp 返回的"name" "description" "usage" "example" 信息
                    info = mod.getHelp()
                    commandsList.append(info)
            except Exception as e:
                print(f"\n❌ 加载 {fileName} 时出错了喵：{e}\n\n")

        # 无参数，即列出命令列表
        if not args:
            print("所有可用命令喵：")
            print("—" * 62 , "\n")
            # 按命令名排序 commands 列表，格式化打印各条命令的名称和简短描述（左对齐宽度15）
            for info in sorted(commandsList , key=lambda x: x["name"]):
                print(f"{info['name']:<15}      {info.get('description' , '')}")
            print("\n" , "—" * 61)
            print("使用 /help <command> 查看详细说明喵——\n\n")
            return
        
        # 有参数，打印对应命令信息
        command = args[0].lstrip("/")
        targetMoudlePath = f"{COMMAND_DIR.replace('/' , '.')}.{command}"
        try:
            # 尝试导入终端指定的命令模块（单个）
            mod = importlib.import_module(targetMoudlePath)
            if hasattr(mod , "getHelp"):
                info = mod.getHelp()
                print(f"\n\n{info['name']}        {info.get('description' , '')}")
                print(f"\n{info.get('usage' , 'ないです（即答')}\n")
                if info.get("example"):
                    print(f"{info['example']}")
                    print("—" * 87 , "\n\n")
            else:
                print(f"❌ 命令 /{command} 没有定义 getHelp() 函数喵……")
        except ModuleNotFoundError:
            print(f"\n❌ 没找到 /{command} 命令喵。")
            print("提示：输入 /help 查看所有可用命令\n\n")
    except Exception as e:
        await logAction(None , "执行 /help 时出错了喵……" , str(e) , "withOneChild")
        raise




def getHelp():
    return {

        "name": "/help",

        "description": "提供 ZincNya bot 命令的帮助信息",

        "usage": "/help [<command>]",

        "example": "展示此页面：/help\n获取 /shutdown 命令的帮助：/help shutdown"

    }