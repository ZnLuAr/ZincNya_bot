"""测试 memory 隐私字段的幂等加密迁移。"""

import sqlite3

import pytest
from cryptography.fernet import Fernet

import utils.core.crypto as crypto
from scripts import migrate_encrypt


@pytest.fixture
def migrationKey(tmp_path, monkeypatch):
    keyPath = tmp_path / ".chatKey"
    keyPath.write_bytes(Fernet.generate_key())
    monkeypatch.setattr(crypto, "KEY_PATH", str(keyPath))
    monkeypatch.setattr(crypto, "_fernetCache", None)
    return crypto.getFernet()


def _makeMemoryDB(path, *, withHint=True, hint=None):
    conn = sqlite3.connect(path)
    columns = (
        "id INTEGER PRIMARY KEY, content BLOB, "
        + ("retrieval_hint BLOB" if withHint else "dummy TEXT")
    )
    conn.execute(f"CREATE TABLE memory_entries ({columns})")
    if withHint:
        conn.execute(
            "INSERT INTO memory_entries (id, content, retrieval_hint) VALUES (1, ?, ?)",
            ("明文正文", hint),
        )
    else:
        conn.execute(
            "INSERT INTO memory_entries (id, content, dummy) VALUES (1, ?, ?)",
            ("明文正文", "x"),
        )
    conn.commit()
    conn.close()


def test_migrate_memory_content_and_hint(migrationKey, tmp_path):
    dbPath = tmp_path / "memory.db"
    _makeMemoryDB(dbPath, hint="明文说明")

    contentResult = migrate_encrypt.migrateTable(
        str(dbPath), "memory_entries", "id", "content", False
    )
    hintResult = migrate_encrypt.migrateTable(
        str(dbPath), "memory_entries", "id", "retrieval_hint", False
    )

    assert contentResult == (0, 1)
    assert hintResult == (0, 1)
    conn = sqlite3.connect(dbPath)
    content, hint = conn.execute(
        "SELECT content, retrieval_hint FROM memory_entries WHERE id = 1"
    ).fetchone()
    conn.close()
    assert migrationKey.decrypt(content).decode("utf-8") == "明文正文"
    assert migrationKey.decrypt(hint).decode("utf-8") == "明文说明"


def test_migrate_memory_is_idempotent_and_skips_null_hint(migrationKey, tmp_path):
    dbPath = tmp_path / "memory.db"
    _makeMemoryDB(dbPath, hint=None)
    conn = sqlite3.connect(dbPath)
    conn.execute(
        "UPDATE memory_entries SET content = ? WHERE id = 1",
        (migrationKey.encrypt("已加密正文".encode("utf-8")),),
    )
    conn.commit()
    conn.close()

    first = migrate_encrypt.migrateTable(
        str(dbPath), "memory_entries", "id", "retrieval_hint", False
    )
    second = migrate_encrypt.migrateTable(
        str(dbPath), "memory_entries", "id", "retrieval_hint", False
    )
    contentSecond = migrate_encrypt.migrateTable(
        str(dbPath), "memory_entries", "id", "content", False
    )

    assert first == (1, 0)
    assert second == (1, 0)
    assert contentSecond == (1, 0)


def test_migrate_memory_old_schema_without_hint_is_skipped(migrationKey, tmp_path):
    dbPath = tmp_path / "memory.db"
    _makeMemoryDB(dbPath, withHint=False)

    result = migrate_encrypt.migrateTable(
        str(dbPath), "memory_entries", "id", "retrieval_hint", False
    )

    assert result == (0, 0)
