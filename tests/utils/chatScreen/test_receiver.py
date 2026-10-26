"""验证聊天界面的历史快照交接、重复通知、退出重进和聊天切换。"""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import Chat, Message, User

from utils import chatHistory
from utils.chatScreen import ui as chatUI
from utils.chatScreen.receiver import startReceiver
from utils.core import appLifecycle
from utils.core.database import Database
from utils.core.stateManager import StateManager


def _message(messageID, text, chatID=-100):
    return Message(
        message_id=messageID, date=datetime(2026, 10, 25, 12, 34, messageID, tzinfo=timezone.utc),
        chat=Chat(chatID, "supergroup"), from_user=User(42, "Alice", False), text=text,
    )


@pytest.fixture
def chatSetup(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "chat.db"), "ChatScreenTest")
    monkeypatch.setattr(chatHistory, "chatHistoryDB", db)
    chatHistory.initDatabase()
    state = StateManager()
    monkeypatch.setattr(appLifecycle, "getStateManager", lambda: state)
    monkeypatch.setattr(chatUI, "getStateManager", lambda: state)
    monkeypatch.setattr(appLifecycle, "whetherAuthorizedUser", lambda value: True)
    return state


def _ui(state, chatID="-100"):
    with patch("utils.chatScreen.ui.Application"), patch("utils.chatScreen.ui.sys.stdout"):
        ui = chatUI.ChatScreenApp(chatID)
    ui._stateManager = state
    ui._refreshTranscript = MagicMock()
    return ui


async def _receive(message):
    await appLifecycle.messageCollector(SimpleNamespace(message=message), None)


async def _close(ui):
    await ui.onExit()
    for task in ui._backgroundTasks:
        task.cancel()
    await asyncio.gather(*ui._backgroundTasks)


@pytest.mark.parametrize("includedInSnapshot", [False, True])
async def test_messages_arriving_during_history_load_display_once(chatSetup, monkeypatch, includedInSnapshot):
    state = chatSetup
    await _receive(_message(1, "进入前的消息"))
    originalLoad = chatHistory.loadHistory

    async def loadWithArrival(chatID):
        assert state.getInteractiveChatID() == "-100"
        if includedInSnapshot:
            await _receive(_message(2, "加载期间的消息"))
        history = await originalLoad(chatID)
        if not includedInSnapshot:
            await _receive(_message(2, "加载期间的消息"))
        return history

    monkeypatch.setattr(chatUI, "loadHistory", loadWithArrival)
    ui = _ui(state)
    try:
        await ui.onEnter()
        done = asyncio.Event()
        appendIncoming = ui.appendIncomingMessage

        def appendAndSignal(timestamp, sender, content):
            appendIncoming(timestamp, sender, content)
            if content == "实时消息":
                done.set()

        ui.appendIncomingMessage = appendAndSignal
        await _receive(_message(3, "实时消息"))
        await asyncio.wait_for(done.wait(), timeout=2)
        for text in ("进入前的消息", "加载期间的消息", "实时消息"):
            assert sum(text in line for line in ui._allLines) == 1
        assert await chatHistory.getMessageCount("-100") == 3
    finally:
        await _close(ui)


async def test_exit_reentry_and_switch_do_not_replay_messages(chatSetup):
    state = chatSetup
    await _receive(_message(1, "第一条"))
    first = _ui(state)
    await first.onEnter()
    await _close(first)
    await _receive(_message(2, "关闭后的消息"))
    await _receive(_message(3, "另一会话消息", chatID=-200))

    for chatID, expected in (("-200", ["另一会话消息"]), ("-100", ["第一条", "关闭后的消息"])):
        ui = _ui(state, chatID)
        try:
            await ui.onEnter()
            assert ui._messageQueue.empty()
            for text in expected:
                assert sum(text in line for line in ui._allLines) == 1
        finally:
            await _close(ui)
    assert state.getInteractiveChatID() is None
    assert await chatHistory.getMessageCount() == 3


async def test_repeated_notification_keeps_original_message_time():
    queue = asyncio.Queue()
    ui = MagicMock()
    done = asyncio.Event()
    ui.appendIncomingMessage.side_effect = lambda *args: done.set() if args[2] == "队列尾部消息" else None
    message = _message(2, "旧时间来信")
    queue.put_nowait((7, message))
    queue.put_nowait((7, message))
    queue.put_nowait((8, _message(3, "快照已有")))
    queue.put_nowait((9, _message(4, "队列尾部消息")))
    task = await startReceiver(ui, queue, asyncio.Event(), {8})
    try:
        await asyncio.wait_for(done.wait(), timeout=2)
        task.cancel()
        await task
    finally:
        task.cancel()
        await task
    assert ui.appendIncomingMessage.call_count == 2
    assert ui.appendIncomingMessage.call_args_list[0].args == (
        message.date.astimezone().strftime("%H:%M:%S"), "Alice", "旧时间来信",
    )


async def test_failed_enter_unsubscribes(chatSetup, monkeypatch):
    monkeypatch.setattr(chatUI, "loadHistory", AsyncMock(side_effect=RuntimeError("历史加载失败")))
    ui = _ui(chatSetup)
    with pytest.raises(RuntimeError, match="历史加载失败"):
        await ui.runSession()
    assert chatSetup.getInteractiveChatID() is None
