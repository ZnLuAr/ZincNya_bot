"""
utils/core/appLifecycle.py

应用生命周期管理模块

负责：
- Telegram Application 的初始化和配置
- 后台任务的启动和协调
- 优雅关机流程
- 进程重启逻辑
"""

import sys
import logging
import asyncio
import importlib
import inspect

from telegram.ext import ApplicationBuilder, MessageHandler, filters

from config import BOT_TOKEN, TELEGRAM_PROXY
from loader import loadHandlers

from utils.core.resourceManager import cleanupAllResources
from utils.core.stateManager import getStateManager
from utils.core.errorHandler import initErrorHandler, setupAsyncioErrorHandler
from utils.core.logger import initLogger




def buildApplication():
    """构建 Telegram Application 实例"""
    builder = ApplicationBuilder().token(BOT_TOKEN)
    if TELEGRAM_PROXY:
        builder = builder.proxy(TELEGRAM_PROXY).get_updates_proxy(TELEGRAM_PROXY)
    return builder.build()




def initializeApp(app):
    """初始化应用组件"""
    from utils.moduleManager import getAllModules

    # 迁移旧版 LLM 文件到 data/llm/ 子目录（必须在数据库初始化之前）
    from config import migrateLegacyLLMPaths
    migrateLegacyLLMPaths()

    state = getStateManager()
    state.setMessageQueue(asyncio.Queue())

    loop = asyncio.get_running_loop()
    state.setEventLoop(loop)

    initLogger()
    initErrorHandler(app)

    # 动态调用模块的 initFunctions（自动去重）
    allModules = getAllModules()
    calledFunctions = set()  # 去重集合

    for moduleName, config in allModules.items():
        if not config.get("enabled", True):
            continue

        for funcPath in config["metadata"].get("initFunctions", []):
            if funcPath in calledFunctions:
                continue  # 跳过已调用的函数

            try:
                modulePath, funcName = funcPath.split(":")
                module = importlib.import_module(modulePath)
                initFunc = getattr(module, funcName)
                initFunc()
                calledFunctions.add(funcPath)
            except Exception as e:
                print(f"  初始化 {funcPath} 失败：{e}")

    loadHandlers(app)
    setupAsyncioErrorHandler(loop)

    # 全局消息收集器
    async def messageCollector(update, context):
        """把所有收到的消息放入队列"""
        message = update.message
        if message:
            queue = state.getMessageQueue()
            if queue:
                await queue.put(message)

    # group=-1 确保在所有其他 handler 之前执行
    app.add_handler(MessageHandler(filters.ALL, messageCollector), group=-1)




async def startApp(app):
    """启动 Telegram Application"""
    await app.initialize()
    await app.start()
    await app.updater.start_polling()


_logger = logging.getLogger(__name__)




async def stopApp(app):
    """关闭 Telegram Application"""
    for name, coro in [
        ("cleanupAllResources", cleanupAllResources()),
        ("updater.stop", app.updater.stop()),
        ("app.stop", app.stop()),
        ("app.shutdown", app.shutdown()),
    ]:
        try:
            await coro
        except Exception as e:
            _logger.warning("关机阶段 %s 出错: %s", name, e)




async def runBackgroundTasks(app, consoleTask):
    """
    运行后台任务并等待关机信号

    返回:
        None - 正常关机
    """
    from utils.moduleManager import getAllModules

    state = getStateManager()
    shutdownEvent = state.getShutdownEvent()

    # 动态启动模块的 backgroundTasks（自动去重）
    backgroundTasks = []
    startedTasks = set()  # 去重集合
    allModules = getAllModules()

    for moduleName, config in allModules.items():
        if not config.get("enabled", True):
            continue

        for taskPath in config["metadata"].get("backgroundTasks", []):
            if taskPath in startedTasks:
                continue  # 跳过已启动的任务

            try:
                modulePath, funcName = taskPath.split(":")
                module = importlib.import_module(modulePath)
                taskFunc = getattr(module, funcName)

                # 根据函数签名判断是否需要传入 app
                sig = inspect.signature(taskFunc)
                if len(sig.parameters) > 0:
                    coro = taskFunc(app)
                else:
                    coro = taskFunc()

                task = asyncio.create_task(coro)
                backgroundTasks.append(task)
                startedTasks.add(taskPath)
            except Exception as e:
                print(f"  启动后台任务 {taskPath} 失败：{e}")

    # 等待控制台任务结束或远程关机信号
    _, pending = await asyncio.wait(
        [consoleTask, asyncio.create_task(shutdownEvent.wait())],
        return_when=asyncio.FIRST_COMPLETED
    )

    # 取消所有任务
    for task in pending:
        task.cancel()
    for task in backgroundTasks:
        task.cancel()

    # 向控制台注入回车，解除线程池中 readline() 的阻塞
    # （控制台关机时 consoleTask 已完成，此操作无影响）
    from utils.inputHelper import interruptInput
    interruptInput()

    # 等待任务取消完成
    await asyncio.gather(*pending, *backgroundTasks, return_exceptions=True)




def restartProcess():
    """重启当前进程"""
    if sys.platform == "win32":
        # Windows: 使用 subprocess 启动新进程
        import subprocess
        subprocess.Popen([sys.executable] + sys.argv)
        sys.exit(0)
    else:
        # Unix: 使用 os.execv 替换当前进程
        import os
        os.execv(sys.executable, [sys.executable] + sys.argv)
