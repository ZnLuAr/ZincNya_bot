"""
utils/command/llm/review/chatScreenReview.py

chatScreen 内的 LLM 审核命令处理（:ra/:re/:rr/:rf/:rc/:rq）与编辑模式提交。

与 consoleReview.py 共享底层 utils.llm.review 原语，但交互范式不同：
本模块面向 TUI（ui.showStatus / ui._composerArea），且审核是两阶段异步——
:re/:rf 先把审核项交回主循环进入"编辑模式"，下一轮输入才由 handleChatScreenEditSubmit 提交。

两个函数之间的私有 dict 协议（mainLoop 透传，不读取）：
    - :re 返回裸审核项 dict（无 _mode 键）
    - :rf 返回 {"_mode": "retry_feedback", "item": <审核项>}
    - handleChatScreenEditSubmit 用 editItem.get("_mode") == "retry_feedback" 区分两种模式
"""

from utils.chatScreen.statusBar import getEditModeStatus

from utils.llm.review import (
    canEditReviewItem,
    canRetryReviewItem,
    reviewCancel,
    reviewEditSubmit,
    reviewRetry,
    reviewRetryWithFeedback,
    reviewSend,
)
from utils.llm.state import getReviewQueue




async def handleChatScreenReviewCommand(command: str, bot, ui) -> dict | None:
    """
    chatScreen 内审核命令处理（:ra/:re/:rr/:rf/:rc/:rq）。

    参数:
        command: 用户输入的命令（已 strip）
        bot: Telegram Bot 实例
        ui: ChatScreenApp 实例

    返回:
        进入编辑模式时返回审核项 dict，否则返回 None。
    """
    queue = getReviewQueue()

    if command == ":rq":
        ui.showStatus(f" 待审核队列：{queue.qsize()} 条" if queue.qsize() > 0 else "审核队列为空")
        return None

    if queue.empty():
        ui.showStatus(" 审核队列空了喵")
        return None

    item = queue.get_nowait()
    kind = item.get("kind", "reply")

    if command == ":ra":
        try:
            await reviewSend(bot, item)
        except Exception as e:
            queue.put_nowait(item)
            ui.showStatus(f" 操作失败喵：{e}")

    elif command == ":re":
        if not canEditReviewItem(item):
            queue.put_nowait(item)
            ui.showStatus(" 当前审核项不可编辑喵")
            return None

        if kind == "memory":
            ui._composerArea.text = item.get("action", {}).get("content", "")
            ui.showStatus(getEditModeStatus("memory"))
        else:
            ui._composerArea.text = item["reply"]
            ui.showStatus(getEditModeStatus("llm"))
        return item

    elif command == ":rr":
        if not canRetryReviewItem(item):
            queue.put_nowait(item)
            ui.showStatus(" 当前审核项不支持重试喵")
            return None

        try:
            newQueueItem = await reviewRetry(item)
            queue.put_nowait(newQueueItem)
        except Exception as e:
            queue.put_nowait(item)
            ui.showStatus(f" LLM 消息生成重试失败喵：{e}")

    elif command == ":rf":
        if not canRetryReviewItem(item):
            queue.put_nowait(item)
            ui.showStatus(" 当前审核项不支持重试喵")
            return None

        # 进入编辑模式
        ui._composerArea.text = ""
        ui.showStatus(" 输入背景信息补充 | Ctrl+S 提交 | Esc 取消")
        return {"_mode": "retry_feedback", "item": item}

    elif command == ":rc":
        await reviewCancel(item)

    return None




async def handleChatScreenEditSubmit(editItem: dict, editedText: str, ui) -> None:
    """
    chatScreen 编辑模式提交处理。

    参数:
        editItem: 正在编辑的审核项（可能是裸 item，或 {"_mode":"retry_feedback","item":...}）
        editedText: 用户编辑后的文本
        ui: ChatScreenApp 实例
    """
    queue = getReviewQueue()

    # 检查是否是补充反馈重试模式
    if editItem.get("_mode") == "retry_feedback":
        item = editItem["item"]
        feedback = editedText.rstrip('\n')
        if feedback.strip():
            try:
                newItem = await reviewRetryWithFeedback(item, feedback)
                queue.put_nowait(newItem)
                ui.showStatus(" 已重新生成喵")
            except Exception as e:
                queue.put_nowait(item)
                ui.showStatus(f" 重试失败喵：{e}")
        else:
            queue.put_nowait(item)
            ui.showStatus(" 取消喵")
    else:
        # 正常编辑模式
        result = await reviewEditSubmit(editItem, editedText)
        queue.put_nowait(result)
