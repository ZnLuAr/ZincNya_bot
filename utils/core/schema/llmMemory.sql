-- LLM structured memory schema snapshot
-- 由 utils/llm/memory/database.py:_initSchema 通过 executescript 加载。

CREATE TABLE IF NOT EXISTS memory_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    content TEXT NOT NULL,
    tags_json TEXT NOT NULL DEFAULT '[]',
    enabled INTEGER NOT NULL DEFAULT 1,
    priority INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'manual',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_memory_scope ON memory_entries(scope_type, scope_id);
CREATE INDEX IF NOT EXISTS idx_memory_enabled ON memory_entries(enabled);
CREATE INDEX IF NOT EXISTS idx_memory_priority ON memory_entries(priority DESC);
CREATE INDEX IF NOT EXISTS idx_memory_updated_at ON memory_entries(updated_at DESC);
