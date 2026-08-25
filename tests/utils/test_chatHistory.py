"""
tests/utils/test_chatHistory.py

chatHistory（utils/chatHistory.py）核心行为：
    - saveMessage/loadHistory 加密往返（content 落盘为密文、读出为明文）
    - 解密失败跳过兜底（密钥更换场景）
    - getChatList / getMessageCount / clearHistory
    - iterMessagesWithDateMarkers 的日期分隔插入

用真实临时 DB（monkeypatch DB 路径），走项目 Database 封装——不 mock crypto。
"""

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
