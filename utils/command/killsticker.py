"""
utils/command/killsticker.py

控制台命令：中止正在进行的 sticker 下载任务
"""

from utils.memoryMonitor import cancelAllStickerTasks




async def execute(app, args):
    count = await cancelAllStickerTasks()
    print(f"已中止 {count} 个下载任务喵——" if count > 0 else "没有正在进行的下载任务喵——")
    return None




def getHelp():
    return "中止所有正在进行的表情包下载任务"
