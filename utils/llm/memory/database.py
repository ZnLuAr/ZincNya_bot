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

from config import (
    DB_TIMESTAMP_FORMAT,
    LLM_MEMORY_DB_PATH,
    LLM_MEMORY_HINT_MAX_CHARS,
    LLM_MEMORY_PRIORITY_CAP,
)

from utils.core.database import Database
from utils.core.schema import loadSchema
from utils.core.crypto import encryptText, decryptText
from utils.core.stateManager import getStateManager
from utils.core.logger import logSystemEvent, LogLevel
from utils.llm.promptSafety import neutralizePromptDelimiters

from .types import MemoryWriteGuard, buildMemoryStateFingerprint


TIMESTAMP_FORMAT = DB_TIMESTAMP_FORMAT  # 数据库时间戳格式（复用 config）
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
MEMORY_MODE_CONTEXTUAL = "contextual"
MEMORY_MODE_PINNED = "pinned"
VALID_MEMORY_MODES = {MEMORY_MODE_CONTEXTUAL, MEMORY_MODE_PINNED}
_REQUIRED_SCHEMA_COLUMNS = {"mode", "retrieval_hint"}
# scope 专属度：session（最贴当前对话）> user > chat > global（最泛）。
# 仅作 priority 打平时的兜底排序键，不构成硬名额。
# 检索排序与 TUI 列表共用，公开导出，不应在别处复制字面量。
MEMORY_SCOPE_RANK = {
    MEMORY_SCOPE_SESSION: 3,
    MEMORY_SCOPE_USER: 2,
    MEMORY_SCOPE_CHAT: 1,
    MEMORY_SCOPE_GLOBAL: 0,
}


memoryDB = Database(LLM_MEMORY_DB_PATH, "LLMMemory")


class MemoryWriteConflict(RuntimeError):
    """写入目标与审核/执行前快照不一致，或越过 pinned 写入边界。"""




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


def normalizeMemoryPriority(priority: int) -> int:
    """将 priority 规范化为业务允许范围内的整数，并拒绝布尔值。"""
    if isinstance(priority, bool):
        raise ValueError("priority 必须是整数")
    try:
        priority = int(priority)
    except (TypeError, ValueError) as exc:
        raise ValueError("priority 必须是整数") from exc
    if priority < 0 or priority > LLM_MEMORY_PRIORITY_CAP:
        raise ValueError(f"priority 必须在 0-{LLM_MEMORY_PRIORITY_CAP} 之间")
    return priority


def normalizeMemoryMode(mode: str) -> str:
    """规范化并校验 ``contextual`` / ``pinned`` memory 模式。"""
    if not isinstance(mode, str):
        raise ValueError("mode 必须是字符串")
    mode = mode.strip().lower()
    if mode not in VALID_MEMORY_MODES:
        raise ValueError(f"mode 必须是 {' / '.join(sorted(VALID_MEMORY_MODES))}")
    return mode


def normalizeRetrievalHint(retrievalHint) -> tuple[Optional[str], Optional[str]]:
    """规范化可选 hint，返回文本与可记录的拒绝原因码。"""
    if retrievalHint is None:
        return None, None
    if not isinstance(retrievalHint, str):
        return None, "hintType"

    retrievalHint = retrievalHint.strip()
    if not retrievalHint:
        return None, None
    if "\n" in retrievalHint or "\r" in retrievalHint:
        return None, "hintMultiline"
    if len(retrievalHint) > LLM_MEMORY_HINT_MAX_CHARS:
        return None, "hintTooLong"
    return retrievalHint, None


def _schemaColumns(conn) -> set[str]:
    """读取当前 ``memory_entries`` 的实际列集合。"""
    return {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(memory_entries)").fetchall()
    }


def _requireMemorySchema(conn) -> None:
    """在依赖新字段的读写前确认 memory schema 已完整迁移。"""
    missingColumns = _REQUIRED_SCHEMA_COLUMNS.difference(_schemaColumns(conn))
    if missingColumns:
        raise RuntimeError(
            f"memory schema 缺少字段: {', '.join(sorted(missingColumns))}"
        )


