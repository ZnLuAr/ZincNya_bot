"""
utils/command/reboot.py

控制台命令：优雅重启客户端（exit 后由进程管理器/systemd 拉起，或 bot.py 的
restartProcess 替换进程——见 utils/core/appLifecycle.py）。
"""

from utils.core.stateManager import getStateManager
from utils.core.logger import logAction, LogLevel, LogChildType




async def execute(app, args):
    await logAction(
        "System",
        "/reboot",
        "收到重启指令",
        LogLevel.INFO,
        LogChildType.NONE
    )
    # 请求优雅重启：runBackgroundTasks 的关机信号触发 → stopApp 收尾 →
    # bot.py main 检测 isRestartRequested → restartProcess
    # 
    # 此处调用的函数依旧以 Restart 为措辞，这是因为 「reboot」在系统语境里通常指整机重启，
    # 而这里做的是进程重启（process restart）。命令使用 reboot 只是 Unix 的习惯。
    getStateManager().requestRestart()
    return "SHUTDOWN"




def getHelp():
    return {
        "name": "/reboot",
        "description": "正确且安全地重启客户端",
        "usage": "直接输入 /reboot 就好了哦",
        "example": "👀",
    }
