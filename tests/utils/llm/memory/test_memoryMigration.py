"""测试 LLM memory 新字段的幂等迁移。"""

from utils.llm.memory.database import _initSchema




def _columns(conn):
    return {row[1] for row in conn.execute("PRAGMA table_info(memory_entries)")}


def _createLegacyTable(conn):
    conn.execute("""
        CREATE TABLE memory_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope_type TEXT NOT NULL,
            scope_id TEXT NOT NULL,
            content BLOB NOT NULL,
            tags_json TEXT NOT NULL DEFAULT '[]',
            enabled INTEGER NOT NULL DEFAULT 1,
            priority INTEGER NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT 'manual',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)


def test_legacyTableMigratesWithoutChangingExistingRows(inMemoryDb):
    _createLegacyTable(inMemoryDb)
    inMemoryDb.execute(
        "INSERT INTO memory_entries (scope_type, scope_id, content, priority) "
        "VALUES ('global', 'global', 'legacy', 3)"
    )

    _initSchema(inMemoryDb)

    row = inMemoryDb.execute("SELECT * FROM memory_entries").fetchone()
    assert {"mode", "retrieval_hint"}.issubset(_columns(inMemoryDb))
    assert row["id"] == 1
    assert row["content"] == "legacy"
    assert row["priority"] == 3
    assert row["mode"] == "contextual"
    assert row["retrieval_hint"] is None


def test_partialAndRepeatedMigrationAreIdempotent(inMemoryDb):
    _createLegacyTable(inMemoryDb)
    inMemoryDb.execute(
        "ALTER TABLE memory_entries "
        "ADD COLUMN mode TEXT NOT NULL DEFAULT 'contextual'"
    )

    _initSchema(inMemoryDb)
    _initSchema(inMemoryDb)

    assert {"mode", "retrieval_hint"}.issubset(_columns(inMemoryDb))