def _initSchema(conn):
    """初始化表结构（由 initDatabase 调用）"""
    conn.executescript(loadSchema("llmMemory"))
    columns = _schemaColumns(conn)
    if "mode" not in columns:
        conn.execute(
            "ALTER TABLE memory_entries "
            "ADD COLUMN mode TEXT NOT NULL DEFAULT 'contextual'"
        )
    if "retrieval_hint" not in columns:
        conn.execute("ALTER TABLE memory_entries ADD COLUMN retrieval_hint BLOB")
    _requireMemorySchema(conn)


def initDatabase():
    """初始化 structured memory 数据库（由 appLifecycle 调用）"""
    memoryDB.initSchema(_initSchema)


def _decryptContent(raw) -> str:
    """
    解密 content 列。

    正常情况下 content 是 encryptText 写入的密文 bytes。
    解密失败时（如个别历史明文行未被迁移脚本处理）回退为原始文本，
    避免单条记录拖垮整次检索。
    """
    try:
        return decryptText(raw)
    except Exception:
        # 兜底：可能是未加密的历史明文（bytes 或 str）
        if isinstance(raw, bytes):
            return raw.decode("utf-8", errors="replace")
        return str(raw)


def _decryptRetrievalHint(raw) -> Optional[str]:
    """解密并重新校验 hint；坏密文或旧非法值按不存在处理。"""
    if raw is None:
        return None
    try:
        value = decryptText(raw)
    except Exception:
        return None
    normalized, reasonCode = normalizeRetrievalHint(value)
    return normalized if reasonCode is None else None


def _parseTimestamp(raw):
    """将 SQLite 时间值解析为 ``datetime``，坏值降级为 ``None``。"""
    if raw is None or isinstance(raw, datetime):
        return raw
    try:
        return datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None


def _rowToMemoryDict(row) -> dict[str, Any]:
    """将 sqlite3.Row 转换为 memory 字典。"""
    columns = set(row.keys())
    return {
        "id": row["id"],
        "scope_type": row["scope_type"],
        "scope_id": row["scope_id"],
        "content": _decryptContent(row["content"]),
        "tags": json.loads(row["tags_json"] or "[]"),
        "enabled": bool(row["enabled"]),
        "priority": row["priority"],
        "source": row["source"],
        "mode": row["mode"] if "mode" in columns else MEMORY_MODE_CONTEXTUAL,
        "retrievalHint": (
            _decryptRetrievalHint(row["retrieval_hint"])
            if "retrieval_hint" in columns else None
        ),
        "created_at": _parseTimestamp(row["created_at"]),
        "updated_at": _parseTimestamp(row["updated_at"]),
    }


def _checkWriteGuard(
    memory: dict,
    guard: MemoryWriteGuard,
    *,
    requestedMode: Optional[str] = None,
) -> None:
    """在写事务内校验 source、scope、完整状态和 pinned 授权。"""
    # guard 检查必须在同一条数据库事务内完成，保证“审核时看到的状态”和“实际
    # UPDATE/DELETE 的目标状态”之间没有可被并发写入插入的窗口。
    if memory.get("source") != "inferred":
        raise MemoryWriteConflict("目标不是 inferred memory")

    scopeType, scopeID = _normalizeScope(guard.scopeType, guard.scopeID)
    if memory.get("scope_type") != scopeType or memory.get("scope_id") != scopeID:
        raise MemoryWriteConflict("目标 scope 已变化")
    if buildMemoryStateFingerprint(memory) != guard.expectedState:
        raise MemoryWriteConflict("目标状态已变化")

    touchesPinned = (
        memory.get("mode") == MEMORY_MODE_PINNED
        or requestedMode == MEMORY_MODE_PINNED
    )
    if touchesPinned and not guard.allowPinned:
        raise MemoryWriteConflict("pinned memory 需要独立人工审核")


