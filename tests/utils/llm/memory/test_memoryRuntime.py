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
    def __init__(
        self,
        *,
        matrixSize=4,
        enhancedMatrixSize=None,
        baseScore=0.9,
        enhancedScore=None,
    ):
        self.matrixSize = matrixSize
        self.enhancedMatrixSize = enhancedMatrixSize
        self.baseScore = baseScore
        self.enhancedScore = enhancedScore
        self.encodedMemoryIDs = []
        self.queryCalls = []
        self.closed = False

    def encodeMemoryRepresentations(self, memory):
        self.encodedMemoryIDs.append(memory["id"])
        baseMatrix = _FakeMatrix(self.matrixSize, self.baseScore)
        if not str(memory.get("retrievalHint") or "").strip():
            enhancedMatrix = baseMatrix
        else:
            enhancedMatrix = _FakeMatrix(
                self.enhancedMatrixSize or self.matrixSize,
                (
                    self.enhancedScore
                    if self.enhancedScore is not None
                    else self.baseScore
                ),
            )
        return SimpleNamespace(base=baseMatrix, enhanced=enhancedMatrix)

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


def _memory(
    memoryID,
    *,
    content=None,
    enabled=True,
    mode="contextual",
    retrievalHint=None,
):
    return {
        "id": memoryID,
        "scope_type": "global",
        "scope_id": "global",
        "content": content or f"事实 {memoryID}",
        "tags": [],
        "retrievalHint": retrievalHint,
        "enabled": enabled,
        "priority": 0,
        "source": "inferred",
        "mode": mode,
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


def test_scoreQuerySyncReturnsAdmissionAndHintRankingScores():
    runtime = MemoryRuntime(encoderFactory=_FakeEncoder)
    runtime._encoder = _FakeEncoder()
    sharedMatrix = _FakeMatrix(score=0.7)

    try:
        results = runtime._scoreQuerySync(
            ["query"],
            (
                (1, sharedMatrix, sharedMatrix),
                (2, _FakeMatrix(score=0.4), _FakeMatrix(score=0.95)),
            ),
        )
    finally:
        runtime._executor.shutdown(wait=True, cancel_futures=True)

    assert results == [{
        "base": {1: 0.7, 2: 0.4},
        "enhanced": {1: 0.7, 2: 0.95},
    }]


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

    assert second == [{"base": {}, "enhanced": {}}]
    assert runtime.getStatus()["queryRejected"] == 1
    assert runtime.getStatus()["queryQueued"] == 1
    await runtime.close()
    assert await first == [{"base": {}, "enhanced": {}}]


@pytest.mark.asyncio
async def test_query_discovered_cache_miss_does_not_evict_hot_entry(monkeypatch):
    monkeypatch.setattr(runtimeModule, "LLM_MEMORY_VECTOR_CACHE_BYTES", 4)
    encoder = _FakeEncoder(matrixSize=4)
    runtime = MemoryRuntime(encoderFactory=lambda: encoder)
    runtime._encoder = encoder
    cached = _memory(1)
    missing = _memory(2)
    runtime._publish(
        1,
        runtime._fingerprint(cached),
        _FakeMatrix(4),
        allowEviction=True,
    )

    with patch(
        "utils.llm.memory.runtime.getMemoryByID",
        new_callable=AsyncMock,
        side_effect=[missing, missing],
    ):
        result = await runtime.scoreSemantic(
            ["query"],
            [missing],
            deadline=time.monotonic() + 2,
        )
        assert result == [{"base": {}, "enhanced": {}}]
        assert runtime._pendingIndex[2].allowEviction is False
        await runtime._processIndex()

    assert encoder.encodedMemoryIDs == [2]
    assert list(runtime._cache) == [1]
    assert runtime.getStatus()["reconcileCapacitySaturated"] is True

    await runtime.scoreSemantic(
        ["query"],
        [missing],
        deadline=time.monotonic() + 2,
    )
    assert runtime._pendingIndex == {}
    await runtime.close()


def test_semantic_cache_status_distinguishes_warm_stale_and_cold_candidates():
    """检索诊断能解释缓存缺席，而不把冷缓存误报成质量命中。"""
    runtime = MemoryRuntime(encoderFactory=_FakeEncoder)
    try:
        warm = _memory(1, content="原始事实")
        stale = _memory(1, content="更新后的事实")
        cold = _memory(2)
        runtime._publish(
            1,
            runtime._fingerprint(warm),
            _FakeMatrix(),
            allowEviction=True,
        )
        runtime._enqueueIndex(2, allowEviction=False)

        status = runtime.getSemanticCacheStatus([stale, cold])

        assert status["status"] == "cold"
        assert status["candidateCount"] == 2
        assert status["readyCount"] == 0
        assert status["missingCount"] == 1
        assert status["staleCount"] == 1
        assert status["pendingCount"] == 1
    finally:
        runtime._executor.shutdown(wait=True, cancel_futures=True)


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


def test_publishCountsSharedMatrixOnceAndDistinctMatricesSeparately():
    runtime = MemoryRuntime(encoderFactory=_FakeEncoder)
    sharedMatrix = _FakeMatrix(4)
    try:
        assert runtime._publish(
            1,
            "shared",
            sharedMatrix,
            sharedMatrix,
            allowEviction=True,
        )
        assert runtime._cacheBytes == 4

        assert runtime._publish(
            2,
            "distinct",
            _FakeMatrix(3),
            _FakeMatrix(5),
            allowEviction=True,
        )
        assert runtime._cacheBytes == 12
    finally:
        runtime._executor.shutdown(wait=True, cancel_futures=True)


def test_publishRejectsEntryWhenEitherRepresentationHasNoSize():
    runtime = MemoryRuntime(encoderFactory=_FakeEncoder)
    try:
        assert not runtime._publish(
            1,
            "invalid-enhanced",
            _FakeMatrix(4),
            _FakeMatrix(0),
            allowEviction=True,
        )
        assert runtime.getStatus()["cacheEntries"] == 0
        assert runtime.getStatus()["lastReason"] == "matrixTooLarge"
    finally:
        runtime._executor.shutdown(wait=True, cancel_futures=True)


def test_publish_rejects_matrix_larger_than_cache_budget(monkeypatch):
    monkeypatch.setattr(runtimeModule, "LLM_MEMORY_VECTOR_CACHE_BYTES", 4)
    runtime = MemoryRuntime(encoderFactory=_FakeEncoder)
    try:
        assert not runtime._publish(
            1,
            "too-large",
            _FakeMatrix(5),
            allowEviction=True,
        )
        assert runtime.getStatus()["cacheEntries"] == 0
        assert runtime.getStatus()["blockedFingerprints"] == 1
        assert runtime.getStatus()["lastReason"] == "matrixTooLarge"
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
async def test_pinned_memory_is_evicted_without_encoding():
    encoder = _FakeEncoder()
    runtime = MemoryRuntime(encoderFactory=lambda: encoder)
    runtime._encoder = encoder
    contextual = _memory(1)
    pinned = _memory(1, mode="pinned")
    runtime._publish(
        1,
        runtime._fingerprint(contextual),
        _FakeMatrix(),
        allowEviction=True,
    )
    runtime.notifyMemoryChanged(1)

    with patch(
        "utils.llm.memory.runtime.getMemoryByID",
        new_callable=AsyncMock,
        return_value=pinned,
    ):
        await runtime._processIndex()

    assert encoder.encodedMemoryIDs == []
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
        assert await scoreTask == [{"base": {}, "enhanced": {}}]
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
            "utils.llm.memory.runtime.getEnabledContextualMemoryPage",
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
async def test_reconcile_read_error_preserves_partial_scan_and_cache():
    runtime = MemoryRuntime(encoderFactory=_FakeEncoder)
    memory = _memory(5)
    runtime._publish(
        5,
        runtime._fingerprint(memory),
        _FakeMatrix(),
        allowEviction=True,
    )
    runtime._reconcileAfterID = 17
    runtime._reconcileSeen = {3, 5}

    with patch(
        "utils.llm.memory.runtime.getEnabledContextualMemoryPage",
        new_callable=AsyncMock,
        return_value=None,
    ):
        await runtime._reconcileStep()

    assert list(runtime._cache) == [5]
    assert runtime._reconcileAfterID == 17
    assert runtime._reconcileSeen == {3, 5}
    assert runtime._nextReconcile > time.monotonic()
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
            "utils.llm.memory.runtime.getEnabledContextualMemoryPage",
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


@pytest.mark.asyncio
async def test_worker_recovers_after_unexpected_iteration_error(monkeypatch):
    monkeypatch.setattr(runtimeModule, "LLM_MEMORY_WORKER_ERROR_BACKOFF_SECONDS", 0)
    runtime = MemoryRuntime(encoderFactory=_FakeEncoder)
    runtime._pendingIndex[1] = runtimeModule._IndexJob(
        queuedAt=time.monotonic(),
        allowEviction=False,
    )
    calls = 0

    async def _processIndex():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient failure")
        runtime._running = False

    with (
        patch("utils.llm.memory.runtime.getMemoryRetrievalMode", return_value="hybrid"),
        patch.object(runtime, "_ensureEncoder", new_callable=AsyncMock, return_value=True),
        patch.object(runtime, "_processIndex", side_effect=_processIndex),
        patch(
            "utils.llm.memory.runtime.logSystemEvent",
            new_callable=AsyncMock,
        ) as logEvent,
    ):
        await runtime.run()

    assert calls == 2
    logEvent.assert_awaited_once()
    assert runtime.getStatus()["workerStarted"] is False
    await runtime.close()




class _SelectorTestLease:
    """测试按事件显式完成lease，避免把任务结束误当作完整检索结束。"""

    def __init__(self):
        """每个测试lease都归属当前事件循环。"""
        self.completion = asyncio.get_running_loop().create_future()


async def test_selectorOwnershipOutlivesCallerAndEndsAtLeaseCompletion():
    """清除调用方引用不删除HTTP所有权，完整lease结束才移除记录。"""
    runtime = MemoryRuntime(encoderFactory=_FakeEncoder)
    lease = _SelectorTestLease()
    try:
        assert runtime.registerSelectorRetrieval(lease) is True
        assert runtime.registerSelectorRetrieval(lease) is False
        assert runtime._selectorRetrievals[lease].callerTask is asyncio.current_task()
        runtime.finishSelectorRetrieval(lease)
        assert runtime._selectorRetrievals[lease].callerTask is None
        assert runtime.getStatus()["selectorRetrievals"] == 1
        lease.completion.set_result(None)
        await asyncio.sleep(0)
        assert runtime.getStatus()["selectorRetrievals"] == 0
        assert runtime.registerSelectorRetrieval(lease) is False
    finally:
        await runtime.close()


async def test_selectorShutdownCancelsOnceAndLeavesCleanupOwned():
    """并发close只取消调用方与请求一次，慢清理继续持有lease直到完成。"""
    runtime = MemoryRuntime(encoderFactory=_FakeEncoder)
    lease = _SelectorTestLease()
    registered, callerCancelled, requestCancelled = (asyncio.Event() for _ in range(3))
    requestRelease, cleanupStarted, cleanupRelease = (asyncio.Event() for _ in range(3))
    tasks = {}

    async def request():
        """首次取消后模拟库内收尾，第二次取消会使本测试失败。"""
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            requestCancelled.set()
            await requestRelease.wait()
            raise

    async def cleanup():
        """等待请求终止后模拟慢关闭，完整关闭才完成测试lease。"""
        await asyncio.wait({tasks["request"]})
        cleanupStarted.set()
        await cleanupRelease.wait()
        lease.completion.set_result(None)

    async def caller():
        """持有整个检索范围；取消后清除自身引用但不触碰后台清理。"""
        assert runtime.registerSelectorRetrieval(lease)
        tasks["request"] = asyncio.create_task(request())
        tasks["cleanup"] = asyncio.create_task(cleanup())
        runtime.trackSelectorTask(lease, tasks["request"], receipt={})
        runtime.trackSelectorTask(lease, tasks["cleanup"], cleanup=True)
        registered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            callerCancelled.set()
            raise
        finally:
            runtime.finishSelectorRetrieval(lease)

    tasks["caller"] = asyncio.create_task(caller())
    await registered.wait()
    await asyncio.sleep(0)
    closes = [asyncio.create_task(runtime.closeSelectors()) for _ in range(2)]
    try:
        await callerCancelled.wait()
        await requestCancelled.wait()
        assert tasks["caller"].cancelling() == 1
        assert tasks["request"].cancelling() == 1
        assert tasks["cleanup"].cancelling() == 0
        assert runtime.selectorAccepting() is False
        assert runtime.registerSelectorRetrieval(_SelectorTestLease()) is False
        requestRelease.set()
        await cleanupStarted.wait()
        assert runtime.getStatus()["selectorRetrievals"] == 1
        assert all(not task.done() for task in closes)
        cleanupRelease.set()
        await asyncio.gather(*closes)
        assert runtime.getStatus()["selectorRetrievals"] == 0
    finally:
        requestRelease.set()
        cleanupRelease.set()
        await asyncio.gather(*tasks.values(), *closes, return_exceptions=True)
        await runtime.close()


async def test_selectorShutdownDeadlineIsSharedAndDoesNotCancelCleanup(monkeypatch):
    """等待到期只报告未完成；第二次close既不重设预算也不取消Future。"""
    monkeypatch.setattr(runtimeModule, "LLM_MEMORY_SELECTOR_SHUTDOWN_SECONDS", .02)
    logger = AsyncMock()
    monkeypatch.setattr(runtimeModule, "logSystemEvent", logger)
    runtime = MemoryRuntime(encoderFactory=_FakeEncoder)
    lease = _SelectorTestLease()
    release = asyncio.Event()

    async def cleanup():
        """模拟超出宽限的关闭，待测试主动释放后完成lease。"""
        await release.wait()
        lease.completion.set_result(None)

    runtime.registerSelectorRetrieval(lease)
    runtime.finishSelectorRetrieval(lease)
    task = asyncio.create_task(cleanup())
    runtime.trackSelectorTask(lease, task, cleanup=True, receipt={})
    try:
        await runtime.closeSelectors()
        deadline = runtime._selectorCloseDeadline
        assert not task.done() and not task.cancelling()
        assert not lease.completion.done()
        assert runtime.getStatus()["selectorShutdownPending"] == 1
        with patch.object(runtimeModule.asyncio, "wait", side_effect=AssertionError("deadline extended")):
            await runtime.closeSelectors()
        assert runtime._selectorCloseDeadline == deadline
        logger.assert_awaited_once()
        assert "pendingRetrievals=1" in logger.await_args.args[1]
        assert runtime.getStatus()["selectorRetrievals"] == 1
    finally:
        release.set()
        await task
        await runtime.close()


async def test_selectorFinishedCallerIsNeverCancelledByShutdown():
    """同一协程已进入主回答时，旧检索的慢清理不能让停机取消主回答。"""
    runtime = MemoryRuntime(encoderFactory=_FakeEncoder)
    lease = _SelectorTestLease()
    answerStarted, answerRelease = asyncio.Event(), asyncio.Event()

    async def caller():
        """检索结束后在同一任务等待合成主回答。"""
        runtime.registerSelectorRetrieval(lease)
        runtime.finishSelectorRetrieval(lease)
        answerStarted.set()
        await answerRelease.wait()

    task = asyncio.create_task(caller())
    await answerStarted.wait()
    closeTask = asyncio.create_task(runtime.closeSelectors())
    try:
        await asyncio.sleep(0)
        assert task.cancelling() == 0 and not task.done()
        lease.completion.set_result(None)
        await closeTask
        assert task.cancelling() == 0
    finally:
        answerRelease.set()
        await asyncio.gather(task, closeTask)
        await runtime.close()


async def test_selectorCleanupFailureRemainsVisibleWithoutPrivateText(monkeypatch):
    """完成任务中的关闭错误保留计数，日志不回显receipt中的异常内容。"""
    logger = AsyncMock()
    monkeypatch.setattr(runtimeModule, "logSystemEvent", logger)
    runtime = MemoryRuntime(encoderFactory=_FakeEncoder)
    lease = _SelectorTestLease()
    runtime.registerSelectorRetrieval(lease)
    runtime._selectorRetrievals[lease].receipt = {"transportCloseError": "private-secret-response"}
    runtime.finishSelectorRetrieval(lease)
    lease.completion.set_result(None)
    await asyncio.sleep(0)
    try:
        assert runtime.getStatus()["selectorCleanupFailures"] == 1
        await runtime.closeSelectors()
        await runtime.closeSelectors()
        logger.assert_awaited_once()
        assert "cleanupFailures=1" in logger.await_args.args[1]
        assert "private-secret" not in repr(logger.await_args)
    finally:
        await runtime.close()


async def test_selectorCloseCancellationLeavesCleanupAndOriginalDeadline(monkeypatch):
    """取消等待方不会取消lease；再次关闭沿用第一次的deadline。"""
    runtime = MemoryRuntime(encoderFactory=_FakeEncoder)
    lease = _SelectorTestLease()
    runtime.registerSelectorRetrieval(lease)
    runtime.finishSelectorRetrieval(lease)
    first = asyncio.create_task(runtime.closeSelectors())
    await asyncio.sleep(0)
    deadline = runtime._selectorCloseDeadline
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not lease.completion.done()
    lease.completion.set_result(None)
    await runtime.closeSelectors()
    assert runtime._selectorCloseDeadline == deadline
    await runtime.close()


async def test_runtimeDirectCloseFinishesSelectorsBeforeEncoder():
    """绕过ResourceManager直接close仍先等待selector，再释放encoder。"""
    encoder = _FakeEncoder()
    runtime = MemoryRuntime(encoderFactory=lambda: encoder)
    runtime._encoder = encoder
    lease = _SelectorTestLease()
    runtime.registerSelectorRetrieval(lease)
    runtime.finishSelectorRetrieval(lease)
    task = asyncio.create_task(runtime.close())
    await asyncio.sleep(0)
    try:
        assert runtime.getStatus()["closing"]
        assert not runtime.selectorAccepting() and not encoder.closed
        lease.completion.set_result(None)
        await task
        assert encoder.closed
    finally:
        if not lease.completion.done():
            lease.completion.set_result(None)
        await task


async def test_selectorRegistrationUsesResourceOrderAndIsIdempotent(monkeypatch):
    """正式注册使用40/30/20顺序，重复初始化不创建第二份生命周期。"""
    from utils.core.resourceManager import ResourceManager

    events = []
    manager = ResourceManager()
    state = SimpleNamespace(value=None)
    state.getMemoryRuntime = lambda: state.value
    state.setMemoryRuntime = lambda value: setattr(state, "value", value)

    async def selectors():
        """记录选择器收尾顺序。"""
        events.append("selectors")

    async def runtimeClose():
        """记录编码器运行时收尾顺序。"""
        events.append("runtime")

    async def checkpoint():
        """记录数据库checkpoint必须最后执行。"""
        events.append("database")

    runtime = SimpleNamespace(closeSelectors=selectors, close=runtimeClose)
    factory = MagicMock(return_value=runtime)
    monkeypatch.setattr(runtimeModule, "MemoryRuntime", factory)
    monkeypatch.setattr(runtimeModule, "getStateManager", lambda: state)
    monkeypatch.setattr(runtimeModule, "getResourceManager", lambda: manager)
    manager.register("database checkpoint", checkpoint, priority=20)
    runtimeModule.registerMemoryRuntime()
    runtimeModule.registerMemoryRuntime()
    factory.assert_called_once_with()
    await manager.cleanupAll()
    assert events == ["selectors", "runtime", "database"]
