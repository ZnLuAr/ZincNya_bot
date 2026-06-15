"""
utils/chatScreen/mainLoop.py

主循环输入处理。

运行 UI 事件循环，分发用户输入：LLM 审核命令、编辑模式提交、普通消息发送。
退出时返回当前编辑项（如有），由 session.py 负责放回审核队列。
"""

from telegram.error import Forbidden

from utils.chatHistory import saveMessage
from utils.llm.state import getReviewQueue

from .formatter import formatMessage
from .statusBar import getDefaultStatus




async def runMainLoop(bot, targetChatID: str, ui, shutdownEvent) -> dict | None:
    """
    运行 chatScreen 主循环，处理用户输入。

    返回退出时仍处于编辑模式的审核项（dict）或 None。
    """
    _reviewEditItem: dict | None = None

    while not shutdownEvent.is_set():
        userInput = await ui.run()

        # 用户 Ctrl+C / Esc 退出
        if userInput is None:
            if _reviewEditItem is not None:
                # 编辑模式中 Esc → 取消编辑，放回队列，不退出聊天
                getReviewQueue().put_nowait(_reviewEditItem)
                _reviewEditItem = None
                ui.resetExitFlag()
                ui.clearComposer()
                ui.showStatus(getDefaultStatus(targetChatID))
                continue
            break

        # 清空 composer 以准备下一轮输入
        ui.clearComposer()

        stripped = userInput.strip()

        # ── LLM 审核命令 ──
        if stripped in (":ra", ":re", ":rr", ":rf", ":rc", ":rq"):
            from utils.command.llm import handleChatScreenReviewCommand
            _reviewEditItem = await handleChatScreenReviewCommand(stripped, bot, ui)
            continue

        # 空消息不发送
        if not stripped:
            continue

        # 编辑模式提交 → 放回审核队列，不直接发送
        if _reviewEditItem is not None:
            from utils.command.llm import handleChatScreenEditSubmit
            await handleChatScreenEditSubmit(_reviewEditItem, userInput.rstrip('\n'), ui)
            _reviewEditItem = None
            continue

        textToSend = userInput.rstrip('\n')

        try:
            ts, sndr, txt = formatMessage("selfMessage", textToSend)
            ui.appendSelfMessage(ts, sndr, txt)
            await bot.send_message(chat_id=targetChatID , text=textToSend)
            await saveMessage(targetChatID, "outgoing", "ZincNya~", textToSend)
        except Forbidden:
            ui.showStatus("被 Forbidden 了……对方可能还没跟咱开始聊天")
        except Exception as e:
            ui.showStatus(f"发送失败: {e}")

    # 主循环正常结束，返回编辑项（如有），由调用方放回队列
    return _reviewEditItem
