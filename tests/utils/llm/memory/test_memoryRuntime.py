"""测试 memory runtime 的有界队列、缓存、一致性与关闭语义。"""

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from utils.llm.memory import runtime as runtimeModule
from utils.llm.memory.runtime import MemoryRuntime




class _FakeSimilarities:
    def __init__(self, score):
        self._score = score

    def max(self):
        return self._score


class _FakeMatrix:
    def __init__(self, sizeBytes=4, score=0.9):
        self.nbytes = sizeBytes
        self._score = score

    def __matmul__(self, vector):
        return _FakeSimilarities(self._score)


class _FakeEncoder:
    def __init__(self, *, matrixSize=4):
        self.matrixSize = matrixSize
        self.encodedMemoryIDs = []
        self.queryCalls = []
        self.closed = False

    def encodeMemory(self, memory):
        self.encodedMemoryIDs.append(memory["id"])
        return _FakeMatrix(self.matrixSize)

    def encodeQueries(self, queryTexts):
        self.queryCalls.append(tuple(queryTexts))
        return [object() for _ in queryTexts]

    def close(self):
        self.closed = True


class _BlockingQueryEncoder(_FakeEncoder):
    def __init__(self, started, release):
        super().__init__()
        self._started = started
        self._release = release

    def encodeQueries(self, queryTexts):
        self._started.set()
        self._release.wait(timeout=2)
        return super().encodeQueries(queryTexts)


def _memory(memoryID, *, content=None, enabled=True):
    return {
        "id": memoryID,
        "scope_type": "global",
        "scope_id": "global",
        "content": content or f"事实 {memoryID}",
        "tags": [],
        "retrievalHint": None,
        "enabled": enabled,
        "priority": 0,
        "source": "inferred",
        "mode": "contextual",
    }


async def _close(runtime):
    if runtime.getStatus()["running"] or runtime.getStatus()["closing"]:
        await runtime.close()




def test_same_id_coalesces_and_online_notification_upgrades_reconcile_job():
    runtime = MemoryRuntime(encoderFactory=_FakeEncoder)
    try:
        assert runtime._enqueueIndex(7, allowEviction=False) is True
        queuedAt = runtime._pendingIndex[7].queuedAt

        runtime.notifyMemoryChanged(7)
        runtime.notifyMemoryChanged(7)

        assert list(runtime._pendingIndex) == [7]
        assert runtime._pendingIndex[7].queuedAt == queuedAt
        assert runtime._pendingIndex[7].allowEviction is True
    finally:
        runtime._executor.shutdown(wait=True, cancel_futures=True)


def test_runtimeConstructionDoesNotReadOptionalModelManifest():
    with patch(
        "utils.llm.memory.runtime.loadModelManifest",
        side_effect=RuntimeError("broken manifest"),
    ) as mockManifest:
        runtime = MemoryRuntime(encoderFactory=_FakeEncoder)

    try:
        mockManifest.assert_not_called()
        assert runtime.getStatus()["encoderReady"] is False
    finally:
        runtime._executor.shutdown(wait=True, cancel_futures=True)


@pytest.mark.asyncio
async def test_invalidManifestDegradesWhenEncoderIsActuallyRequested():
    runtime = MemoryRuntime(encoderFactory=_FakeEncoder)
    with (
        patch(
            "utils.llm.memory.runtime.loadModelManifest",
            side_effect=RuntimeError("broken manifest"),
        ),
        patch(
            "utils.llm.memory.runtime.logSystemEvent",
            new_callable=AsyncMock,
        ),
    ):
        assert await runtime._ensureEncoder() is False

    assert runtime.getStatus()["lastReason"] == "RuntimeError"
    await runtime.close()


def test_index_queue_limit_is_bounded(monkeypatch):
    monkeypatch.setattr(runtimeModule, "LLM_MEMORY_INDEX_QUEUE_LIMIT", 1)
    runtime = MemoryRuntime(encoderFactory=_FakeEncoder)
    try:
        runtime.notifyMemoryChanged(1)
        runtime.notifyMemoryChanged(2)

        assert list(runtime._pendingIndex) == [1]
        assert runtime.getStatus()["indexDropped"] == 1
        assert runtime.getStatus()["lastReason"] == "indexQueueFull"
    finally:
        runtime._executor.shutdown(wait=True, cancel_futures=True)


