"""用临时数据库和临时密钥验证工作上下文的事务、恢复与故障边界。"""

import asyncio
import json
import sqlite3
import threading
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from cryptography.fernet import Fernet

from utils.core import crypto
from utils.core.database import Database
from utils.llm.context import store as storeModule
from utils.llm.context.store import ContextStore, ContextStorageError




@pytest.fixture
async def storeEnv(tmp_path, monkeypatch):
    monkeypatch.setattr(crypto, "_fernetCache", Fernet(Fernet.generate_key()))
    monkeypatch.setattr("utils.core.database.getResourceManager", lambda: MagicMock())
    monkeypatch.setattr(storeModule, "logSystemEvent", AsyncMock())
    path = tmp_path / "contexts.db"
    db = Database(str(path), "ContextStoreTest")
    store = ContextStore(db)
    await store.initialize()
    return store, db, path


async def newTask(store, *, shared=False):
    contextID = await store.createContext("-100", shared=shared)
    taskID = str(uuid4())
    await store.createTask(contextID, taskID)
    return contextID, taskID


async def queryRows(db, sql, args=()):
    return await db.run(lambda conn: [dict(row) for row in conn.execute(sql, args)])




async def test_EncryptedRoundTripPreservesOperationAndMetadata(storeEnv):
    store, db, path = storeEnv
    contextID, taskID = await newTask(store)
    UUID(contextID)
    await store.beginOperation(contextID, taskID, 1, "说话", {"text": "保密正文向量甲乙丙"})
    before = await store.readRecords(contextID)
    assert before[0]["status"] == "executing"
    await store.finishOperation(contextID, taskID, 1, "done", {"modelText": "成功结果私密丁戊己"})
    await store.finishTask(taskID, "completed", 2)
    records = await store.readRecords(contextID)
    assert records[0] | {"createdAt": 0} == {
        "contextID": contextID, "taskID": taskID, "operationIndex": 1,
        "kind": "operation", "status": "done", "position": 1, "createdAt": 0,
        "content": {
            "name": "说话", "args": {"text": "保密正文向量甲乙丙"},
            "result": {"modelText": "成功结果私密丁戊己"},
        },
    }
    task = (await queryRows(db, "SELECT * FROM tasks"))[0]
    assert task["modelCalls"] == 2
    assert task["status"] == "completed"
    raw = (await queryRows(db, "SELECT content FROM records"))[0]["content"]
    assert isinstance(raw, bytes)
    for source in path.parent.glob("contexts.db*"):
        disk = source.read_bytes()
        assert "保密正文向量甲乙丙".encode() not in disk
        assert "成功结果私密丁戊己".encode() not in disk


async def test_SharedContextBindingAcrossInstancesIsAtomic(storeEnv):
    store, db, path = storeEnv
    other = ContextStore(Database(str(path), "OtherContextStore"))
    await other.initialize()
    ids = await asyncio.gather(*[
        (store if index % 2 else other).createContext("chat", shared=True)
        for index in range(12)
    ])
    assert len(set(ids)) == 1
    independent = await store.createContext("chat")
    assert independent != ids[0]
    assert await store.createContext("chat", shared=True) == ids[0]
    assert len(await queryRows(db, "SELECT * FROM currentContexts")) == 1


async def test_ConcurrentWritersAllocateDistinctPositions(storeEnv):
    store, db, path = storeEnv
    other = ContextStore(Database(str(path), "ConcurrentContextStore"))
    await other.initialize()
    contextID, taskID = await newTask(store)
    await asyncio.gather(*[
        (store if index % 2 else other).beginOperation(contextID, taskID, index, "op", {"index": index})
        for index in range(1, 13)
    ])
    records = await store.readRecords(contextID)
    assert [row["position"] for row in records] == list(range(1, 13))
    assert {row["operationIndex"] for row in records} == set(range(1, 13))
    assert len(await queryRows(db, "SELECT * FROM records")) == 12


