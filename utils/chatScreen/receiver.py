"""
utils/chatScreen/receiver.py

消息接收后台协程。

启动一个后台 task 持续监听全局 messageQueue，筛选属于当前 chatID 的消息
并展示到 UI；超时轮询时顺便检查关机信号与审核队列。
"""

import asyncio
from datetime import datetime

from utils.chatHistory import saveMessage
from utils.llm.state import getReviewQueue, peekReviewHint

from .formatter import getSenderName, extractDisplayText
from .statusBar import getReviewQueueStatus




async def startReceiver(state, targetChatID: str, ui, queue: asyncio.Queue,
                        shutdownEvent: asyncio.Event) -> asyncio.Task:
    """
    启动消息接收后台协程，返回 receiverTask。

    调用方负责在退出时 cancel 并 await 该 task。
    """
    async def receiverLoop():
        """后台协程：持续监听对方发来的消息并展示到 UI。"""
        nonTargetMessages = []

        try:
            while state.isInteractive():
                try:
                    try:
                        msg = await asyncio.wait_for(queue.get(), timeout=0.5)
                    except asyncio.TimeoutError:
                        # 检测外部关机信号，强制退出 UI
                        if shutdownEvent.is_set():
                            ui.requestExit()
                            break
                        # 定期检查审核队列，更新状态栏提示
                        try:
                            rq = getReviewQueue()
                            if rq.qsize() > 0:
                                hint = peekReviewHint() or ""
                                ui.showStatus(getReviewQueueStatus(rq.qsize(), hint))
                        except Exception:
                            pass
                        continue

                    if not msg:
                        continue

                    if str(msg.chat.id) != str(targetChatID):
                        nonTargetMessages.append(msg)
                        continue

                    sender = getSenderName(msg)
                    content = extractDisplayText(msg)
                    await saveMessage(targetChatID, "incoming", sender, content)

                    # sender / content 已解析，直接复用，不再走 formatMessage()
                    # 对同一 msg 二次调用 getSenderName + extractDisplayText。
                    timestamp = datetime.now().strftime("%H:%M:%S")
                    ui.appendIncomingMessage(timestamp, sender, content)

                except asyncio.CancelledError:
                    break
                except Exception:
                    pass
        finally:
            for msg in nonTargetMessages:
                await queue.put(msg)

    return asyncio.create_task(receiverLoop())
