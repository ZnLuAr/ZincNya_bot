"""
utils/llm/memory/database.py

LLM structured memory 数据库模块。

提供：
    - memory_entries 表初始化
    - CRUD 接口
    - 分层检索（global -> chat -> user -> session）
    - 上下文格式化（含 id/src，供 LLM 识别可操作的 inferred 记忆）
    - 检索摘要
"""




import json
from datetime import datetime
from typing import Any, Optional

from config import LLM_MEMORY_DB_PATH

from utils.core.database import Database
from utils.logger import logSystemEvent, LogLevel


TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
MEMORY_SCOPE_GLOBAL = "global"
MEMORY_SCOPE_CHAT = "chat"
MEMORY_SCOPE_USER = "user"
MEMORY_SCOPE_SESSION = "session"
VALID_SCOPE_TYPES = {
    MEMORY_SCOPE_GLOBAL,
    MEMORY_SCOPE_CHAT,
    MEMORY_SCOPE_USER,
    MEMORY_SCOPE_SESSION,
}
VALID_SOURCES = {"manual", "inferred"}


memoryDB = Database(LLM_MEMORY_DB_PATH, "LLMMemory")




def _normalizeScope(scopeType: str, scopeID: str | int | None) -> tuple[str, str]:
    """规范化 scope_type / scope_id。"""
    scopeType = str(scopeType).strip().lower()
    if scopeType not in VALID_SCOPE_TYPES:
        raise ValueError(f"无效的 scopeType: {scopeType}")

    if scopeType == MEMORY_SCOPE_GLOBAL:
        return MEMORY_SCOPE_GLOBAL, "global"

    if scopeID is None or str(scopeID).strip() == "":
        raise ValueError(f"scopeType={scopeType} 时 scopeID 不能为空")

    return scopeType, str(scopeID)


def _normalizeTags(tags: Optional[list[str]]) -> list[str]:
    """规范化 tags，去重并去除空白。"""
    if not tags:
        return []

    result = []
    seen = set()
    for tag in tags:
        tag = str(tag).strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        result.append(tag)
    return result


def _normalizeSource(source: str) -> str:
    """规范化 source。"""
    source = str(source).strip().lower()
    if source not in VALID_SOURCES:
        raise ValueError(f"无效的 source: {source}")
    return source


