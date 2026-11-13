"""验证操作循环与 journal 的顺序、故障边界、取消和预写期间停止。"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from cryptography.fernet import Fernet

from utils.core import crypto
from utils.core.database import Database
from utils.llm.context.journal import ContextJournal
from utils.llm.context.store import ContextStore, ContextStorageError
from utils.llm.operation.loop import runTask
from utils.llm.operation.types import (
    Capability, OperationJournal, OperationResult, OperationStatus, TaskInfo, TaskStatus,
)




_WAIT_TIMEOUT = 5




@pytest.fixture(autouse=True)
def isolateLogs(monkeypatch):
    """所有测试日志留在内存，避免写入实际运行目录。"""
    monkeypatch.setattr("utils.llm.operation.loop.logSystemEvent", AsyncMock())
    monkeypatch.setattr("utils.llm.context.store.logSystemEvent", AsyncMock())


@pytest.fixture
async def persistedJournal(tmp_path, monkeypatch):
    """为停止竞态提供真实事务，同时隔离数据库、加密密钥和清理回调。"""
    monkeypatch.setattr(crypto, "KEY_PATH", str(tmp_path / ".chatKey"))
    monkeypatch.setattr(crypto, "_fernetCache", Fernet(Fernet.generate_key()))
    monkeypatch.setattr("utils.core.database.getResourceManager", lambda: MagicMock())
    store = ContextStore(Database(str(tmp_path / "contexts.db"), "LoopPersistenceTest"))
    await store.initialize()
    contextID = await store.createContext("-100")
    await store.createTask(contextID, "persistence-task")
    return ContextJournal(store, contextID, "persistence-task")




def _batch(*values, end=True):
    payload = {"operations": [{"op": "message", "text": value} for value in values], "end": end}
    return f"<OPERATIONS>{json.dumps(payload, ensure_ascii=False)}</OPERATIONS>"


def _task():
    return TaskInfo(taskID="persistence-task", chatID="-100", userID=42)


def _capability(handle=None):
    return Capability(
        name="message", describe=lambda task: "发送消息",
        validate=AsyncMock(return_value=None),
        handle=handle or AsyncMock(return_value=OperationResult(status=OperationStatus.DONE, modelText="已发送")),
    )


def _journal():
    return Mock(spec=OperationJournal)




@pytest.mark.parametrize("completedFirst", [False, True])
async def test_checkReadyFailureBlocksNextModelAndSideEffects(completedFirst):
    """存储暂停时阻止下一次模型调用，已成功操作不被撤销或改写。"""
    error = ContextStorageError("存储已暂停")
    journal = _journal()
    journal.checkReady.side_effect = [None, error] if completedFirst else error
    capability = _capability()
    model = AsyncMock(return_value=_batch("第一条", end=False))

    outcome = await runTask(task=_task(), capabilities=[capability], callModel=model, journal=journal)

    expected = int(completedFirst)
    assert outcome.status == TaskStatus.FAILED
    assert outcome.error is error
    assert outcome.modelCalls == expected
    assert model.await_count == expected
    assert capability.handle.await_count == expected
    assert journal.beforeOperation.await_count == expected
    assert journal.afterOperation.await_count == expected
    assert [record.result.status for record in outcome.records] == [OperationStatus.DONE] * expected


@pytest.mark.parametrize("failureIndex", [1, 2])
async def test_beforeOperationFailureBlocksHandlerAndRemainingBatch(failureIndex):
    """预写失败的操作不派发处理器，前序成功结果保持 DONE。"""
    error = ContextStorageError("提交操作失败")
    journal = _journal()
    journal.beforeOperation.side_effect = [None] * (failureIndex - 1) + [error]
    capability = _capability()
    model = AsyncMock(return_value=_batch("第一条", "第二条", "第三条"))

    outcome = await runTask(task=_task(), capabilities=[capability], callModel=model, journal=journal)

    assert outcome.status == TaskStatus.FAILED
    assert outcome.error is error
    assert outcome.modelCalls == 1
    model.assert_awaited_once()
    assert capability.handle.await_count == failureIndex - 1
    assert journal.beforeOperation.await_count == failureIndex
    assert journal.afterOperation.await_count == failureIndex - 1
    assert [record.operation.index for record in outcome.records] == list(range(1, failureIndex))
    assert all(record.result.status == OperationStatus.DONE for record in outcome.records)


@pytest.mark.parametrize("failureIndex", [1, 2])
async def test_afterOperationFailurePreservesDoneAndBlocksNewSideEffects(failureIndex):
    """结果保存失败属于任务故障，不能把已成功副作用改记为失败或继续派发。"""
    error = ContextStorageError("提交结果失败")
    journal = _journal()
    journal.afterOperation.side_effect = [None] * (failureIndex - 1) + [error]
    result = OperationResult(status=OperationStatus.DONE, modelText="已发送", display="发送成功")
    capability = _capability(AsyncMock(return_value=result))
    model = AsyncMock(return_value=_batch("第一条", "第二条", "第三条"))

    outcome = await runTask(task=_task(), capabilities=[capability], callModel=model, journal=journal)

    assert outcome.status == TaskStatus.FAILED
    assert outcome.error is error
    assert outcome.modelCalls == 1
    model.assert_awaited_once()
    assert capability.handle.await_count == failureIndex
    assert journal.beforeOperation.await_count == failureIndex
    assert journal.afterOperation.await_count == failureIndex
    assert len(outcome.records) == failureIndex
    assert all(record.result is result for record in outcome.records)
    assert journal.afterOperation.await_args.args[0] is outcome.records[-1]


async def test_journalCallsSurroundHandlerAndRecordSkippedOperations():
    """处理器运行前必须提交参数，失败后的同批操作仅记录 skipped。"""
    events = []
    journal = _journal()
    journal.checkReady.side_effect = lambda: events.append("ready")

    async def beforeOperation(operation):
        events.append(("before", operation.index))

    async def handle(operation, task):
        events.append(("handle", operation.index))
        return OperationResult(status=OperationStatus.FAILED, modelText="发送被拒绝")

    async def afterOperation(record):
        events.append(("after", record.operation.index, record.result.status))

    journal.beforeOperation.side_effect = beforeOperation
    journal.afterOperation.side_effect = afterOperation
    capability = _capability(AsyncMock(side_effect=handle))
    model = AsyncMock(return_value=_batch("失败", "跳过"))

    outcome = await runTask(
        task=_task(), capabilities=[capability], callModel=model, journal=journal, maxModelCalls=1,
    )

    assert outcome.status == TaskStatus.LIMIT_REACHED
    assert events == [
        "ready", ("before", 1), ("handle", 1),
        ("after", 1, OperationStatus.FAILED), ("after", 2, OperationStatus.SKIPPED),
    ]
    assert [record.result.status for record in outcome.records] == [OperationStatus.FAILED, OperationStatus.SKIPPED]


@pytest.mark.parametrize("phase", ["checkReady", "beforeOperation", "afterOperation"])
async def test_journalCancellationPropagatesAndBlocksRemainingOperations(phase):
    """journal 的取消应原样向上传播，不能吞成任务失败或启动后续操作。"""
    journal = _journal()
    cancellation = asyncio.CancelledError()
    getattr(journal, phase).side_effect = cancellation
    capability = _capability()
    model = AsyncMock(return_value=_batch("第一条", "第二条"))

    with pytest.raises(asyncio.CancelledError) as caught:
        await runTask(task=_task(), capabilities=[capability], callModel=model, journal=journal)

    assert caught.value is cancellation
    assert model.await_count == (0 if phase == "checkReady" else 1)
    assert capability.handle.await_count == (1 if phase == "afterOperation" else 0)
    assert journal.beforeOperation.await_count == (0 if phase == "checkReady" else 1)
    assert journal.afterOperation.await_count == (1 if phase == "afterOperation" else 0)
    if phase == "afterOperation":
        assert journal.afterOperation.await_args.args[0].result.status == OperationStatus.DONE


@pytest.mark.parametrize("end", [False, True])
async def test_stopDuringBeforeOperationMarksPrewrittenSkipped(persistedJournal, monkeypatch, end):
    """等待预写期间收到停止信号时，原记录落 skipped，任务停止且不派发能力。"""
    journal = persistedJournal
    task = _task()
    capability = _capability()
    model = AsyncMock(return_value=_batch("不应发送", "后续也不发送", end=end))
    written = asyncio.Event()
    release = asyncio.Event()
    beforeOperation = journal.beforeOperation

    async def holdAfterWrite(operation):
        await beforeOperation(operation)
        written.set()
        await release.wait()

    monkeypatch.setattr(journal, "beforeOperation", holdAfterWrite)
    running = asyncio.create_task(runTask(
        task=task, capabilities=[capability], callModel=model, journal=journal, maxModelCalls=1,
    ))
    try:
        await asyncio.wait_for(written.wait(), timeout=_WAIT_TIMEOUT)
        before = await journal.store.readRecords(journal.contextID)
        assert [record["status"] for record in before] == ["executing"]
        capability.handle.assert_not_awaited()
        task.stopEvent.set()
        release.set()
        outcome = await asyncio.wait_for(running, timeout=_WAIT_TIMEOUT)
    finally:
        # 断言失败时也结束后台循环，避免遗留任务影响其他用例。
        release.set()
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)

    assert outcome.status == TaskStatus.STOPPED
    assert outcome.error is None
    assert outcome.modelCalls == 1
    model.assert_awaited_once()
    capability.handle.assert_not_awaited()
    assert len(outcome.records) == 1
    assert outcome.records[0].operation.index == 1
    assert outcome.records[0].result.status == OperationStatus.SKIPPED
    saved = await journal.store.readRecords(journal.contextID)
    assert len(saved) == 1
    assert saved[0]["status"] == "skipped"
    assert saved[0]["operationIndex"] == 1
    assert saved[0]["content"]["args"] == {"text": "不应发送"}