async def _notifyMemoryChanged(memoryID: int) -> None:
    """在数据库提交后通知可选 runtime；通知失败不能回滚持久化。"""
    try:
        runtime = getStateManager().getMemoryRuntime()
        if runtime is not None:
            runtime.notifyMemoryChanged(memoryID)
    except Exception as exc:
        await logSystemEvent(
            "LLM memory 索引通知失败",
            f"ID {memoryID}: {type(exc).__name__}",
            LogLevel.WARNING,
            exception=exc,
        )




async def addMemory(
    scopeType: str,
    scopeID: str | int | None,
    content: str,
    *,
    tags: Optional[list[str]] = None,
    priority: int = 0,
    source: str = "manual",
    enabled: bool = True,
    mode: str = MEMORY_MODE_CONTEXTUAL,
    retrievalHint: Optional[str] = None,
) -> Optional[int]:
    """新增一条 structured memory。"""
    try:
        scopeType, scopeID = _normalizeScope(scopeType, scopeID)
        source = _normalizeSource(source)
        priority = normalizeMemoryPriority(priority)
        mode = normalizeMemoryMode(mode)
        retrievalHint, hintReason = normalizeRetrievalHint(retrievalHint)
        tagsJson = json.dumps(_normalizeTags(tags), ensure_ascii=False)
        content = str(content).strip()
        if not content:
            raise ValueError("content 不能为空")

        # content 是用户隐私正文，写入前加密为密文 bytes（存入 BLOB 列）。
        encryptedContent = encryptText(content)
        encryptedHint = encryptText(retrievalHint) if retrievalHint else None

        def _query(conn):
            _requireMemorySchema(conn)
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO memory_entries (
                    scope_type, scope_id, content, tags_json, enabled, priority,
                    source, mode, retrieval_hint
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scopeType, scopeID, encryptedContent, tagsJson, int(enabled),
                    priority, source, mode, encryptedHint,
                )
            )
            return cursor.lastrowid

        memoryID = await memoryDB.run(_query)
        if hintReason:
            await logSystemEvent(
                "LLM memory 检索说明已弃用",
                f"action=add, reason={hintReason}",
                LogLevel.WARNING,
            )
        if memoryID is not None:
            await _notifyMemoryChanged(memoryID)
        return memoryID

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


async def getMemoryCounts() -> dict[str, int]:
    """只读取记忆条目计数，不解密正文或检索说明。"""
    try:
        def _query(conn):
            row = conn.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN enabled = 1 THEN 1 ELSE 0 END) AS enabled "
                "FROM memory_entries"
            ).fetchone()
            return {
                "total": int(row["total"] or 0),
                "enabled": int(row["enabled"] or 0),
            }

        return await memoryDB.run(_query)
    except Exception as exc:
        await logSystemEvent(
            "LLM memory 计数读取失败",
            type(exc).__name__,
            LogLevel.ERROR,
            exception=exc,
        )
        return {"total": 0, "enabled": 0}