async def test_RestartRecoversOnceAndPreservesCompletedStates(storeEnv):
    store, db, path = storeEnv
    contextID, taskID = await newTask(store)
    for index, status in enumerate(["done", "submitted", "failed", "skipped"], 1):
        await store.beginOperation(contextID, taskID, index, "op", {})
        await store.finishOperation(contextID, taskID, index, status, {})
    await store.finishTask(taskID, "completed", 3)
    otherTaskID = str(uuid4())
    await store.createTask(contextID, otherTaskID)
    await store.beginOperation(contextID, otherTaskID, 1, "pending", {})
    restarted = ContextStore(Database(str(path), "RestartedContextStore"))
    await restarted.initialize()
    assert [row["status"] for row in await restarted.readRecords(contextID)] == [
        "done", "expired", "failed", "skipped", "uncertain",
    ]
    tasks = await queryRows(db, "SELECT status FROM tasks ORDER BY rowid")
    assert [row["status"] for row in tasks] == ["completed", "interrupted"]
    nextTask = str(uuid4())
    await restarted.createTask(contextID, nextTask)
    await restarted.beginOperation(contextID, nextTask, 1, "new", {})
    await restarted.initialize()
    assert (await restarted.readRecords(contextID))[-1]["status"] == "executing"


async def test_InterruptedTaskPreservesSubmittedAndMarksExecuting(storeEnv):
    store, db, _ = storeEnv
    contextID, taskID = await newTask(store)
    await store.beginOperation(contextID, taskID, 1, "send", {})
    await store.finishOperation(contextID, taskID, 1, "submitted", {"reviewID": 3})
    await store.beginOperation(contextID, taskID, 2, "tool", {})
    await store.beginOperation(contextID, taskID, 3, "tool", {}, status="skipped")
    await store.finishTask(taskID, "interrupted", 1)
    assert [row["status"] for row in await store.readRecords(contextID)] == [
        "submitted", "uncertain", "skipped",
    ]
    assert (await queryRows(db, "SELECT status FROM tasks"))[0]["status"] == "interrupted"


async def test_ForkSnapshotSurvivesParentChangesAndDeletion(storeEnv):
    store, db, _ = storeEnv
    contextID, taskID = await newTask(store, shared=True)
    await store.beginOperation(contextID, taskID, 1, "first", {"x": 1})
    await store.finishOperation(contextID, taskID, 1, "done", {"answer": 2})
    await store.beginOperation(contextID, taskID, 2, "second", {})
    forkID = await store.forkContext(contextID, 1)
    childRecords = await store.readRecords(forkID)
    assert len(childRecords) == 1
    assert childRecords[0]["taskID"] == taskID
    assert childRecords[0]["content"] == (await store.readRecords(contextID))[0]["content"]
    assert childRecords[0]["contextID"] == forkID
    await store.finishOperation(contextID, taskID, 2, "done", {})
    await store.finishTask(taskID, "completed", 1)
    await store.deleteContext(contextID)
    assert await store.readRecords(forkID) == childRecords
    assert await queryRows(db, "SELECT * FROM currentContexts") == []
    assert await queryRows(db, "SELECT * FROM tasks") == []
    childTask = str(uuid4())
    await store.createTask(forkID, childTask)
    await store.beginOperation(forkID, childTask, 1, "child", {})
    assert [row["position"] for row in await store.readRecords(forkID)] == [1, 2]


async def test_ForkExecutingSnapshotRemainsIndependent(storeEnv):
    store, _, _ = storeEnv
    contextID, taskID = await newTask(store)
    await store.beginOperation(contextID, taskID, 1, "tool", {})
    childID = await store.forkContext(contextID, 1)
    await store.finishTask(taskID, "interrupted", 0)
    assert (await store.readRecords(childID))[0]["status"] == "executing"
    await store.deleteContext(contextID)
    assert (await store.readRecords(childID))[0]["status"] == "executing"


@pytest.mark.parametrize("position", [-1, 1, 100, True, "0"])
async def test_ForkRejectsInvalidPositionWithoutPausing(storeEnv, position):
    store, _, _ = storeEnv
    contextID = await store.createContext("chat")
    with pytest.raises(ContextStorageError):
        await store.forkContext(contextID, position)
    store.requireWritable()


