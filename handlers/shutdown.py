"""
shutdown.py

提供在 Telegram 端使用 /shutdown、/restart、/status 等管理命令的途径，
仅限 operators 使用。
"""

import os
import re
import sys
import time
import asyncio
import psutil

from telegram import Update
from datetime import datetime
from telegram.constants import MessageEntityType
from telegram.ext import CommandHandler , MessageHandler , ContextTypes , filters


from config import Permission
from utils.operators import hasPermission
from utils.core.stateManager import getStateManager
from utils.logger import logSystemEvent , LogChildType
from utils.core.errorDecorators import handleTelegramErrors


# Bot 启动时间（用于计算运行时长）
_START_TIME = time.time()




@handleTelegramErrors(errorReply="就不关喵w ——")
async def shutdown(update: Update , _: ContextTypes.DEFAULT_TYPE):
    """关闭 bot（仅 operators）"""
    userID = update.effective_user.id

    if not hasPermission(userID , Permission.SHUTDOWN):
        await update.message.reply_text("❌ もー、只有ご主人才有权限执行这个操作喵")
        return

    await logSystemEvent(
        f"收到 Telegram 端的关机指令",
        f"指令来自 {update.effective_user.full_name}，正在关机……",
        childType=LogChildType.WITH_ONE_CHILD
    )

    await update.message.reply_text("……瞼を閉じました……=  =")

    # 通知 bot.py ，高雅不堪地关机
    getStateManager().requestShutdown()




@handleTelegramErrors(errorReply="就不关喵w ——")
async def restart(update: Update , _: ContextTypes.DEFAULT_TYPE):
    """重启 bot（仅 operators）"""
    userID = update.effective_user.id

    if not hasPermission(userID , Permission.RESTART):
        await update.message.reply_text("❌ もー、只有ご主人才有权限执行这个操作喵")
        return

    await logSystemEvent(
        f"收到 Telegram 端的重启指令",
        f"指令来自 {update.effective_user.full_name} ，正在重启……",
        childType=LogChildType.WITH_ONE_CHILD
    )

    await update.message.reply_text("……瞼を閉じました……=  =")

    # 通知 bot.py 进行高雅不堪的关机后重启
    getStateManager().requestRestart()




@handleTelegramErrors(errorReply="呜……不给看w ——！")
async def status(update: Update , _: ContextTypes.DEFAULT_TYPE):
    """查看运行状态（仅 operators）"""
    userID = update.effective_user.id

    if not hasPermission(userID , Permission.STATUS):
        await update.message.reply_text("❌ もー、只有ご主人才有权限执行这个操作喵")
        return

    # 计算运行时长
    uptime = time.time() - _START_TIME
    days = int(uptime // 86400)
    hours = int((uptime % 86400) // 3600)
    minutes = int((uptime % 3600) // 60)
    seconds = int(uptime % 60)

    uptimeStr = ""
    if days > 0:
        uptimeStr += f"{days} 天 "
    if hours > 0:
        uptimeStr += f"{hours} 小时 "
    if minutes > 0:
        uptimeStr += f"{minutes} 分钟 "
    uptimeStr += f"{seconds} 秒"

    # 获取进程信息
    process = psutil.Process(os.getpid())
    memoryMB = process.memory_info().rss / 1024 / 1024
    cpuPercent = await asyncio.to_thread(process.cpu_percent, 0.1)

    statusText = (
        f"锌酱的目前啊、\n"
        f"已经运行了 {uptimeStr}喵\n"
        f"\n"
        f"当前有：\n"
        f"内存占用：{memoryMB:.2f} MB\n"
        f"CPU 占用：{cpuPercent:.1f}%\n"
        f"\n"
        f"Python {sys.version.split()[0]}\n"
        f"统计于 {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}喵"
    )

    await update.message.reply_text(statusText)




# @ 关键词 → 处理函数 映射
_KEYWORD_MAP = {
    "关机": shutdown,
    "重启": restart,
    "运行状态": status,

    "去睡觉": shutdown,
    "起来重睡": restart,
    "看看你的": status,
}


@handleTelegramErrors   # 实在想不到什么时候会触发这里的装饰器，但是防御性编程😋
async def _mentionDispatch(update: Update , context: ContextTypes.DEFAULT_TYPE):
    """ 处理 @bot + 关键词 的消息 """
    text = update.message.text
    botUsername = context.bot.username

    # 检查是否提及了 bot 自己（不区分大小写）
    if f"@{botUsername}".lower() not in text.lower():
        return

    # 去掉 @bot 后匹配关键词（大小写不敏感替换）
    pattern = re.compile(re.escape(f"@{botUsername}"), re.IGNORECASE)
    body = pattern.sub("", text).strip()
    if body in _KEYWORD_MAP:
        await _KEYWORD_MAP[body](update , context)




def register():
    """注册 handlers """
    return {
        "handlers": [
            CommandHandler("shutdown" , shutdown),
            CommandHandler("restart" , restart),
            CommandHandler("status" , status),
            MessageHandler(
                filters.TEXT & filters.Entity(MessageEntityType.MENTION) ,
                _mentionDispatch
            ),
        ],
        "name": "Telegram 端关机/重启",
        "description": "Telegram 端关机/重启命令（仅ops）",
        "auth": False,  # 使用独立的 operator 权限系统
    }
