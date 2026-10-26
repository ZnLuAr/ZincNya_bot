"""测试资源清理前的停接标记与重启意图，不创建真实Telegram应用。"""

from types import SimpleNamespace
import asyncio
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from utils.core import appLifecycle
from utils.core.stateManager import StateManager
from utils import chatHistory
from utils.core.database import Database
from telegram import Chat, Message, User




@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("cleanupFailure", [False, True])
async def test_stopAppSignalsBeforeCleanupAndPreservesRestart(monkeypatch, restart, cleanupFailure):
    """普通关闭及清理失败都先设置事件，已请求重启不能被覆盖为关机。"""
    state = StateManager()
    if restart:
        state.requestRestart()
    state.requestShutdown = MagicMock(side_effect=AssertionError("restart flag would be erased"))
    events = []

    async def cleanup():
        """清理入口必须已观察到停接，不依赖后续updater停止。"""
        assert state.getShutdownEvent().is_set()
        assert state.isRestartRequested() is restart
        events.append("cleanup")
        if cleanupFailure:
            raise RuntimeError("synthetic cleanup failure")

    async def stopUpdater():
        """记录updater只在资源清理之后停止。"""
        events.append("updater")

    async def stop():
        """记录应用停止且不依赖上一步成功。"""
        events.append("stop")

    async def shutdown():
        """记录应用最终释放。"""
        events.append("shutdown")

    app = SimpleNamespace(updater=SimpleNamespace(stop=stopUpdater), stop=stop, shutdown=shutdown)
    monkeypatch.setattr(appLifecycle, "getStateManager", lambda: state)
    monkeypatch.setattr(appLifecycle, "cleanupAllResources", cleanup)
    await appLifecycle.stopApp(app)
    assert events == ["cleanup", "updater", "stop", "shutdown"]
    assert state.isRestartRequested() is restart
    state.requestShutdown.assert_not_called()


@pytest.fixture
def incomingSetup(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "incoming.db"), "IncomingTest")
    monkeypatch.setattr(chatHistory, "chatHistoryDB", db)
    chatHistory.initDatabase()
    state = StateManager()
    monkeypatch.setattr(appLifecycle, "getStateManager", lambda: state)
    return db, state


@pytest.mark.parametrize("allowed,expected", [({"-100"}, 1), ({"42"}, 1), (set(), 0)])
async def test_collector_records_by_chat_or_sender(incomingSetup, monkeypatch, allowed, expected):
    """记录范围不取决于 LLM 开关、触发词或是否进入聊天界面。"""
    monkeypatch.setattr(appLifecycle, "whetherAuthorizedUser", lambda value: str(value) in allowed)
    message = Message(
        message_id=7, date=datetime(2026, 10, 25, 12, 34, tzinfo=timezone.utc),
        chat=Chat(-100, "supergroup"), from_user=User(42, "Alice", False), text="普通群聊消息",
    )
    await appLifecycle.messageCollector(SimpleNamespace(message=message), None)
    history = await chatHistory.loadHistory("-100")
    assert len(history) == expected
    if expected:
        assert history[0]["content"] == "普通群聊消息"
        assert history[0]["timestamp"] == message.date.astimezone().replace(tzinfo=None)


async def test_collector_persists_media_and_notifies_after_commit_once(incomingSetup, monkeypatch):
    db, state = incomingSetup
    monkeypatch.setattr(appLifecycle, "whetherAuthorizedUser", lambda value: str(value) == "-100")
    queue = state.subscribeChatMessages("-100")
    originalNotify = state.notifyChatMessage

    def notifyAfterCommit(chatID, recordID, message):
        # 通知发生的当下，独立连接已经能读到这条记录。
        with closing(sqlite3.connect(db._dbPath)) as conn:
            assert conn.execute("SELECT id FROM messages WHERE id = ?", (recordID,)).fetchone() is not None
        originalNotify(chatID, recordID, message)

    monkeypatch.setattr(state, "notifyChatMessage", notifyAfterCommit)
    from telegram import PhotoSize
    message = Message(
        message_id=8, date=datetime(2026, 10, 25, 12, 34, tzinfo=timezone.utc),
        chat=Chat(-100, "supergroup"), from_user=None,
        photo=[PhotoSize("photo", "unique", 100, 100)], caption="图片说明",
    )
    await asyncio.gather(*(
        appLifecycle.messageCollector(SimpleNamespace(message=message), None) for _ in range(4)
    ))
    recordID, notification = queue.get_nowait()
    assert notification is message
    assert queue.empty()
    history = await chatHistory.loadHistory("-100")
    assert history[0]["id"] == recordID
    assert history[0]["content"] == "[图片] 图片说明"
    assert len(history) == 1


async def test_failed_write_does_not_notify(incomingSetup, monkeypatch):
    db, state = incomingSetup
    monkeypatch.setattr(appLifecycle, "whetherAuthorizedUser", lambda value: True)
    await db.run(lambda conn: conn.execute("""CREATE TRIGGER reject_message AFTER INSERT ON messages
        BEGIN SELECT RAISE(ABORT, '保存失败'); END"""))
    queue = state.subscribeChatMessages("-100")
    message = Message(9, datetime.now(timezone.utc), Chat(-100, "supergroup"), text="保存失败")
    await appLifecycle.messageCollector(SimpleNamespace(message=message), None)
    assert queue.empty()
    assert await chatHistory.getMessageCount("-100") == 0


async def test_initialize_registers_collector_and_initializes_history_without_llm(monkeypatch):
    """所有可选模块关闭时，收信入口及聊天数据库仍由核心启动流程提供。"""
    state = StateManager()
    monkeypatch.setattr(appLifecycle, "getStateManager", lambda: state)
    monkeypatch.setattr("config.migrateLegacyLLMPaths", lambda: None)
    monkeypatch.setattr("utils.moduleManager.getAllModules", lambda: {})
    for name in ("initLogger", "initErrorHandler", "loadHandlers", "setupAsyncioErrorHandler"):
        monkeypatch.setattr(appLifecycle, name, MagicMock())
    initHistory = MagicMock()
    monkeypatch.setattr(appLifecycle, "initDatabase", initHistory)
    app = MagicMock()
    appLifecycle.initializeApp(app)
    initHistory.assert_called_once()
    assert app.add_handler.call_args.kwargs["group"] == -1
    assert app.add_handler.call_args.args[0].callback is appLifecycle.messageCollector