def _initSchema(conn):
    """初始化表结构（由 initDatabase 调用）"""
    cursor = conn.cursor()

    cursor.execute("""
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
        )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_memory_scope ON memory_entries(scope_type, scope_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_memory_enabled ON memory_entries(enabled)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_memory_priority ON memory_entries(priority DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_memory_updated_at ON memory_entries(updated_at DESC)")


def initDatabase():
    """初始化 structured memory 数据库（由 appLifecycle 调用）"""
    memoryDB.initSchema(_initSchema)


def _rowToMemoryDict(row) -> dict[str, Any]:
    """将 sqlite3.Row 转换为 memory 字典。"""
    return {
        "id": row["id"],
        "scope_type": row["scope_type"],
        "scope_id": row["scope_id"],
        "content": row["content"],
        "tags": json.loads(row["tags_json"] or "[]"),
        "enabled": bool(row["enabled"]),
        "priority": row["priority"],
        "source": row["source"],
        "created_at": datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
        "updated_at": datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else None,
    }


async def addMemory(
    scopeType: str,
    scopeID: str | int | None,
    content: str,
    *,
    tags: Optional[list[str]] = None,
    priority: int = 0,
    source: str = "manual",
    enabled: bool = True,
) -> Optional[int]:
    """新增一条 structured memory。"""
    try:
        scopeType, scopeID = _normalizeScope(scopeType, scopeID)
        source = _normalizeSource(source)
        tagsJson = json.dumps(_normalizeTags(tags), ensure_ascii=False)
        content = str(content).strip()
        if not content:
            raise ValueError("content 不能为空")

        def _query(conn):
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO memory_entries (
                    scope_type, scope_id, content, tags_json, enabled, priority, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (scopeType, scopeID, content, tagsJson, int(enabled), int(priority), source)
            )
            return cursor.lastrowid

        return await memoryDB.run(_query)

    except Exception as e:
        await logSystemEvent(
            "LLM memory 添加失败",
            str(e),
            LogLevel.ERROR,
            exception=e,
        )
        return None


async def getMemoryByID(memoryID: int) -> Optional[dict[str, Any]]:
    """按 ID 获取单条 memory。"""
    try:
        def _query(conn):
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM memory_entries WHERE id = ?", (memoryID,))
            row = cursor.fetchone()
            return _rowToMemoryDict(row) if row else None

        return await memoryDB.run(_query)

    except Exception as e:
        await logSystemEvent(
            "LLM memory 查询失败",
            f"ID {memoryID}: {e}",
            LogLevel.ERROR,
            exception=e,
        )
        return None


async def getMemories(
    scopeType: Optional[str] = None,
    scopeID: str | int | None = None,
    enabledOnly: bool = False,
    limit: int = 0,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """按条件列出 memory。"""
    try:
        if scopeType is not None:
            scopeType, scopeID = _normalizeScope(scopeType, scopeID)

        def _query(conn):
            cursor = conn.cursor()
            clauses = []
            params = []

            if scopeType is not None:
                clauses.append("scope_type = ?")
                params.append(scopeType)
                clauses.append("scope_id = ?")
                params.append(scopeID)

            if enabledOnly:
                clauses.append("enabled = 1")

            query = "SELECT * FROM memory_entries"
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            query += " ORDER BY priority DESC, updated_at DESC, id DESC"

            if limit > 0:
                query += " LIMIT ? OFFSET ?"
                params.extend([limit, offset])

            cursor.execute(query, tuple(params))
            return [_rowToMemoryDict(row) for row in cursor.fetchall()]

        return await memoryDB.run(_query)

    except Exception as e:
        await logSystemEvent(
            "LLM memory 列表加载失败",
            str(e),
            LogLevel.ERROR,
            exception=e,
        )
        return []


async def updateMemory(
    memoryID: int,
    *,
    content: Optional[str] = None,
    tags: Optional[list[str]] = None,
    priority: Optional[int] = None,
    enabled: Optional[bool] = None,
    source: Optional[str] = None,
) -> bool:
    """更新 memory。"""
    try:
        def _query(conn):
            cursor = conn.cursor()
            updates = []
            params = []

            if content is not None:
                normalizedContent = str(content).strip()
                if not normalizedContent:
                    raise ValueError("content 不能为空")
                updates.append("content = ?")
                params.append(normalizedContent)

            if tags is not None:
                updates.append("tags_json = ?")
                params.append(json.dumps(_normalizeTags(tags), ensure_ascii=False))

            if priority is not None:
                updates.append("priority = ?")
                params.append(int(priority))

            if enabled is not None:
                updates.append("enabled = ?")
                params.append(int(enabled))

            if source is not None:
                updates.append("source = ?")
                params.append(_normalizeSource(source))

            if not updates:
                return True

            updates.append("updated_at = CURRENT_TIMESTAMP")
            params.append(memoryID)
            cursor.execute(
                f"UPDATE memory_entries SET {', '.join(updates)} WHERE id = ?",
                tuple(params)
            )
            return cursor.rowcount > 0

        return await memoryDB.run(_query)

    except Exception as e:
        await logSystemEvent(
            "LLM memory 更新失败",
            f"ID {memoryID}: {e}",
            LogLevel.ERROR,
            exception=e,
        )
        return False


async def deleteMemory(memoryID: int) -> bool:
    """删除 memory。"""
    try:
        def _query(conn):
            cursor = conn.cursor()
            cursor.execute("DELETE FROM memory_entries WHERE id = ?", (memoryID,))
            return cursor.rowcount > 0

        return await memoryDB.run(_query)

    except Exception as e:
        await logSystemEvent(
            "LLM memory 删除失败",
            f"ID {memoryID}: {e}",
            LogLevel.ERROR,
            exception=e,
        )
        return False


async def retrieveMemories(
    chatID: str | int | None = None,
    userID: str | int | None = None,
    sessionID: str | int | None = None,
    perScopeLimit: int = 3,
    totalLimit: int = 10,
) -> list[dict[str, Any]]:
    """按分层顺序检索 memory。"""
    try:
        scopes = [(MEMORY_SCOPE_GLOBAL, "global")]
        if chatID is not None:
            scopes.append((MEMORY_SCOPE_CHAT, str(chatID)))
        if userID is not None:
            scopes.append((MEMORY_SCOPE_USER, str(userID)))
        if sessionID is not None:
            scopes.append((MEMORY_SCOPE_SESSION, str(sessionID)))

        memories = []
        for scopeType, scopeID in scopes:
            if len(memories) >= totalLimit:
                break

            remaining = totalLimit - len(memories)
            scopeLimit = min(max(perScopeLimit, 0), remaining)
            if scopeLimit <= 0:
                break

            items = await getMemories(
                scopeType=scopeType,
                scopeID=scopeID,
                enabledOnly=True,
                limit=scopeLimit,
            )
            memories.extend(items)

        return memories

    except Exception as e:
        await logSystemEvent(
            "LLM memory 检索失败",
            str(e),
            LogLevel.ERROR,
            exception=e,
        )
        return []


def buildMemoryContextBlock(memories: list[dict[str, Any]]) -> str:
    """将检索到的 memories 格式化为上下文块。"""
    if not memories:
        return ""

    lines = ["[以下是长期记忆，仅作参考]"]
    for item in memories:
        scopeType = item.get("scope_type", "?")
        scopeID = item.get("scope_id", "?")
        priority = item.get("priority", 0)
        content = item.get("content", "")
        tags = item.get("tags") or []

        source = item.get("source", "manual")
        header = f"- ({scopeType}:{scopeID}, p={priority}, id={item['id']}, src={source}) {content}"
        if tags:
            header += f" [tags: {', '.join(tags)}]"
        lines.append(header)

    return "\n".join(lines)


def summarizeRetrievedMemories(memories: list[dict[str, Any]]) -> str:
    """生成检索摘要，用于日志和可观测性。"""
    if not memories:
        return "命中 0 条"

    scopeCounts: dict[str, int] = {}
    ids = []
    for item in memories:
        scopeType = item.get("scope_type", "?")
        scopeCounts[scopeType] = scopeCounts.get(scopeType, 0) + 1
        ids.append(str(item.get("id", "?")))

    countsText = ", ".join(f"{k}={v}" for k, v in scopeCounts.items())
    return f"命中 {len(memories)} 条 | {countsText} | ids: {', '.join(ids)}"
