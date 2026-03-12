# -*- coding: utf-8 -*-

import asyncio

from utils.core.appLifecycle import (
    buildApplication,
    initializeApp,
    startApp,
    stopApp,
    runBackgroundTasks,
    restartProcess,
)
from utils.core.stateManager import getStateManager
from utils.core.consoleListener import consoleListener




async def main():
    """Bot 主入口"""
    # 构建和初始化应用
    app = buildApplication()
    initializeApp(app)

    print("ZincNya Bot——\n喵的一声，就启动啦——\n")

    try:
        # 启动 Telegram 轮询
        await startApp(app)

        # 启动控制台监听器
        consoleTask = asyncio.create_task(consoleListener(app))

        # 运行后台任务并等待关机信号
        await runBackgroundTasks(app, consoleTask)

    finally:
        # 优雅关机
        await stopApp(app)

    # 如果是重启请求，替换进程
    if getStateManager().isRestartRequested():
        restartProcess()


if __name__ == "__main__":
    asyncio.run(main())
