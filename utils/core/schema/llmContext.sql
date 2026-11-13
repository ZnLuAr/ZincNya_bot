-- 工作上下文与聊天当前指向。lastActivity 使用 Unix 秒时间戳。
CREATE TABLE IF NOT EXISTS contexts (
    contextID TEXT PRIMARY KEY,
    chatID TEXT NOT NULL,
    parentContextID TEXT,
    parentPosition INTEGER,
    lastActivity REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS currentContexts (
    chatID TEXT PRIMARY KEY,
    contextID TEXT NOT NULL REFERENCES contexts(contextID) ON DELETE CASCADE
);

-- metadata 预留加密的任务元数据；任务结束状态与模型调用次数供恢复和诊断使用。
CREATE TABLE IF NOT EXISTS tasks (
    taskID TEXT PRIMARY KEY,
    contextID TEXT NOT NULL REFERENCES contexts(contextID) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN (
        'running', 'completed', 'limitReached', 'stopped', 'failed', 'interrupted'
    )),
    modelCalls INTEGER NOT NULL DEFAULT 0 CHECK (modelCalls >= 0),
    metadata BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_context_status ON tasks(contextID, status);

-- position 是上下文内从 1 递增的记录位置。分叉保留来源 taskID，记录归新上下文所有。
CREATE TABLE IF NOT EXISTS records (
    contextID TEXT NOT NULL REFERENCES contexts(contextID) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (position > 0),
    taskID TEXT NOT NULL,
    operationIndex INTEGER NOT NULL CHECK (operationIndex > 0),
    kind TEXT NOT NULL CHECK (kind = 'operation'),
    status TEXT NOT NULL CHECK (status IN (
        'executing', 'done', 'submitted', 'failed', 'uncertain', 'skipped', 'expired'
    )),
    content BLOB NOT NULL,
    createdAt REAL NOT NULL,
    PRIMARY KEY (contextID, position),
    UNIQUE (contextID, taskID, operationIndex)
);

-- 健康试写使用独立行；成功提交后才能解除实例的暂停标记。
CREATE TABLE IF NOT EXISTS storageHealth (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    probe INTEGER NOT NULL
);
INSERT OR IGNORE INTO storageHealth(id, probe) VALUES (1, 0);