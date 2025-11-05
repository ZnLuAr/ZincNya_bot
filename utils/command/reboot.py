import os
import sys
import asyncio

from utils.logger import logAction




async def execute(app , args):
    print("\n🔁 收到重启指令喵……正在保存状态……\n")

    await logAction("Console" , "/reboot" , "OK喵" , "withOneChild")
    await app.updater.stop()
    await app.stop()
    await app.shutdown()

    await asyncio.sleep(1)
    print("     · 重启中，请稍后…… = =\n\n")

    python = sys.executable
    os.execl(python , python , *sys.argv)