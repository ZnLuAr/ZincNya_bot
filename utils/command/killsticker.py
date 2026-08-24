"""
utils/command/killsticker.py

控制台命令：中止正在进行的 sticker 下载任务
"""

from utils.memoryMonitor import cancelAllStickerTasks




async def execute(app, args):
    count = await cancelAllStickerTasks()
    if count > 0:
        print(f"已中止 {count} 个下载任务喵——\n")
    else:
        print("👀 没有正在进行的下载任务喵——\n")
    return None




def getHelp():
    return {
        "name": "/killsticker",
        "description": "中止所有正在进行的表情包下载任务",
        "usage": "直接输入 /killsticker 就好了哦",
        "example": "任务跑到一半卡住、或者内存告警要求止损时使用",
    }
