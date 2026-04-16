"""
utils/core/consoleListener.py

控制台命令监听器

负责：
异步读取控制台输入、调度命令处理和处理 EOF 和异常
"""




import asyncio

from handlers.cli import handleConsoleCommand

from utils.core.stateManager import getStateManager
from utils.inputHelper import asyncInput




async def consoleListener(app):
    """
    控制台命令监听循环

    参数:
        app: Telegram Application 实例

    返回:
        "SHUTDOWN" - 收到关机命令
    """
    print("控制台命令可用喵。输入 /help 查看帮助。\n")
    state = getStateManager()

    try:
        while True:
            # 交互模式下阻塞等待（零 CPU）
            await state.waitForNonInteractive()

            try:
                command = await asyncInput("")

                # 再次检查（可能在输入期间进入交互模式）
                if state.isInteractive():
                    continue

                command = command.strip()
                if not command:
                    continue

                commandResult = await handleConsoleCommand(app, command)
                if commandResult == "SHUTDOWN":
                    return "SHUTDOWN"

            except EOFError:
                return "SHUTDOWN"
            except Exception as e:
                print(f"控制台读取出错喵：{e}")
                continue

    except asyncio.CancelledError:
        # 远程关机时被取消，直接返回
        return