async def updateMemory(
    memoryID: int,
    *,
    content: Optional[str] = None,
    tags: Optional[list[str]] = None,
    priority: Optional[int] = None,
    enabled: Optional[bool] = None,
    source: Optional[str] = None,
    mode: Optional[str] = None,
    retrievalHint: Optional[str] = None,
    guard: Optional[MemoryWriteGuard] = None,
) -> bool:
    """按字段补丁更新 memory，并在同一事务内执行可选写入 guard。

    各字段语义：
        None 表示不修改；非 None 表示替换为新值。
    特别地，使用 tags=[] 表示显式清空所有标签（与 None=保留原标签 区分）。
    ``retrievalHint=""`` 表示显式清空；正文或 tags 改变但未给新 hint 时，
    旧 hint 会自动失效。只有真实发生更新并提交成功后才通知 runtime。
    """
    try:
        normalizedMode = normalizeMemoryMode(mode) if mode is not None else None
        normalizedPriority = (
            normalizeMemoryPriority(priority) if priority is not None else None
        )
        normalizedHint, hintReason = normalizeRetrievalHint(retrievalHint)

        def _query(conn):
            _requireMemorySchema(conn)
            if guard is not None and not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")

            cursor = conn.cursor()
            cursor.execute("SELECT * FROM memory_entries WHERE id = ?", (memoryID,))
            row = cursor.fetchone()
            if row is None:
                return False, False

            oldMemory = _rowToMemoryDict(row)
            if guard is not None:
                _checkWriteGuard(
                    oldMemory,
                    guard,
                    requestedMode=normalizedMode,
                )

            updates = []
            params = []
            contentChanged = False
            tagsChanged = False

            if content is not None:
                normalizedContent = str(content).strip()
                if not normalizedContent:
                    raise ValueError("content 不能为空")
                if normalizedContent != oldMemory.get("content"):
                    updates.append("content = ?")
                    params.append(encryptText(normalizedContent))
                    contentChanged = True

            if tags is not None:
                normalizedTags = _normalizeTags(tags)
                if normalizedTags != oldMemory.get("tags"):
                    updates.append("tags_json = ?")
                    params.append(json.dumps(normalizedTags, ensure_ascii=False))
                    tagsChanged = True

            if normalizedPriority is not None and normalizedPriority != oldMemory.get("priority"):
                updates.append("priority = ?")
                params.append(normalizedPriority)

            if enabled is not None and bool(enabled) != oldMemory.get("enabled"):
                updates.append("enabled = ?")
                params.append(int(enabled))

            if source is not None:
                normalizedSource = _normalizeSource(source)
                if normalizedSource != oldMemory.get("source"):
                    updates.append("source = ?")
                    params.append(normalizedSource)

            if normalizedMode is not None and normalizedMode != oldMemory.get("mode"):
                updates.append("mode = ?")
                params.append(normalizedMode)

            oldHint = oldMemory.get("retrievalHint")
            # hint 是正文/标签的语义缓存辅助字段：显式传空字符串表示清除，
            # 省略表示保留；正文或标签变化时旧 hint 失效，防止过时提示继续导流。
            hintWasExplicitlyCleared = (
                isinstance(retrievalHint, str) and not retrievalHint.strip()
            )
            if hintWasExplicitlyCleared:
                nextHint = None
            elif retrievalHint is not None and hintReason is None:
                nextHint = normalizedHint
            elif contentChanged or tagsChanged:
                nextHint = None
            else:
                nextHint = oldHint

            if nextHint != oldHint:
                updates.append("retrieval_hint = ?")
                params.append(encryptText(nextHint) if nextHint else None)

            if not updates:
                return True, False

            updates.append("updated_at = CURRENT_TIMESTAMP")
            params.append(memoryID)
            cursor.execute(
                f"UPDATE memory_entries SET {', '.join(updates)} WHERE id = ?",
                tuple(params)
            )
            return cursor.rowcount > 0, cursor.rowcount > 0

        success, changed = await memoryDB.run(_query)
        if hintReason:
            await logSystemEvent(
                "LLM memory 检索说明已弃用",
                f"action=update, id={memoryID}, reason={hintReason}",
                LogLevel.WARNING,
            )
        if success and changed:
            await _notifyMemoryChanged(memoryID)
        return success

    except Exception as e:
        await logSystemEvent(
            "LLM memory 更新失败",
            f"ID {memoryID}: {e}",
            LogLevel.ERROR,
            exception=e,
        )
        return False


async def deleteMemory(
    memoryID: int,
    *,
    guard: Optional[MemoryWriteGuard] = None,
) -> bool:
    """删除 memory；提供 guard 时在同一事务内先校验目标快照。"""
    try:
        def _query(conn):
            _requireMemorySchema(conn)
            if guard is not None and not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")

            cursor = conn.cursor()
            if guard is not None:
                cursor.execute("SELECT * FROM memory_entries WHERE id = ?", (memoryID,))
                row = cursor.fetchone()
                if row is None:
                    return False
                _checkWriteGuard(_rowToMemoryDict(row), guard)
            cursor.execute("DELETE FROM memory_entries WHERE id = ?", (memoryID,))
            return cursor.rowcount > 0

        success = await memoryDB.run(_query)
        if success:
            await _notifyMemoryChanged(memoryID)
        return success

    except Exception as e:
        await logSystemEvent(
            "LLM memory 删除失败",
            f"ID {memoryID}: {e}",
            LogLevel.ERROR,
            exception=e,
        )
        return False




