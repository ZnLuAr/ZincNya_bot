"""
utils/command/clear.py

用于清理控制台屏幕的命令模块。

提供多种清屏方式：
    - 清除整个终端屏幕
    - 清除指定行数
    - 重置到启动状态（重新打印启动信息）
"""

import sys
from handlers.cli import parseArgsTokens




async def execute(app , args: list[str]):

    parsed = {
        "all": None,
        "n": None,
        "reset": None,
    }

    # 缩写映射
    argAlias = {
        "a": "all",
        "n": "lines",
        "r": "reset",
    }

    parsed = parseArgsTokens(parsed , args , argAlias)

    allFlag = parsed["all"]
    linesCount = parsed["n"]
    resetFlag = parsed["reset"]


    # -r/--reset: 清屏并重新打印启动信息
    if resetFlag is not None:
        clearScreen()
        printStartupBanner()
        return


    # -n/--lines <数字>: 清除指定行数
    if linesCount and linesCount != "NoValue":
        try:
            n = int(linesCount)
            if n > 0:
                clearLines(n)
                return
            else:
                print("❌ 行数必须是正整数喵——\n")
                return
        except ValueError:
            print(f"❌ 无效的行数喵：{linesCount}\n")
            return


    # -a/--all 或无参数: 清除整个屏幕
    clearScreen()




def clearScreen():
    """清除整个终端屏幕"""
    sys.stdout.write("\033[2J")     # 清除整个屏幕
    sys.stdout.write("\033[H")      # 光标移到左上角
    sys.stdout.flush()


def clearLines(n: int):
    """向上清除指定行数"""
    for _ in range(n):
        sys.stdout.write("\033[F")  # 光标上移一行
        sys.stdout.write("\033[2K") # 清除整行
    sys.stdout.flush()


def printStartupBanner():
    """打印启动信息"""
    print("ZincNya Bot——\n喵的一声，就启动啦——\n")
    print("控制台命令可用喵。输入 /help 查看帮助。\n")




def getHelp():
    return {

        "name": "/clear",

        "description": "清理控制台屏幕",

        "usage": (
            "/clear (-a/--all) (-n/--n <int>) (-r/--reset)"
        ),

        "example": (
            "清除整个屏幕：/clear\n"
            "清除整个屏幕：/clear --all\n"
            "向上清除 10 行：/clear -n 10\n"
            "重置到启动状态：/clear --reset"
        ),

    }
