-- chatHistory schema snapshot
-- 由 utils/chatHistory.py:_initSchema 通过 executescript 加载。
-- 本文件描述 messages 表的"初始幂等 schema"；旧库迁移由 Python 代码额外执行。

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    sender TEXT,
    content BLOB NOT NULL,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_chat_id ON messages(chat_id);
CREATE INDEX IF NOT EXISTS idx_timestamp ON messages(timestamp);
