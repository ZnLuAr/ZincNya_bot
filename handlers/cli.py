"""
handlers/cli.py

用于处理来自 bot.py 控制台输入的指令解析与指令路由的模块。

模块可分为两部分：命令读取/分发（handleConsoleCommand）与参数解析（parseArgsTokens）。


================================================================================
命令读取/分发部分：handleConsoleCommand()

handleConsoleCommand() 负责解析 bot.py 传入的控制台字符串，并根据用户输入的指令名，
动态加载位于 config.COMMAND_DIR 下的对应模块（如 /whitelist 对应 whitelist.py）。

函数的基本流程如下：
    1. 使用 shlex.split() 将控制台输入拆分为 token 列表
    2. 将首个 token 视为指令名（去除开头的 '/'）
    3. 按路径构造模块名称并判断文件是否存在
    4. 使用 importlib.import_module() 导入对应指令模块
    5. 若模块中定义了 execute(app, args)，则调用并异步执行
    6. 将执行中发生的错误交由 logAction 记录日志

若用户输入的指令不存在或模块加载失败，将以友好的提示反馈到控制台。


================================================================================
参数解析部分：parseArgsTokens()

parseArgsTokens() 是通用参数解析器，供各指令模块调用。
    它接受输入：
    - parsed: dict        各指令模块定义的“参数结构模板”
    - tokens: list[str]   需要解析的 token 序列
    - aliasMap: dict      （可选）缩写参数映射表，如 {'a': 'add'}

原始 parsed 通常形如：
    {
        "at": None,
        "id": [],
        "text": None,
        "chat": None
    }
最终返回填充了值的 parsed 字典。

支持解析的输入格式包括：
    - -f value1 value2
    - --flag value
    - --flag=value
    - -id 123 456 789
    并自动忽略非以“-”开头的 token。

关于参数解析行为的要点：
    - 若 aliasMap 存在且输入为缩写（如 -a），会被替换为对应的全称键（如 add）
    - 若 parsed[key] 是 list，则追加读取到的多个值
    - 若 parsed[key] 是其他类型，则仅接收第一个值
    - 若参数未显式带值，会返回 ["NoValue"] 作为占位

该解析器是所有命令模块共享的基础能力，使得 Bot 的指令系统能够以统一、可扩展的方式处理各种输入。
"""




from telegram import Bot
import shlex
import importlib
import inspect
import os

from config import COMMAND_DIR
from utils.logger import logAction




async def handleConsoleCommand(app , commandLine: str):
    '''
    解析自主文件 bot.py 传来的控制台输入
    如若是有效的命令，则分发给对应模块。

    cli.py 最后向主文件传回命令执行的结果。
    '''

    args = shlex.split(commandLine)
    if not args:
        return
    
    commandName = args[0].lstrip("/")  #去掉前缀 “/”
    commandArgs = args[1:]
    
    # 加载对应命令模块
    moudlePath = f"{COMMAND_DIR.replace('/', '.')}.{commandName}"
    filePath = os.path.join(COMMAND_DIR , f"{commandName}.py")

    if not os.path.exists(filePath):
        print(f"\n/{commandName}……喵？\n锌酱……还没见过这条指令喵……？\n\n")
        return
    
    try:
        module = importlib.import_module(moudlePath)
    except Exception as e:
        print(f"导入 /{commandName} 时发生错误喵：{e}\n")
        return
    
    # 最终执行命令
    if hasattr(module , "execute"):
        try:
            result = await module.execute(app , commandArgs)
            if result == "SHUTDOWN":
                return "SHUTDOWN"
            if inspect.isawaitable(result):
                await result
        except Exception as e:
            await logAction("Console" , f"/{commandName} {commandArgs}" , f"执行时出错了喵：{e}" , "withOneChild")
    else:
            print(f"❌ やばいー/{commandName} 模块中没有定义 execute(app, args) 函数喵！\n")




def parseArgsTokens(parsed: dict , tokens: list[str] , aliasMap: dict|None=None):

    '''
    通用参数解析函数

    接受来自各指令模块的 parsed:dict

    原始 parsed 形如：
        {"at": None, "text": None, "id": [], "chat":None}

    最后返回填充了各个参数值的 parsed


    支持格式：
      -f value1 value2 value3
      --flag value
      --flag=value
      -id 123 456 789

    非参数（不以 '-' 开头的 token）会被忽略。
    '''

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        key = tok.lstrip("-")
        values = []  # 参数值

        # 跳过不以“-”开头的 token
        if not tok.startswith("-"):
            i += 1
            continue

        # 尝试将输入的 key 对应全称（若输入的是正确的缩写）
        if aliasMap and key in aliasMap:
            key = aliasMap[key]

        # 当 key 不在各个指令所定义的 parsed 当中时
        if key not in parsed:
            i += 1
            continue


        # 获取参数值——

        # 当是以 -key=value 形式输入，则 value 将被提前定义，下个条件语句直接进入 values 列表
        if "=" in key:
            key , value = key.split("=" , 1)
        else:
            value = None

        if value is not None:
            values.append(value)
        else:
            # 当不以上者形式输入时，向后收集所有不以“-”开头的 token
            j = i + 1
            while j < len(tokens) and not tokens[j].startswith("-"):
                values.append(tokens[j])
                j += 1

        # 若没有任何 value ，返回 "NoValue"
        if not values:
            values = ["NoValue"]


        # 将解析出的参数分别写入 parsed
        if isinstance(parsed[key] , list):
            parsed[key].extend(values)
        else:
            # 若存在非 list 参数，则只接收第一个值
            parsed[key] = values[0]

        # 前进，读取下一个参数、跳过所有被吃掉的值
        i += 1 + len(values)


    return parsed