async def getMemoryCandidates(
    *,
    chatID: str | int | None = None,
    userID: str | int | None = None,
    sessionID: str | int | None = None,
) -> list[dict[str, Any]]:
    """读取当前 scope 下全部 enabled 记忆，不按 priority 或数量截断。

    这是 hybrid 的宽候选入口；priority 只在 legacy 兼容路径排序，不能在
    语义/词面评分之前把低 priority 记忆永久挤出候选池。
    """
    scopes = [(MEMORY_SCOPE_GLOBAL, "global")]
    for scopeType, scopeID in (
        (MEMORY_SCOPE_CHAT, chatID),
        (MEMORY_SCOPE_USER, userID),
        (MEMORY_SCOPE_SESSION, sessionID),
    ):
        if scopeID is not None:
            scopes.append((scopeType, str(scopeID)))

    try:
        def _query(conn):
            clauses = ["(scope_type = ? AND scope_id = ?)"] * len(scopes)
            params = [value for scope in scopes for value in scope]
            cursor = conn.execute(
                "SELECT * FROM memory_entries WHERE enabled = 1 AND ("
                + " OR ".join(clauses)
                + ") ORDER BY id ASC",
                tuple(params),
            )

            memories = []
            failedIDs = []
            while rows := cursor.fetchmany(128):
                for row in rows:
                    try:
                        memories.append(_rowToMemoryDict(row))
                    except Exception:
                        failedIDs.append(row["id"])
            return memories, failedIDs

        memories, failedIDs = await memoryDB.run(_query)
        if failedIDs:
            await logSystemEvent(
                "LLM memory 候选行读取失败",
                f"count={len(failedIDs)}, ids={','.join(map(str, failedIDs[:20]))}",
                LogLevel.WARNING,
            )
        return memories
    except Exception as exc:
        await logSystemEvent(
            "LLM memory 候选读取失败",
            type(exc).__name__,
            LogLevel.ERROR,
            exception=exc,
        )
        return []


async def getMemorySnapshots(memoryIDs) -> list[dict[str, Any]]:
    """批量读取当前记录，供检索结果注入前复核。"""
    normalizedIDs = sorted({int(memoryID) for memoryID in memoryIDs})
    if not normalizedIDs:
        return []

    try:
        def _query(conn):
            placeholders = ",".join("?" for _ in normalizedIDs)
            cursor = conn.execute(
                f"SELECT * FROM memory_entries WHERE id IN ({placeholders})",
                tuple(normalizedIDs),
            )
            return [_rowToMemoryDict(row) for row in cursor.fetchall()]

        return await memoryDB.run(_query)
    except Exception as exc:
        await logSystemEvent(
            "LLM memory 快照读取失败",
            f"count={len(normalizedIDs)}, error={type(exc).__name__}",
            LogLevel.ERROR,
            exception=exc,
        )
        return []


async def getEnabledMemoryPage(
    afterID: int = 0,
    pageSize: int = 128,
) -> list[dict[str, Any]]:
    """按 ID 分页读取全库 enabled 记忆，供后台索引对账。"""
    pageSize = max(1, min(int(pageSize), 1000))
    try:
        def _query(conn):
            cursor = conn.execute(
                "SELECT * FROM memory_entries "
                "WHERE enabled = 1 AND id > ? ORDER BY id ASC LIMIT ?",
                (int(afterID), pageSize),
            )
            return [_rowToMemoryDict(row) for row in cursor.fetchall()]

        return await memoryDB.run(_query)
    except Exception as exc:
        await logSystemEvent(
            "LLM memory 索引分页读取失败",
            f"afterID={afterID}, error={type(exc).__name__}",
            LogLevel.ERROR,
            exception=exc,
        )
        return []


