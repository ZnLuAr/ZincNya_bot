"""
tests/utils/test_chatHistory.py

chatHistory（utils/chatHistory.py）核心行为：
    - saveMessage/loadHistory 加密往返（content 落盘为密文、读出为明文）
    - 解密失败跳过兜底（密钥更换场景）
    - getChatList / getMessageCount / clearHistory
    - iterMessagesWithDateMarkers 的日期分隔插入

用真实临时 DB（monkeypatch DB 路径），走项目 Database 封装——不 mock crypto。
"""

import asyncio
import sqlite3
from contextlib import closing
from datetime import datetime
from unittest.mock import patch

import pytest

from config import BOT_DISPLAY_NAME
from utils import chatHistory
from utils.chatHistory import (
    getMessageCount,
    getChatList,
    iterMessagesWithDateMarkers,
    loadHistory,
    recordBotMessage,
    recordIncomingMessage,
    saveMessage,
)



@pytest.fixture
def db(tmp_path, monkeypatch):
    """让 chatHistoryDB 指向临时库并初始化 schema"""
    fakeDbPath = tmp_path / "testChat.db"
    from utils.core.database import Database
    fakeDb = Database(str(fakeDbPath), "TestChatHistory")
    monkeypatch.setattr(chatHistory, "chatHistoryDB", fakeDb)
    chatHistory.initDatabase()
    return fakeDb



async def _saveBatch(db, chatID, msgs):
    for direction, sender, content in msgs:
        assert await saveMessage(chatID, direction, sender, content) is True



class TestSaveLoadRoundtrip:
    async def test_roundtrip_plaintext_out(self, db):
        await _saveBatch(db, "111", [("incoming", "alice", "你好"), ("outgoing", "me", "你好喵")])
        history = await loadHistory("111")
        assert {m["content"] for m in history} == {"你好", "你好喵"}
        assert {m["direction"] for m in history} == {"incoming", "outgoing"}
        assert all(isinstance(m["timestamp"], datetime) for m in history)

    async def test_content_stored_encrypted(self, db, tmp_path):
        """落盘必须是密文：直接读 sqlite 检查 content 列不含明文"""
        import sqlite3
        await _saveBatch(db, "222", [("incoming", "bob", "秘密消息内容XYZ")])
        conn = sqlite3.connect(db._dbPath)
        try:
            raw = conn.execute("SELECT content FROM messages WHERE chat_id='222'").fetchone()[0]
        finally:
            conn.close()
        assert isinstance(raw, bytes)
        assert "秘密消息内容XYZ".encode("utf-8") not in raw     # 密文不含明文

    async def test_limit_returns_latest(self, db):
        """时间戳精确到秒——手动写 5 条跨秒的消息保证时序可判"""
        import sqlite3
        from utils.core.crypto import encryptText
        conn = sqlite3.connect(db._dbPath)
        for i in range(5):
            ts = f"2026-08-01 10:00:0{i}"
            conn.execute(
                "INSERT INTO messages (chat_id, direction, sender, content, timestamp) VALUES (?,?,?,?,?)",
                ("333", "incoming", "u", encryptText(f"msg{i}"), ts),
            )
        conn.commit(); conn.close()
        history = await loadHistory("333", limit=3)
        assert [m["content"] for m in history] == ["msg2", "msg3", "msg4"]    # 最旧在前，取最近 3 条

    async def test_order_oldest_first(self, db):
        """loadHistory 返回最旧在前（messages.reverse() 的语义）"""
        import sqlite3
        from utils.core.crypto import encryptText
        conn = sqlite3.connect(db._dbPath)
        conn.execute("INSERT INTO messages (chat_id, direction, sender, content, timestamp) VALUES (?,?,?,?,?)",
                     ("444", "incoming", "u", encryptText("first"), "2026-08-01 10:00:01"))
        conn.execute("INSERT INTO messages (chat_id, direction, sender, content, timestamp) VALUES (?,?,?,?,?)",
                     ("444", "incoming", "u", encryptText("second"), "2026-08-01 10:00:02"))
        conn.commit(); conn.close()
        assert (await loadHistory("444"))[0]["content"] == "first"

    async def test_same_second_order_by_id(self, db):
        """同秒双条（时间戳仅秒级精度）→ 按 id 决胜，返回顺序 = 插入顺序"""
        import sqlite3
        from utils.core.crypto import encryptText
        conn = sqlite3.connect(db._dbPath)
        conn.execute("INSERT INTO messages (chat_id, direction, sender, content, timestamp) VALUES (?,?,?,?,?)",
                     ("445", "incoming", "u", encryptText("先问"), "2026-08-01 10:00:00"))
        conn.execute("INSERT INTO messages (chat_id, direction, sender, content, timestamp) VALUES (?,?,?,?,?)",
                     ("445", "outgoing", "ZincNya~", encryptText("后答"), "2026-08-01 10:00:00"))
        conn.commit(); conn.close()
        history = await loadHistory("445")
        assert [m["content"] for m in history] == ["先问", "后答"]    # 插入序，非随机序
        assert [m["direction"] for m in history] == ["incoming", "outgoing"]



