"""
utils/chatScreen/receiver.py

消息接收后台协程。

监听当前会话的已保存消息通知并展示；历史快照中已有的记录不再追加。
超时轮询时检查关机信号与审核队列。此处不写聊天历史。
"""

import asyncio

from utils.core.logger import logSystemEvent, LogLevel
from utils.llm.review import peekReviewHint
from utils.llm.state import getReviewQueue

from .formatter import formatMessage
from .statusBar import getReviewQueueStatus


# receiverLoop 单次 queue.get() 轮询超时，超时后顺带检查关机信号与审核队列
_POLL_TIMEOUT_SECONDS = 0.5




async def startReceiver(ui, queue: asyncio.Queue, shutdownEvent: asyncio.Event,
                        historyRecordIDs: set[int]) -> asyncio.Task:
    """
    启动消息接收后台协程，返回 receiverTask。

    调用方负责在退出时 cancel 并 await 该 task。
    """
    async def receiverLoop():
        """持续监听对方发来的消息并展示到 UI 的后台协程。"""
        while True:
            try:
                try:
                    recordID, message = await asyncio.wait_for(queue.get(), timeout=_POLL_TIMEOUT_SECONDS)
                except asyncio.TimeoutError:
                    if shutdownEvent.is_set():
                        ui.requestExit()
                        break
                    rq = getReviewQueue()
                    if rq.qsize() > 0:
                        hint = peekReviewHint() or ""
                        ui.showStatus(getReviewQueueStatus(rq.qsize(), hint))
                    continue

                if recordID in historyRecordIDs:
                    continue
                timestamp, sender, content = formatMessage("incomingMessage", message)
                ui.appendIncomingMessage(timestamp, sender, content)
                historyRecordIDs.add(recordID)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                await logSystemEvent("聊天界面接收消息失败", str(exc), LogLevel.ERROR, exception=exc)

    return asyncio.create_task(receiverLoop())
