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
            if inspect.isawaitable(result):
                await result
        except Exception as e:
            await logAction("Console" , f"/{commandName} {commandArgs}" , f"执行时出错了喵：{e}" , "withOneChild")
    else:
            print(f"❌ やばいー/{commandName} 模块中没有定义 execute(app, args) 函数喵！\n")




def parseArgsTokens(parsed: dict , tokens: list[str]):

    '''
    通用参数解析函数
    接受来自各指令模块的 parsed:dict
    原始 parsed 形如 {"at": None, "text": None, "id": [], "chat":None}
    最后返回填充了各个参数值的 parsed

    支持格式：
      -f value1 value2 value3
      --flag value
      --flag=value
      -id 123 456 789
      -c 1234 任意备注文本 不用引号
      -t "带空格的文本"

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