class TestRecordBotMessage:

    async def test_bot_message_roundtrip(self, db):
        """recordBotMessage 落盘为 outgoing + BOT_DISPLAY_NAME，content 明文往返"""
        assert await recordBotMessage("888", "锌酱的回复") is True
        history = await loadHistory("888")
        assert len(history) == 1
        assert history[0]["direction"] == "outgoing"
        assert history[0]["sender"] == BOT_DISPLAY_NAME
        assert history[0]["content"] == "锌酱的回复"


class TestIncomingMessageIdentity:

    async def test_concurrent_duplicate_is_inserted_once(self, db):
        """同一 Telegram 来信并发处理，只允许一个调用返回新增记录 ID。"""
        results = await asyncio.gather(*(
            recordIncomingMessage(
                "-100", "alice", "重复投递",
                telegramMessageID=42, timestamp="2026-10-25 12:34:56",
            ) for _ in range(8)
        ))
        assert sum(result is not None for result in results) == 1
        history = await loadHistory("-100")
        assert len(history) == 1
        assert history[0]["timestamp"] == datetime(2026, 10, 25, 12, 34, 56)
        assert history[0]["id"] == next(result for result in results if result is not None)

    async def test_identity_is_scoped_to_chat_and_does_not_compare_text(self, db):
        for chatID, messageID in (("-100", 1), ("-100", 2), ("-200", 1)):
            assert await recordIncomingMessage(
                chatID, "alice", "相同内容", telegramMessageID=messageID,
                timestamp="2026-10-25 12:34:56",
            ) is not None
        assert await getMessageCount() == 3

    async def test_old_schema_keeps_records_and_accepts_new_messages(self, tmp_path, monkeypatch):
        """旧库补列可重复执行，历史密文、时间和主键保持不变。"""
        from utils.core.crypto import encryptText
        from utils.core.database import Database

        path = tmp_path / "old.db"
        content = encryptText("旧记录")
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("""CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL,
                direction TEXT NOT NULL, sender TEXT, content BLOB NOT NULL, timestamp TEXT
            )""")
            conn.execute(
                "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?)",
                (27, "-100", "incoming", "alice", content, "2026-09-01 01:02:03"),
            )
            conn.commit()
            chatHistory._initSchema(conn)
            chatHistory._initSchema(conn)
            row = conn.execute("SELECT * FROM messages").fetchone()
            assert row == (27, "-100", "incoming", "alice", content, "2026-09-01 01:02:03", None)
        monkeypatch.setattr(chatHistory, "chatHistoryDB", Database(str(path), "OldChat"))
        assert await recordIncomingMessage(
            "-100", "alice", "新记录", telegramMessageID=28, timestamp="2026-10-25 12:34:56",
        ) is not None
        assert [message["content"] for message in await loadHistory("-100")] == ["旧记录", "新记录"]

    async def test_concurrent_overflow_archives_every_removed_message(self, db, tmp_path, monkeypatch):
        monkeypatch.setattr(chatHistory, "CHAT_HISTORY_LIMIT", 3)
        monkeypatch.setattr(chatHistory, "CHAT_BACKUP_DIR", str(tmp_path / "archives"))
        results = await asyncio.gather(*(
            recordIncomingMessage(
                "-100", "alice", f"消息 {i}", telegramMessageID=i,
                timestamp=f"2026-10-25 12:34:{i:02}",
            ) for i in range(10)
        ))
        assert all(result is not None for result in results)
        with closing(sqlite3.connect(db._dbPath)) as conn:
            retained = {row[0] for row in conn.execute("SELECT telegram_message_id FROM messages")}
        archived = set()
        for path in (tmp_path / "archives").glob("*.db"):
            with closing(sqlite3.connect(path)) as conn:
                archived.update(row[0] for row in conn.execute("SELECT telegram_message_id FROM messages"))
        assert retained == {7, 8, 9}
        assert retained.isdisjoint(archived)
        assert retained | archived == set(range(10))

    async def test_archive_failure_keeps_committed_messages(self, db, tmp_path, monkeypatch):
        monkeypatch.setattr(chatHistory, "CHAT_HISTORY_LIMIT", 1)
        blockedPath = tmp_path / "not-a-directory"
        blockedPath.write_text("阻止创建归档目录", encoding="utf-8")
        monkeypatch.setattr(chatHistory, "CHAT_BACKUP_DIR", str(blockedPath))
        for messageID in (1, 2):
            assert await recordIncomingMessage(
                "-100", "alice", "保留消息", telegramMessageID=messageID,
                timestamp="2026-10-25 12:34:56",
            ) is not None
        assert await getMessageCount("-100") == 2