async def retrieveMemories(
    chatID: str | int | None = None,
    userID: str | int | None = None,
    sessionID: str | int | None = None,
    perScopeLimit: int = 20,   # 单 scope 候选上限（防止过多地召回某 scope），非硬性名额
    totalLimit: int = 10,
) -> list[dict[str, Any]]:
    """legacy 路径：按 scope 上限汇集，再按 priority 排序取 totalLimit。

    hybrid 不调用这个截断入口，避免旧的硬候选池重新成为语义检索的瓶颈。
    """
    try:
        scopes = [(MEMORY_SCOPE_GLOBAL, "global")]
        if chatID is not None:
            scopes.append((MEMORY_SCOPE_CHAT, str(chatID)))
        if userID is not None:
            scopes.append((MEMORY_SCOPE_USER, str(userID)))
        if sessionID is not None:
            scopes.append((MEMORY_SCOPE_SESSION, str(sessionID)))

        pool: list[dict[str, Any]] = []
        for scopeType, scopeID in scopes:
            items = await getMemories(
                scopeType=scopeType,
                scopeID=scopeID,
                enabledOnly=True,
                limit=max(perScopeLimit, 0),
            )
            pool.extend(items)

        return selectLegacyMemoryCandidates(pool, totalLimit=totalLimit)

    except Exception as e:
        await logSystemEvent(
            "LLM memory 检索失败",
            str(e),
            LogLevel.ERROR,
            exception=e,
        )
        return []


def selectLegacyMemoryCandidates(
    memories: list[dict[str, Any]],
    *,
    totalLimit: int = 10,
) -> list[dict[str, Any]]:
    """按 legacy 的稳定排序选取已形成的候选池。

    per-scope 截断由 ``getMemories`` 完成；这个纯函数只负责汇池后的
    排序和总量截断，因此离线评测可以复用线上 legacy 选择规则，而不
    需要连接数据库或复制排序键。
    """
    try:
        totalLimit = max(int(totalLimit), 0)
    except (TypeError, ValueError):
        totalLimit = 0

    # 排序键（全部 DESC）：priority > scope 专属度 > updated_at > id。
    # 每个字段都带默认，脏数据不能把整次检索放大成异常。
    ordered = sorted(
        memories,
        key=lambda memory: (
            memory.get("priority", 0),
            MEMORY_SCOPE_RANK.get(memory.get("scope_type"), 0),
            memory.get("updated_at") or datetime.min,
            memory.get("id", 0),
        ),
        reverse=True,
    )
    return ordered[:totalLimit]




def buildMemoryContextBlock(memories: list[dict[str, Any]]) -> str:
    """将检索到的 memories 格式化为上下文块"""
    if not memories:
        return ""

    # 块头明确相关性门控 + priority 语义纠偏，切断"高 priority = 必须提及"的误读。
    lines = [
        "[以下是长期记忆。仅在与当前对话直接相关时才引用；"
        "不相关的条目请忽略，不要为了提及而提及。"
        "w= 是内部召回权重，仅供你判断是否调整记忆（调整时用 update 的 priority 字段），"
        "不代表与当前对话的相关性，不要因 w 高就强行提及。]"
    ]
    for item in memories:
        scopeType = item.get("scope_type", "?")
        scopeID = item.get("scope_id", "?")
        priority = item.get("priority", 0)
        # content / tags 是不可信叶子（inferred 记忆可能源自用户对话），
        # 进 [...]/<...> 结构标记前就中和分隔符，防止伪造高信任块越权。
        content = neutralizePromptDelimiters(item.get("content", ""))
        tags = [neutralizePromptDelimiters(t) for t in (item.get("tags") or [])]

        source = item.get("source", "manual")
        # w= 是语义中性的内部权重（避免"优先级/重要性"暗示 LLM 必须提及）；
        # id/src 保留，LLM 识别可操作 inferred 记忆所必需。
        header = f"- ({scopeType}:{scopeID}, w={priority}, id={item['id']}, src={source}) {content}"
        if tags:
            header += f" [tags: {', '.join(tags)}]"
        lines.append(header)

    return "\n".join(lines)




def summarizeRetrievedMemories(memories: list[dict[str, Any]]) -> str:
    """生成检索摘要，用于日志和可观测性"""
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