@pytest.mark.asyncio
async def test_query_queue_limit_rejects_without_unbounded_executor_submission(monkeypatch):
    monkeypatch.setattr(runtimeModule, "LLM_MEMORY_QUERY_QUEUE_LIMIT", 1)
    encoder = _FakeEncoder()
    runtime = MemoryRuntime(encoderFactory=lambda: encoder)
    runtime._encoder = encoder
    candidate = _memory(1)
    runtime._publish(
        1,
        runtime._fingerprint(candidate),
        _FakeMatrix(),
        allowEviction=True,
    )
    first = asyncio.create_task(runtime.scoreSemantic(
        ["first"],
        [candidate],
        deadline=time.monotonic() + 2,
    ))
    await asyncio.sleep(0)

    second = await runtime.scoreSemantic(
        ["second"],
        [candidate],
        deadline=time.monotonic() + 2,
    )

    assert second == [{}]
    assert runtime.getStatus()["queryRejected"] == 1
    assert runtime.getStatus()["queryQueued"] == 1
    await runtime.close()
    assert await first == [{}]


def test_lru_byte_limit_and_reconcile_publish_does_not_evict(monkeypatch):
    monkeypatch.setattr(runtimeModule, "LLM_MEMORY_VECTOR_CACHE_BYTES", 10)
    runtime = MemoryRuntime(encoderFactory=_FakeEncoder)
    try:
        assert runtime._publish(1, "one", _FakeMatrix(6), allowEviction=True)
        assert runtime._publish(2, "two", _FakeMatrix(6), allowEviction=True)
        assert list(runtime._cache) == [2]
        assert runtime._cacheBytes == 6

        assert not runtime._publish(
            3,
            "three",
            _FakeMatrix(5),
            allowEviction=False,
        )
        assert list(runtime._cache) == [2]
        assert runtime.getStatus()["reconcileCapacitySaturated"] is True

        assert runtime._publish(3, "three", _FakeMatrix(5), allowEviction=True)
        assert list(runtime._cache) == [3]
        assert runtime._cacheBytes == 5
        assert runtime.getStatus()["reconcileCapacitySaturated"] is False
    finally:
        runtime._executor.shutdown(wait=True, cancel_futures=True)


@pytest.mark.asyncio
async def test_stale_encode_result_is_rejected():
    encoder = _FakeEncoder()
    runtime = MemoryRuntime(encoderFactory=lambda: encoder)
    runtime._encoder = encoder
    original = _memory(1, content="旧事实")
    changed = _memory(1, content="新事实")
    runtime.notifyMemoryChanged(1)
    with patch(
        "utils.llm.memory.runtime.getMemoryByID",
        new_callable=AsyncMock,
        side_effect=[original, changed],
    ):
        await runtime._processIndex()

    assert 1 not in runtime._cache
    assert runtime.getStatus()["staleResults"] == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_delete_or_disable_invalidates_cached_matrix():
    encoder = _FakeEncoder()
    runtime = MemoryRuntime(encoderFactory=lambda: encoder)
    runtime._encoder = encoder
    memory = _memory(1)
    runtime._publish(
        1,
        runtime._fingerprint(memory),
        _FakeMatrix(),
        allowEviction=True,
    )
    runtime.notifyMemoryChanged(1)
    with patch(
        "utils.llm.memory.runtime.getMemoryByID",
        new_callable=AsyncMock,
        return_value=None,
    ):
        await runtime._processIndex()

    assert runtime.getStatus()["cacheEntries"] == 0
    await runtime.close()


@pytest.mark.asyncio
async def test_query_timeout_keeps_native_job_active_until_it_really_finishes():
    started = threading.Event()
    release = threading.Event()
    encoder = _BlockingQueryEncoder(started, release)
    runtime = MemoryRuntime(encoderFactory=lambda: encoder)
    runtime._encoder = encoder
    candidate = _memory(1)
    runtime._publish(
        1,
        runtime._fingerprint(candidate),
        _FakeMatrix(),
        allowEviction=True,
    )
    scoreTask = asyncio.create_task(runtime.scoreSemantic(
        ["query"],
        [candidate],
        deadline=time.monotonic() + 0.03,
    ))
    await asyncio.sleep(0)
    processTask = asyncio.create_task(runtime._processQuery())
    try:
        assert await asyncio.to_thread(started.wait, 1)
        assert await scoreTask == [{}]
        assert runtime.getStatus()["activeNativeJob"] is True
        assert processTask.done() is False
    finally:
        release.set()

    await processTask
    assert runtime.getStatus()["activeNativeJob"] is False
    await runtime.close()