async def test_ForkZeroCreatesEmptyIndependentContext(storeEnv):
    store, _, _ = storeEnv
    contextID = await store.createContext("chat", shared=True)
    childID = await store.forkContext(contextID, 0)
    assert await store.readRecords(childID) == []
    assert await store.createContext("chat", shared=True) == contextID


async def test_DeleteRunningTaskIsRejectedWithoutPausing(storeEnv):
    store, _, _ = storeEnv
    contextID, taskID = await newTask(store)
    with pytest.raises(ContextStorageError):
        await store.deleteContext(contextID)
    store.requireWritable()
    await store.finishTask(taskID, "failed", 0)
    await store.deleteContext(contextID)
    with pytest.raises(ContextStorageError):
        await store.readRecords(contextID)


async def test_DuplicateWritesAndWrongIdentityLeaveOriginalRecords(storeEnv):
    store, _, _ = storeEnv
    contextID, taskID = await newTask(store)
    otherID = await store.createContext("other")
    with pytest.raises(ContextStorageError):
        await store.createTask(contextID, taskID)
    with pytest.raises(ContextStorageError):
        await store.beginOperation(otherID, taskID, 1, "wrong", {})
    await store.beginOperation(contextID, taskID, 1, "correct", {})
    with pytest.raises(ContextStorageError):
        await store.beginOperation(contextID, taskID, 1, "duplicate", {})
    with pytest.raises(ContextStorageError):
        await store.finishOperation(otherID, taskID, 1, "done", {})
    await store.finishOperation(contextID, taskID, 1, "done", {"answer": 1})
    with pytest.raises(ContextStorageError):
        await store.finishOperation(contextID, taskID, 1, "failed", {"answer": 2})
    await store.finishTask(taskID, "completed", 1)
    with pytest.raises(ContextStorageError):
        await store.finishTask(taskID, "failed", 9)
    with pytest.raises(ContextStorageError):
        await store.beginOperation(contextID, taskID, 2, "late", {})
    store.requireWritable()
    assert (await store.readRecords(contextID))[0]["content"]["result"] == {"answer": 1}


@pytest.mark.parametrize("badContent", [b"broken token", "wrong type", None])
async def test_CorruptCiphertextRaisesExplicitError(storeEnv, badContent):
    store, db, _ = storeEnv
    contextID, taskID = await newTask(store)
    await store.beginOperation(contextID, taskID, 1, "tool", {})
    if badContent is None:
        badContent = crypto.encryptText(json.dumps(["not an object"]))
    await db.run(lambda conn: conn.execute("UPDATE records SET content=?", (badContent,)))
    with pytest.raises(ContextStorageError):
        await store.readRecords(contextID)
    with pytest.raises(ContextStorageError):
        await store.forkContext(contextID, 1)
    assert len(await queryRows(db, "SELECT * FROM contexts")) == 1


async def test_MissingContextWithOrphanRecordsRaises(storeEnv):
    store, db, _ = storeEnv
    contextID, taskID = await newTask(store)
    await store.beginOperation(contextID, taskID, 1, "tool", {})
    # 模拟历史损坏库：直接连接默认关闭外键，留下无所属上下文的记录。
    await db.run(lambda conn: conn.execute("DELETE FROM contexts WHERE contextID=?", (contextID,)))
    with pytest.raises(ContextStorageError):
        await store.readRecords(contextID)
    assert await queryRows(db, "SELECT * FROM records")


async def test_WriteFailurePausesNewWorkButAcceptsInflightResults(storeEnv, monkeypatch):
    store, db, _ = storeEnv
    contextID, taskID = await newTask(store)
    await store.beginOperation(contextID, taskID, 1, "tool", {})
    originalRun = db.run

    async def failRun(callback):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(db, "run", failRun)
    with pytest.raises(ContextStorageError):
        await store.beginOperation(contextID, taskID, 2, "tool", {})
    with pytest.raises(ContextStorageError):
        store.requireWritable()
    with pytest.raises(ContextStorageError):
        await store.checkWritable()
    monkeypatch.setattr(db, "run", originalRun)
    with pytest.raises(ContextStorageError):
        await store.createTask(contextID, str(uuid4()))
    with pytest.raises(ContextStorageError):
        await store.beginOperation(contextID, taskID, 2, "tool", {})
    await store.finishOperation(contextID, taskID, 1, "done", {"realResult": "done"})
    await store.finishTask(taskID, "completed", 1)
    with pytest.raises(ContextStorageError):
        store.requireWritable()
    await store.checkWritable()
    store.requireWritable()
    assert (await queryRows(db, "SELECT probe FROM storageHealth"))[0]["probe"] == 1
    assert (await store.readRecords(contextID))[0]["status"] == "done"


