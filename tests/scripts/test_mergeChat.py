"""验证聊天历史合并兼容旧库，并保留 Telegram 消息身份。"""

import sqlite3
from contextlib import closing

from cryptography.fernet import Fernet
import pytest

from scripts.merge_data import (
    ScriptContext, _merge_chat, analyze_chat_diff, apply_chat_history,
    ensure_chat_schema, plan_chat_history,
)


def _createChatDB(path, *, modern, fernet, messageIDs):
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("""CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL,
            direction TEXT NOT NULL, sender TEXT, content BLOB NOT NULL, timestamp TEXT
        )""")
        if modern:
            ensure_chat_schema(conn)
        for messageID in messageIDs:
            columns = "chat_id, direction, sender, content, timestamp"
            values = ["-100", "incoming", "alice", fernet.encrypt("相同内容".encode()), "2026-10-25 12:34:56"]
            if modern:
                columns += ", telegram_message_id"
                values.append(messageID)
            conn.execute(
                f"INSERT INTO messages ({columns}) VALUES ({', '.join('?' for _ in values)})", values,
            )
        conn.commit()


@pytest.mark.parametrize("modern", [False, True])
@pytest.mark.parametrize("interactive", [False, True])
def test_merge_preserves_identity_and_upgrades_old_target(tmp_path, modern, interactive):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    key = Fernet.generate_key()
    (source / ".chatKey").write_bytes(key)
    (target / ".chatKey").write_bytes(key)
    fernet = Fernet(key)
    _createChatDB(source / "chatHistory.db", modern=modern, fernet=fernet, messageIDs=[1, 2])
    _createChatDB(target / "chatHistory.db", modern=False, fernet=fernet, messageIDs=[])
    if interactive:
        diff = analyze_chat_diff(target / "chatHistory.db", source / "chatHistory.db", target / ".chatKey")
        assert _merge_chat(diff, source / "chatHistory.db", target / "chatHistory.db")
    else:
        ctx = ScriptContext(
            source_data=source, target_data=target, selected_sections={"chat-history"}, apply=True,
            max_preview=1, max_json_diff_lines=300, show_full_content=False, no_content=False,
            backup_dir=target / "backup", allow_wal=True,
        )
        plan = plan_chat_history(ctx)
        assert not plan.errors
        assert apply_chat_history(plan) == 2
        if modern:
            assert apply_chat_history(plan) == 0
    with closing(sqlite3.connect(target / "chatHistory.db")) as conn:
        messageIDs = [row[0] for row in conn.execute("SELECT telegram_message_id FROM messages ORDER BY id")]
    if modern:
        assert messageIDs == [1, 2]
        diff = analyze_chat_diff(target / "chatHistory.db", source / "chatHistory.db", target / ".chatKey")
        assert diff.remote_only == []
    else:
        # 旧记录没有 Telegram 消息 ID，交互合并仍按旧规则比较内容和时间。
        assert messageIDs == ([None] if interactive else [None, None])
