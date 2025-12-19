# -*- coding: utf-8 -*-


from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    filters,
)
import asyncio
import sys

# 以下是项目内的模块名
from loader import loadHandlers
from config import BOT_TOKEN
from utils.logger import initLogger
from handlers.cli import handleConsoleCommand




async def consoleListener(app):
    print("控制台命令可用喵。输入 /help 查看帮助。\n")
    loop = asyncio.get_event_loop()

    while True:
        # 如果有其它功能需要夺取交互权，则转让输入权给内层的其它功能
        if app.bot_data["state"]["interactiveMode"] is not False:
            await asyncio.sleep(0.1)
            continue

        # 在异步环境中读取标准输入
        command = await loop.run_in_executor(None , sys.stdin.readline)
        command = command.strip()
        if not command:
            continue
        
        # 调用 CLI 处理器
        commandResult = await handleConsoleCommand(app , command)
        if commandResult == "SHUTDOWN":
            return "SHUTDOWN"
            



async def main():

    # 初始化应用
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    # 定义 bot 的一些运行状况，挂载在 app.bot_data 上
    app.bot_data["state"] = {
        "interactiveMode": False,           # 是否暂时由某个模块接管控制台输入
        "messageQueue": asyncio.Queue()     # 用于收集 Message 对象
    }

    initLogger()                            # 初始化日志
    loadHandlers(app)                       # 注册 handlers 的功能（我迟早得把它优化了）


    # ========================================================================
    async def messageCollector(update , context):
        '''
        全局消息收集，把所有收到的消息放入队列

        该队列的一个典型的应用就是被 utils/command/send.py 的 chatScreen() 调用
            - 其间用作消息的实时同步，从全局收集消息并输出来自于聊天对象的消息
                当有来自聊天对象的消息时，把消息 put 到队列供消费
        '''

        message = update.message
        if message:
            await context.application.bot_data["state"]["messageQueue"].put(message)
    # ========================================================================


    # 注册 MessageHandler（filters.ALL）
    app.add_handler(MessageHandler(filters.ALL , messageCollector))

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