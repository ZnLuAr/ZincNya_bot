# -*- coding: utf-8 -*-


from telegram.ext import ApplicationBuilder
import asyncio
import sys

# 以下是项目内的模块名
from loader import loadHandlers
from config import *
from utils.logger import initLogger
from handlers.cli import *




async def consoleListener(app):
    print("控制台命令可用喵。输入 /help 查看帮助。\n")
    loop = asyncio.get_event_loop()

    while True:
        # 在异步环境中读取标准输入
        command = await loop.run_in_executor(None , sys.stdin.readline)
        command = command.strip()

        if not command:
            continue
        
        # 调用 CLI 处理器
        commandResult = await handleConsoleCommand(app , command)
        if commandResult == "SHUTDOWN":
            break
            



async def main():

    # 初始化应用
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    # 初始化日志
    initLogger()
    loadHandlers(app)

    
    print("ZincNya Bot——\n喵的一声，就启动啦——\n")

    await app.initialize()
    await app.start()
    await app.updater.start_polling()  # 启动监听

    consoleTask = asyncio.create_task(consoleListener(app))

    # 等待控制台任务结束，即 /shutdown 时
    await consoleTask

    await app.updater.stop()    
    await app.stop()
    await app.shutdown()




if __name__ == "__main__":
    asyncio.run(main())