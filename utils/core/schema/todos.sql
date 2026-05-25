-- Todos schema snapshot
-- 由 utils/todos/database.py:_initSchema 通过 executescript 加载。
-- 旧库的 ALTER TABLE 迁移（添加 reminded 列）仍在 Python 代码中执行。

CREATE TABLE IF NOT EXISTS todos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    content TEXT NOT NULL,
    remind_time DATETIME,
    priority TEXT DEFAULT 'P_',
    status TEXT DEFAULT 'pending',
    reminded INTEGER DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    completed_at DATETIME
);

CREATE INDEX IF NOT EXISTS idx_chat_user ON todos(chat_id, user_id);
CREATE INDEX IF NOT EXISTS idx_status ON todos(status);
CREATE INDEX IF NOT EXISTS idx_remind_time ON todos(remind_time, status);
