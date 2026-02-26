# -*- coding: utf-8 -*-


from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    filters,
)
import asyncio

from loader import loadHandlers
from config import BOT_TOKEN , TELEGRAM_PROXY
from utils.logger import initLogger
from handlers.cli import handleConsoleCommand
from utils.errorHandler import initErrorHandler , setupAsyncioErrorHandler
from utils.inputHelper import asyncInput
from utils.core.stateManager import getStateManager


async def consoleListener(app):
    print("控制台命令可用喵。输入 /help 查看帮助。\n")
    state = getStateManager()

    while True:
        if state.getInteractiveMode():
            await asyncio.sleep(0.1)
            continue

        try:
            command = await asyncInput("")

            if state.getInteractiveMode():
                continue

            command = command.strip()
            if not command:
                continue

            commandResult = await handleConsoleCommand(app , command)
            if commandResult == "SHUTDOWN":
                return "SHUTDOWN"

        except EOFError:
            return "SHUTDOWN"
        except Exception as e:
            print(f"控制台读取出错喵：{e}")
            continue
            



async def main():

    # 初始化应用
    builder = ApplicationBuilder().token(BOT_TOKEN)
    if TELEGRAM_PROXY:
        builder = builder.proxy(TELEGRAM_PROXY).get_updates_proxy(TELEGRAM_PROXY)
    app = builder.build()
    
    # 初始化全局状态管理器
    state = getStateManager()
    state.setMessageQueue(asyncio.Queue())

    initLogger()                            # 初始化日志
    initErrorHandler(app)                   # 初始化错误处理
    loadHandlers(app)                       # 动态加载 Telegram handlers

    # 设置 asyncio 异常处理器
    loop = asyncio.get_event_loop()
    setupAsyncioErrorHandler(loop)

    # ========================================================================
    async def messageCollector(update , context):
        """全局消息收集，把所有收到的消息放入队列"""
        message = update.message
        if message:
            queue = getStateManager().getMessageQueue()
            if queue:
                await queue.put(message)
    # ========================================================================


    # 注册 MessageHandler（filters.ALL），使用 group=-1 确保它在所有其他 handler 之前执行
    # 且不影响其他 handler 的执行
    app.add_handler(MessageHandler(filters.ALL , messageCollector) , group=-1)

    print("ZincNya Bot——\n喵的一声，就启动啦——\n")

    await app.initialize()
    await app.start()
    await app.updater.start_polling()  # 启动监听

    consoleTask = asyncio.create_task(consoleListener(app))

    # 等待控制台任务结束，即 /shutdown 时
    result = await consoleTask
    if result == "SHUTDOWN":
        await app.updater.stop()    
        await app.stop()
        await app.shutdown()




if __name__ == "__main__":
    asyncio.run(main())