class TestDecryptFallback:
    async def test_corrupt_row_skipped(self, db):
        """历史明文/坏密文行不拖垮查询——解密失败跳过并计数"""
        import sqlite3
        await _saveBatch(db, "555", [("incoming", "u", "好的消息")])
        # 直接写入一条坏密文
        conn = sqlite3.connect(db._dbPath)
        conn.execute("INSERT INTO messages (chat_id, direction, sender, content, timestamp) VALUES ('555', 'incoming', 'u', ?, datetime('now'))", (b"not-a-fernet-token",))
        conn.commit()
        conn.close()
        history = await loadHistory("555")
        assert [m["content"] for m in history] == ["好的消息"]    # 坏行被跳过，好行完好



class TestChatListAndCount:
    async def test_chat_list_fields(self, db):
        await _saveBatch(db, "111", [("incoming", "a", "x")])
        await _saveBatch(db, "222", [("incoming", "b", "y"), ("outgoing", "me", "z")])
        chats = {c["chat_id"]: c for c in await getChatList()}
        assert chats["222"]["message_count"] == 2
        assert chats["111"]["message_count"] == 1

    async def test_message_count(self, db):
        await _saveBatch(db, "666", [("incoming", "u", "a"), ("outgoing", "m", "b")])
        assert await getMessageCount("666") == 2
        assert await getMessageCount("nonexistent") == 0

    async def test_clear_history_single_chat(self, db):
        await _saveBatch(db, "777", [("incoming", "u", "x")])
        await _saveBatch(db, "888", [("incoming", "u", "y")])
        assert await chatHistory.clearHistory("777") is True
        assert await getMessageCount("777") == 0
        assert await getMessageCount("888") == 1     # 其它 chat 不受影响



class TestDateMarkers:
    def test_marker_inserted_between_days(self):
        msgs = [
            {"content": "a", "timestamp": datetime(2026, 8, 1, 10, 0)},
            {"content": "b", "timestamp": datetime(2026, 8, 2, 9, 0)},
        ]
        items = list(iterMessagesWithDateMarkers(msgs))
        kinds = [k for k, _ in items]
        # 实现行为：首条消息前也插日期标记（lastDate=None 视为变化）
        assert kinds == ["date", "message", "date", "message"]
        assert items[0][1].startswith("2026/08/01")
        assert items[2][1].startswith("2026/08/02")

    def test_same_day_no_marker(self):
        msgs = [
            {"content": "a", "timestamp": datetime(2026, 8, 1, 10, 0)},
            {"content": "b", "timestamp": datetime(2026, 8, 1, 11, 0)},
        ]
        items = list(iterMessagesWithDateMarkers(msgs))
        assert [k for k, _ in items] == ["date", "message", "message"]    # 同日无额外标记
