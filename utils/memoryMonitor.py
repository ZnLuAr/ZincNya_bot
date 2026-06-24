"""
utils/memoryMonitor.py

内存监控后台任务
"""

import time
import asyncio

import psutil
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from config import (
    MEMORY_MONITOR_INTERVAL,
    MEMORY_WARNING_THRESHOLD_MB,
    MEMORY_ALERT_COOLDOWN,
    MEMORY_GATE_THRESHOLD_MB,
    Permission,
)

from utils.core.logger import logSystemEvent, LogLevel, LogChildType
from utils.operators import getOperatorsWithPermission


_lastAlertTime: float = 0.0
_activeStickerTasks: list = []  # asyncio.Task 引用列表，供 cancelAllStickerTasks 使用




async def monitorMemory(app):
    """
    内存监控后台任务

    每隔 MEMORY_MONITOR_INTERVAL 秒检查系统可用内存，低于阈值时：
    - 记录 WARNING 日志
    - 通知所有 NOTIFY 权限的 OP
    - 如果当前有 sticker 下载任务，消息附带"终止"按钮
    """
    global _lastAlertTime

    while True:
        try:
            await asyncio.sleep(MEMORY_MONITOR_INTERVAL)

            availableMB = psutil.virtual_memory().available / (1024 * 1024)

            if availableMB < MEMORY_WARNING_THRESHOLD_MB:
                now = time.monotonic()
                if (now - _lastAlertTime) > MEMORY_ALERT_COOLDOWN:
                    _lastAlertTime = now

                    await logSystemEvent(
                        "内存不足告警",
                        f"可用内存：{availableMB:.0f}MB（阈值：{MEMORY_WARNING_THRESHOLD_MB}MB）",
                        level=LogLevel.WARNING,
                        childType=LogChildType.LAST_CHILD
                    )

                    text, keyboard = alertMessageConstructor(availableMB)

                    for opID in getOperatorsWithPermission(Permission.NOTIFY):
                        try:
                            await app.bot.send_message(
                                chat_id=int(opID),
                                text=text,
                                reply_markup=keyboard
                            )
                        except TelegramError:
                            pass  # 用户 block bot / chat not found 等预期失败
                        except Exception as e:
                            await logSystemEvent(
                                "内存告警发送失败",
                                f"OP {opID}: {type(e).__name__}: {str(e)}",
                                level=LogLevel.ERROR,
                                childType=LogChildType.CHILD
                            )

        except asyncio.CancelledError:
            break
        except Exception as e:
            await logSystemEvent(
                "内存监控任务异常",
                str(e),
                level=LogLevel.ERROR,
                childType=LogChildType.LAST_CHILD
            )
            await asyncio.sleep(min(MEMORY_MONITOR_INTERVAL, 10))




def alertMessageConstructor(availableMB: float) -> tuple:
    """
    构建内存告警消息与可选的 InlineKeyboard

    参数：
        availableMB: 当前可用内存（MB）

    返回：
        (text, reply_markup)
        - reply_markup 为 None 时表示无活跃下载任务，不附加按钮

    """
    text = (
        "呜哇……内存池快见底了\n\n"
        f"剩余的内存只有 {availableMB:.0f}MB 了喵……"
    )

    if len(_activeStickerTasks) > 0:
        text += "\n\n现在有表情包下载任务正在进行，要中止吗？"
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("终止下载任务", callback_data="sticker:kill:all")
        ]])
    else:
        keyboard = None

    return text, keyboard




def isMemoryLow() -> bool:
    """
    检查当前可用内存是否低于拦截阈值

    返回：
        True - 内存不足，应拒绝新重型任务
        False - 内存充足
    """
    availableMB = psutil.virtual_memory().available / (1024 * 1024)
    return availableMB < MEMORY_GATE_THRESHOLD_MB




def registerStickerTask(task: asyncio.Task):
    """注册一个 sticker 下载任务（供 handlers/stickers.py 调用）"""
    _activeStickerTasks.append(task)


def unregisterStickerTask(task: asyncio.Task):
    """注销一个 sticker 下载任务"""
    if task in _activeStickerTasks:
        _activeStickerTasks.remove(task)




async def cancelAllStickerTasks() -> int:
    """
    中止所有正在进行的 sticker 下载任务

    返回：
        已中止的任务数量
    """
    tasks = _activeStickerTasks[:]  # 复制，避免迭代时修改
    count = len(tasks)

    for task in tasks:
        task.cancel()

    await asyncio.gather(*tasks, return_exceptions=True)
    _activeStickerTasks.clear()

    await logSystemEvent(
        "sticker 下载任务已中止",
        f"共 {count} 个任务",
        level=LogLevel.INFO,
        childType=LogChildType.LAST_CHILD
    )

    return count