@pytest.mark.asyncio
async def test_reconcile_recovers_missed_notification():
    encoder = _FakeEncoder()
    runtime = MemoryRuntime(encoderFactory=lambda: encoder)
    runtime._encoder = encoder
    memory = _memory(5)
    with (
        patch(
            "utils.llm.memory.runtime.getEnabledMemoryPage",
            new_callable=AsyncMock,
            side_effect=[[memory], []],
        ),
        patch(
            "utils.llm.memory.runtime.getMemoryByID",
            new_callable=AsyncMock,
            side_effect=[memory, memory],
        ),
    ):
        await runtime._reconcileStep()
        assert runtime._pendingIndex[5].allowEviction is False
        await runtime._processIndex()
        await runtime._reconcileStep()

    assert list(runtime._cache) == [5]
    assert encoder.encodedMemoryIDs == [5]
    await runtime.close()


@pytest.mark.asyncio
async def test_reconcile_capacity_saturation_stops_churn_until_online_eviction(monkeypatch):
    monkeypatch.setattr(runtimeModule, "LLM_MEMORY_VECTOR_CACHE_BYTES", 4)
    encoder = _FakeEncoder(matrixSize=4)
    runtime = MemoryRuntime(encoderFactory=lambda: encoder)
    runtime._encoder = encoder
    cached = _memory(1)
    second = _memory(2)
    third = _memory(3)
    runtime._publish(
        1,
        runtime._fingerprint(cached),
        _FakeMatrix(4),
        allowEviction=True,
    )
    memories = {2: second, 3: third}

    async def _getMemory(memoryID):
        return memories[memoryID]

    with (
        patch(
            "utils.llm.memory.runtime.getEnabledMemoryPage",
            new_callable=AsyncMock,
            return_value=[cached, second, third],
        ),
        patch(
            "utils.llm.memory.runtime.getMemoryByID",
            side_effect=_getMemory,
        ),
    ):
        await runtime._reconcileStep()
        await runtime._processIndex()
        await runtime._processIndex()

        assert encoder.encodedMemoryIDs == [2]
        assert list(runtime._cache) == [1]
        assert runtime.getStatus()["reconcileCapacitySaturated"] is True

        runtime._pendingIndex.clear()
        runtime._reconcileAfterID = 0
        await runtime._reconcileStep()
        assert runtime._pendingIndex == {}

        runtime.notifyMemoryChanged(3)
        await runtime._processIndex()

    assert encoder.encodedMemoryIDs == [2, 3]
    assert list(runtime._cache) == [3]
    assert runtime.getStatus()["reconcileCapacitySaturated"] is False
    await runtime.close()


@pytest.mark.asyncio
async def test_cancelled_native_wrapper_waits_for_underlying_thread():
    started = threading.Event()
    release = threading.Event()
    runtime = MemoryRuntime(encoderFactory=_FakeEncoder)

    def _blockingWork():
        started.set()
        release.wait(timeout=2)
        return "done"

    task = asyncio.create_task(runtime._runNative(_blockingWork))
    assert await asyncio.to_thread(started.wait, 1)
    task.cancel()
    await asyncio.sleep(0.02)

    assert task.done() is False
    assert runtime.getStatus()["activeNativeJob"] is True
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runtime.getStatus()["activeNativeJob"] is False
    await runtime.close()


@pytest.mark.asyncio
async def test_close_waits_for_native_job_then_closes_encoder_and_clears_state():
    started = threading.Event()
    release = threading.Event()
    encoder = _FakeEncoder()
    runtime = MemoryRuntime(encoderFactory=lambda: encoder)
    runtime._encoder = encoder

    def _blockingWork():
        started.set()
        release.wait(timeout=2)

    nativeTask = asyncio.create_task(runtime._runNative(_blockingWork))
    assert await asyncio.to_thread(started.wait, 1)
    state = SimpleNamespace(
        value=runtime,
        getMemoryRuntime=lambda: state.value,
        setMemoryRuntime=lambda value: setattr(state, "value", value),
    )
    with patch("utils.llm.memory.runtime.getStateManager", return_value=state):
        closeTask = asyncio.create_task(runtime.close())
        await asyncio.sleep(0.02)

        assert closeTask.done() is False
        assert encoder.closed is False
        assert state.value is runtime

        release.set()
        await nativeTask
        await closeTask

    assert encoder.closed is True
    assert state.value is None
    assert runtime.getStatus()["cacheEntries"] == 0


@pytest.mark.asyncio
async def test_encoder_factory_is_initialized_once_and_close_is_idempotent():
    encoder = _FakeEncoder()
    factory = MagicMock(return_value=encoder)
    runtime = MemoryRuntime(encoderFactory=factory)

    assert await runtime._ensureEncoder() is True
    assert await runtime._ensureEncoder() is True
    await asyncio.gather(runtime.close(), runtime.close())

    factory.assert_called_once_with()
    assert encoder.closed is True
