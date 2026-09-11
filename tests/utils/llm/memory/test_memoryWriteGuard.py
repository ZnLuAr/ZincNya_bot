"""测试模型记忆写入的事务状态 guard。"""

from unittest.mock import patch

import pytest

import utils.core.crypto as crypto
from utils.llm.memory.database import addMemory, getMemoryByID, updateMemory
from utils.llm.memory.types import MemoryWriteGuard, buildMemoryStateFingerprint




@pytest.fixture
def tmpKey(tmp_path, monkeypatch):
    monkeypatch.setattr(crypto, "KEY_PATH", str(tmp_path / ".chatKey"))
    monkeypatch.setattr(crypto, "_fernetCache", None)


@pytest.fixture
def memoryDb(inMemoryDb):
    conn = inMemoryDb
    conn.executescript("""
        CREATE TABLE memory_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope_type TEXT NOT NULL,
            scope_id TEXT NOT NULL,
            content BLOB NOT NULL,
            tags_json TEXT NOT NULL DEFAULT '[]',
            enabled INTEGER NOT NULL DEFAULT 1,
            priority INTEGER NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT 'manual',
            mode TEXT NOT NULL DEFAULT 'contextual',
            retrieval_hint BLOB,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    return conn


@pytest.fixture
def patchRun(memoryDb):
    with patch("utils.llm.memory.database.memoryDB.run") as mockRun:
        async def _run(func):
            with memoryDb:
                return func(memoryDb)
        mockRun.side_effect = _run
        yield memoryDb


def _guard(memory, *, allowPinned=False):
    return MemoryWriteGuard(
        expectedState=buildMemoryStateFingerprint(memory),
        scopeType=memory["scope_type"],
        scopeID=memory["scope_id"],
        allowPinned=allowPinned,
    )


@pytest.mark.asyncio
async def test_staleGuardCannotOverwriteChangedTarget(tmpKey, patchRun):
    memoryID = await addMemory("user", "42", "旧事实", source="inferred")
    guard = _guard(await getMemoryByID(memoryID))
    patchRun.execute(
        "UPDATE memory_entries SET priority = 2 WHERE id = ?",
        (memoryID,),
    )
    patchRun.commit()

    assert await updateMemory(memoryID, content="新事实", guard=guard) is False
    assert (await getMemoryByID(memoryID))["content"] == "旧事实"


@pytest.mark.asyncio
async def test_contextualGuardCannotWritePinnedTarget(tmpKey, patchRun):
    memoryID = await addMemory("user", "42", "事实", source="inferred")
    memory = await getMemoryByID(memoryID)
    guard = _guard(memory)

    assert await updateMemory(
        memoryID,
        mode="pinned",
        guard=guard,
    ) is False
    assert (await getMemoryByID(memoryID))["mode"] == "contextual"


@pytest.mark.asyncio
async def test_humanGuardCanPromoteUnchangedTarget(tmpKey, patchRun):
    memoryID = await addMemory("user", "42", "事实", source="inferred")
    memory = await getMemoryByID(memoryID)

    assert await updateMemory(
        memoryID,
        mode="pinned",
        guard=_guard(memory, allowPinned=True),
    ) is True
    assert (await getMemoryByID(memoryID))["mode"] == "pinned"


@pytest.mark.asyncio
async def test_guardNeverModifiesManualMemory(tmpKey, patchRun):
    memoryID = await addMemory("global", None, "人工事实", source="manual")
    memory = await getMemoryByID(memoryID)

    assert await updateMemory(
        memoryID,
        content="模型改写",
        guard=_guard(memory, allowPinned=True),
    ) is False
    assert (await getMemoryByID(memoryID))["content"] == "人工事实"