async def test_FailedResultWriteRemainsExecutingAfterHealthProbe(storeEnv, monkeypatch):
    store, db, _ = storeEnv
    contextID, taskID = await newTask(store)
    await store.beginOperation(contextID, taskID, 1, "tool", {})
    originalRun = db.run
    monkeypatch.setattr(db, "run", AsyncMock(side_effect=sqlite3.OperationalError("failure")))
    with pytest.raises(ContextStorageError):
        await store.finishOperation(contextID, taskID, 1, "done", {"sent": True})
    monkeypatch.setattr(db, "run", originalRun)
    await store.checkWritable()
    assert (await store.readRecords(contextID))[0]["status"] == "executing"


async def test_TransactionRollsBackPartialMutation(storeEnv, monkeypatch):
    store, db, _ = storeEnv
    contextID, taskID = await newTask(store)

    def failTouch(conn, contextID):
        raise sqlite3.OperationalError("transaction fault")

    monkeypatch.setattr(store, "_touch", failTouch)
    with pytest.raises(ContextStorageError):
        await store.beginOperation(contextID, taskID, 1, "tool", {})
    assert await queryRows(db, "SELECT * FROM records") == []


@pytest.mark.parametrize("failAfterRelease", [False, True])
async def test_CancelledWriteWaitsForThreadBeforeReleasingLock(storeEnv, monkeypatch, failAfterRelease):
    store, db, _ = storeEnv
    contextID, taskID = await newTask(store)
    started = asyncio.Event()
    release = threading.Event()
    eventLoop = asyncio.get_running_loop()
    originalRun = db.run

    async def delayedRun(callback):
        def delayed(conn):
            eventLoop.call_soon_threadsafe(started.set)
            if not release.wait(5):
                raise RuntimeError("test release timed out")
            result = callback(conn)
            if failAfterRelease:
                raise sqlite3.OperationalError("late failure")
            return result

        return await originalRun(delayed)

    monkeypatch.setattr(db, "run", delayedRun)
    pending = asyncio.create_task(store.beginOperation(contextID, taskID, 1, "tool", {}))
    try:
        await asyncio.wait_for(started.wait(), 2)
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done()
        assert store._lock.locked()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(pending, 2)
    assert not store._lock.locked()
    monkeypatch.setattr(db, "run", originalRun)
    records = await store.readRecords(contextID)
    if failAfterRelease:
        assert records == []
        with pytest.raises(ContextStorageError):
            store.requireWritable()
    else:
        assert records[0]["status"] == "executing"
        store.requireWritable()


async def test_InitializeFailureIsExplicitAndRetryRecovers(storeEnv, monkeypatch):
    _, db, _ = storeEnv
    fresh = ContextStore(db)
    originalRun = db.run
    monkeypatch.setattr(db, "run", AsyncMock(side_effect=sqlite3.OperationalError("unavailable")))
    with pytest.raises(ContextStorageError):
        await fresh.initialize()
    with pytest.raises(ContextStorageError):
        fresh.requireWritable()
    with pytest.raises(ContextStorageError):
        await fresh.createContext("chat")
    monkeypatch.setattr(db, "run", originalRun)
    await fresh.initialize()
    fresh.requireWritable()


@pytest.mark.parametrize("status", ["unknown", "done", "running"])
async def test_InvalidTaskStatusIsRejectedWithoutPausing(storeEnv, status):
    store, _, _ = storeEnv
    _, taskID = await newTask(store)
    with pytest.raises(ContextStorageError):
        await store.finishTask(taskID, status, 0)
    store.requireWritable()
