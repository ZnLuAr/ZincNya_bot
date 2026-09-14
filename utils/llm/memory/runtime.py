"""
utils/llm/memory/runtime.py

hybrid 检索语义通道的后台运行时：维护记忆向量缓存并响应查询评分。

全进程单实例，由 registerMemoryRuntime 创建、经 stateManager 暴露、
由 backgroundTasks 驱动 run() 循环。核心职责：

- 增量索引：记忆写入/修改/删除后重新编码其向量（由 database 发通知，
  每 30 秒的全库巡检兜底；容量允许时补齐，容量饱和时对冷条目采用
  best-effort 跳过，因此不宣称无条件最终一致）；
- 查询评分：用缓存向量计算候选记忆与查询的语义相似度；
- 缓存治理：向量缓存不超过 32MB，超出按最久未使用淘汰。

数据库是唯一正本，缓存可整体丢弃重建。所有 ONNX 调用统一排入
单线程 executor 串行执行，事件循环线程只做队列调度。legacy 模式下
循环休眠、不加载模型，切换 hybrid 后才开始工作。
"""

import asyncio
import time
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from config import (
    LLM_MEMORY_INDEX_PAGE_SIZE,
    LLM_MEMORY_INDEX_QUEUE_LIMIT,
    LLM_MEMORY_QUERY_BURST_LIMIT,
    LLM_MEMORY_QUERY_QUEUE_LIMIT,
    LLM_MEMORY_RECONCILE_SECONDS,
    LLM_MEMORY_VECTOR_CACHE_BYTES,
    LLM_MEMORY_WORKER_ERROR_BACKOFF_SECONDS,
)

from utils.core.logger import LogLevel, logSystemEvent
from utils.core.resourceManager import getResourceManager
from utils.core.stateManager import getStateManager
from utils.llm.config import getMemoryRetrievalMode

from .database import (
    MEMORY_MODE_CONTEXTUAL,
    getEnabledContextualMemoryPage,
    getMemoryByID,
)
from .encoder import MemoryEncoder, loadModelManifest
from .types import buildMemoryContentFingerprint




@dataclass
class _CacheEntry:
    """
    一条记忆的向量缓存项。

    fingerprint 是操作的向量记忆内容的指纹；下次检索重算一次，
    若两次指纹不相等（记忆/模型更改）就作废重编码。
    """

    fingerprint: str
    matrix: object
    sizeBytes: int


@dataclass
class _QueryJob:
    """
    排队等待评分的查询任务。

    cacheSnapshot 在入队时定格本次使用的向量集合：排队期间后台
    重编码某条记忆，本次评分仍用入队时的旧矩阵——评分输入与结果
    保持自洽，不受中途替换影响。
    """

    queryTexts: tuple[str, ...]
    cacheSnapshot: tuple[tuple[int, object], ...]
    deadline: float
    future: asyncio.Future


@dataclass
class _IndexJob:
    """
    单条记忆的重编码待办；同 ID 在队列中仅保留一项。

    allowEviction 标记来源：来自写入/修改通知的为 True（新内容需
    尽快可查，允许驱逐既有缓存）；来自周期巡检补漏的为 False
    （补编码不允许驱逐使用中的缓存）。
    """

    queuedAt: float
    allowEviction: bool




class MemoryRuntime:
    """
    持有编码器、待编码队列和向量缓存的单例调度者。

    查询评分和重新编码共用一个工作线程（模型只加载一份，也不并发
    抢 CPU）；记忆的增删改经 notifyMemoryChanged 进待编码队列，
    30 秒一次的库内巡检负责补上漏掉的通知。巡检不驱逐热缓存，故在
    字节预算饱和时只能尽力补齐，并由状态字段暴露未覆盖情况。
    """

    def __init__(self, *, encoderFactory=MemoryEncoder):
        """初始化单 worker 调度器；此时不会加载可选模型依赖。"""
        self._encoderFactory = encoderFactory
        self._modelRevision = "unavailable"
        self._encodingVersion = "unavailable"
        self._encoder = None
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="llm-memory",
        )
        # 两个 OrderedDict 分别承担 FIFO 待办队列和 LRU 缓存：待办按 ID 合并，
        # 缓存按最近访问顺序淘汰；两者都在事件循环线程内修改，不额外引入锁。
        self._queryJobs = deque()
        self._pendingIndex = OrderedDict()
        self._cache = OrderedDict()
        self._cacheBytes = 0
        self._wakeEvent = asyncio.Event()
        self._running = True
        self._closing = False
        self._workerStarted = False
        self._queryBurst = 0
        self._nextEncoderAttempt = 0.0
        self._nextReconcile = 0.0
        self._reconcileAfterID = 0
        self._reconcileSeen = set()
        self._reconcileCapacitySaturated = False
        self._blockedFingerprints = {}
        self._activeNativeJobs = 0
        self._nativeIdleEvent = asyncio.Event()
        self._nativeIdleEvent.set()
        self._activeQueryJob = None
        self._workerTask = None
        self._closedEvent = asyncio.Event()
        self._stats = {
            "queryRejected": 0,
            "queryTimedOut": 0,
            "indexDropped": 0,
            "staleResults": 0,
            "encodeFailures": 0,
            "workerFailures": 0,
            "lastReason": None,
        }


    def _fingerprint(self, memory: dict) -> str:
        """按当前模型 revision 与编码版本计算缓存内容指纹。"""
        return buildMemoryContentFingerprint(
            memory,
            modelRevision=self._modelRevision,
            encodingVersion=self._encodingVersion,
        )


    def _evict(self, memoryID: int) -> bool:
        """移除一条热缓存并同步容量计数；不存在时返回 False。"""
        entry = self._cache.pop(int(memoryID), None)
        if entry is not None:
            self._cacheBytes -= entry.sizeBytes
            self._reconcileCapacitySaturated = False
            return True
        return False


    def _publish(
        self,
        memoryID: int,
        fingerprint: str,
        matrix,
        *,
        allowEviction: bool,
    ) -> bool:
        """将新算好的向量写入缓存；超出字节预算时按 allowEviction 决定是否驱逐旧项。"""
        sizeBytes = int(getattr(matrix, "nbytes", 0))
        # 单个矩阵不能超过总预算；reconcile 不允许为了一个条目驱逐已有热缓存，
        # 而用户刚修改的条目可以驱逐旧项以尽快可用。
        if sizeBytes <= 0 or sizeBytes > LLM_MEMORY_VECTOR_CACHE_BYTES:
            self._stats["lastReason"] = "matrixTooLarge"
            self._blockedFingerprints[int(memoryID)] = fingerprint
            self._evict(memoryID)
            return False

        self._blockedFingerprints.pop(int(memoryID), None)
        self._evict(memoryID)
        if (
            not allowEviction
            and self._cacheBytes + sizeBytes > LLM_MEMORY_VECTOR_CACHE_BYTES
        ):
            self._reconcileCapacitySaturated = True
            self._stats["lastReason"] = "reconcileCapacity"
            return False

        while self._cache and self._cacheBytes + sizeBytes > LLM_MEMORY_VECTOR_CACHE_BYTES:
            oldestID = next(iter(self._cache))
            self._evict(oldestID)
        if self._cacheBytes + sizeBytes > LLM_MEMORY_VECTOR_CACHE_BYTES:
            return False

        self._cache[int(memoryID)] = _CacheEntry(
            fingerprint=fingerprint,
            matrix=matrix,
            sizeBytes=sizeBytes,
        )
        self._cacheBytes += sizeBytes
        return True


    def _enqueueIndex(self, memoryID: int, *, allowEviction: bool) -> bool:
        """把记忆 ID 加进待编码队列；同 ID 已在队列就只升级权限不重复排。"""
        memoryID = int(memoryID)
        # 更新通知可以在短时间内连续到达，按 ID 合并既减少重复编码，也让队列
        # 上限真正限制“不同 memory 的待办数”。
        existing = self._pendingIndex.get(memoryID)
        if existing is not None:
            if allowEviction:
                existing.allowEviction = True
            return True
        if (
            not allowEviction
            and self._reconcileCapacitySaturated
            and memoryID not in self._cache
        ):
            return False
        if len(self._pendingIndex) >= LLM_MEMORY_INDEX_QUEUE_LIMIT:
            self._stats["indexDropped"] += 1
            self._stats["lastReason"] = "indexQueueFull"
            return False

        self._pendingIndex[memoryID] = _IndexJob(
            queuedAt=time.monotonic(),
            allowEviction=allowEviction,
        )
        return True


    def _requestMemoryIndex(self, memoryID: int, *, allowEviction: bool) -> bool:
        """按来源排入索引待办，并在成功入队后唤醒 worker。

        数据库写入通知使用 ``allowEviction=True``，让刚变更的事实尽快
        可查；查询发现和周期对账使用 False，不能仅因被某次查询看到就
        驱逐已有热缓存。
        """
        if not self._running:
            return False
        queued = self._enqueueIndex(memoryID, allowEviction=allowEviction)
        if queued:
            self._wakeEvent.set()
        return queued


    def notifyMemoryChanged(self, memoryID: int) -> None:
        """记忆已变更，排队重新编码；队列满时丢弃。

        周期巡检会在容量允许时补排；容量饱和时为保护热缓存而暂缓冷条目，
        因而这里的恢复保证是 best-effort，而不是无条件最终一致。
        """
        self._requestMemoryIndex(memoryID, allowEviction=True)


    def notifyModeChanged(self) -> None:
        """唤醒 worker，并允许切到 hybrid 后立即重试加载 encoder。"""
        self._nextEncoderAttempt = 0.0
        self._wakeEvent.set()


    def getStatus(self) -> dict:
        """返回不含 memory 正文和 hint 的只读运行状态。"""
        oldestQueuedAt = (
            next(iter(self._pendingIndex.values())).queuedAt
            if self._pendingIndex
            else None
        )
        return {
            "running": self._running,
            "closing": self._closing,
            "workerStarted": self._workerStarted,
            "encoderReady": self._encoder is not None,
            "queryQueued": len(self._queryJobs),
            "indexPending": len(self._pendingIndex),
            "oldestIndexAgeMs": (
                round((time.monotonic() - oldestQueuedAt) * 1000, 2)
                if oldestQueuedAt is not None
                else 0.0
            ),
            "cacheEntries": len(self._cache),
            "cacheBytes": self._cacheBytes,
            "reconcileCapacitySaturated": self._reconcileCapacitySaturated,
            "blockedFingerprints": len(self._blockedFingerprints),
            "activeNativeJob": self._activeNativeJobs > 0,
            "activeNativeJobs": self._activeNativeJobs,
            **self._stats,
        }


    def getSemanticCacheStatus(self, candidates: list[dict]) -> dict:
        """返回本次候选的语义缓存覆盖快照，不暴露记忆内容。

        ``scoreSemantic`` 对冷条目只排入后台编码，不会在请求线程同步
        补向量。因此检索 diagnostics 需要区分 warm、partial 和 cold，
        否则“语义没有命中”很容易被误判为模型质量问题。这里是进入
        评分前的瞬时观察，不承诺请求返回时缓存仍完全相同。
        """
        contextualCandidates = [
            memory for memory in candidates
            if memory.get("mode", MEMORY_MODE_CONTEXTUAL) == MEMORY_MODE_CONTEXTUAL
        ]
        readyCount = 0
        staleCount = 0
        missingCount = 0
        pendingCount = 0
        for memory in contextualCandidates:
            memoryID = int(memory["id"])
            entry = self._cache.get(memoryID)
            if entry is None:
                missingCount += 1
            else:
                try:
                    isCurrent = entry.fingerprint == self._fingerprint(memory)
                except Exception:
                    isCurrent = False
                if isCurrent:
                    readyCount += 1
                else:
                    staleCount += 1
            if memoryID in self._pendingIndex:
                pendingCount += 1

        candidateCount = len(contextualCandidates)
        if not candidateCount:
            cacheState = "empty"
        elif readyCount == candidateCount:
            cacheState = "warm"
        elif readyCount == 0:
            cacheState = "cold"
        else:
            cacheState = "partial"

        return {
            "status": cacheState,
            "candidateCount": candidateCount,
            "readyCount": readyCount,
            "missingCount": missingCount,
            "staleCount": staleCount,
            "pendingCount": pendingCount,
            "cacheEntries": len(self._cache),
            "cacheBytes": self._cacheBytes,
            "reconcileCapacitySaturated": self._reconcileCapacitySaturated,
        }


    async def _runNative(self, function, *args):
        """在唯一 native worker 执行阻塞调用，并让取消等待真实任务收尾。

        ThreadPoolExecutor 已提交的 ONNX 调用无法被协程取消安全打断；
        取消等待方时屏蔽 CancelledError 继续等到 native 收尾，防止
        encoder/executor 在 ONNX 线程还在读写时被提前释放（段错误）。
        """
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self._executor, function, *args)
        self._activeNativeJobs += 1
        self._nativeIdleEvent.clear()
        try:
            try:
                return await asyncio.shield(future)
            except asyncio.CancelledError as cancelled:
                # 取消协程不能安全取消已经提交到 ThreadPoolExecutor 的 ONNX 调用；
                # 这里等待 native future 收尾，再向上层重新抛出取消，避免关闭时并发
                # 释放 encoder 或 executor。
                while not future.done():
                    try:
                        await asyncio.shield(future)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if future.done() and not future.cancelled():
                    try:
                        future.exception()
                    except Exception:
                        pass
                raise cancelled
        finally:
            self._activeNativeJobs -= 1
            if self._activeNativeJobs == 0:
                self._nativeIdleEvent.set()


    async def _ensureEncoder(self) -> bool:
        """首次使用时才创建 encoder；失败记 WARNING，30 秒后重试。

        不在启动时加载：legacy 模式用不到模型，且创建失败不应影响
        bot 启动——encoder 缺席时检索仅靠词面通道。
        """
        if self._encoder is not None:
            return True
        now = time.monotonic()
        if now < self._nextEncoderAttempt:
            return False

        try:
            manifest = loadModelManifest()
            self._modelRevision = manifest["revision"]
            self._encodingVersion = manifest["encodingVersion"]
            self._encoder = await self._runNative(self._encoderFactory)
            self._stats["lastReason"] = None
            return True
        except Exception as exc:
            self._modelRevision = "unavailable"
            self._encodingVersion = "unavailable"
            self._stats["encodeFailures"] += 1
            self._stats["lastReason"] = type(exc).__name__
            self._nextEncoderAttempt = now + LLM_MEMORY_RECONCILE_SECONDS
            await logSystemEvent(
                "LLM memory 编码器不可用",
                type(exc).__name__,
                LogLevel.WARNING,
            )
            return False


    def _scoreQuerySync(self, queryTexts, cacheSnapshot):
        """同步计算查询与缓存 chunks 的相似度，并聚合到 memory ID。"""
        # 一个 memory 可能被切成多个 chunk；对每个查询取 chunk 的最大相似度，
        # 再返回 memory ID -> score，后续检索层只处理 memory 粒度的分数。
        queryVectors = self._encoder.encodeQueries(queryTexts)
        results = [dict() for _ in queryTexts]
        for memoryID, matrix in cacheSnapshot:
            for queryIndex in range(len(queryTexts)):
                similarities = matrix @ queryVectors[queryIndex]
                results[queryIndex][memoryID] = float(similarities.max())
        return results


    async def scoreSemantic(
        self,
        queryTexts: list[str],
        candidates: list[dict],
        *,
        deadline: float,
    ) -> list[dict[int, float]]:
        """计算候选记忆与各查询文本的语义相似度，返回 [{memoryID: 分数}]。

        只使用缓存中已有的向量：未编码或向量过期的记忆排入后台补编码，
        本次不给语义分——不为单条记忆在请求线程同步编码（首次访问
        延迟因此可控，冷启动阶段由词面通道兜底）。查询队列已满、
        超时、encoder 未就绪均返回空 dict，由检索层按通道缺席处理。
        """
        emptyResult = [dict() for _ in queryTexts]
        if not queryTexts or not self._running:
            return emptyResult
        if (
            self._encoder is None
            or self._modelRevision is None
            or self._encodingVersion is None
        ):
            self._wakeEvent.set()
            for memory in candidates:
                if memory.get("mode", MEMORY_MODE_CONTEXTUAL) == MEMORY_MODE_CONTEXTUAL:
                    self._requestMemoryIndex(memory["id"], allowEviction=False)
            return emptyResult

        # 过期或缺失的缓存只触发有界后台建索引，不在当前请求同步编码；这使
        # 首次访问的延迟可预测，并允许 lexical 通道在语义缓存未就绪时继续工作。
        cacheSnapshot = []
        for memory in candidates:
            memoryID = int(memory["id"])
            if memory.get("mode", MEMORY_MODE_CONTEXTUAL) != MEMORY_MODE_CONTEXTUAL:
                self._evict(memoryID)
                continue
            fingerprint = self._fingerprint(memory)
            entry = self._cache.get(memoryID)
            if entry is None or entry.fingerprint != fingerprint:
                if entry is not None:
                    self._evict(memoryID)
                self._requestMemoryIndex(memoryID, allowEviction=False)
                continue
            self._cache.move_to_end(memoryID)
            cacheSnapshot.append((memoryID, entry.matrix))

        if not cacheSnapshot:
            return emptyResult
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            self._stats["queryTimedOut"] += 1
            self._stats["lastReason"] = "queryTimeout"
            return emptyResult
        if len(self._queryJobs) >= LLM_MEMORY_QUERY_QUEUE_LIMIT:
            self._stats["queryRejected"] += 1
            self._stats["lastReason"] = "queryQueueFull"
            return emptyResult

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._queryJobs.append(_QueryJob(
            queryTexts=tuple(queryTexts),
            cacheSnapshot=tuple(cacheSnapshot),
            deadline=deadline,
            future=future,
        ))
        self._wakeEvent.set()

        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=remaining)
        except asyncio.TimeoutError:
            future.cancel()
            self._stats["queryTimedOut"] += 1
            self._stats["lastReason"] = "queryTimeout"
            return emptyResult
        except asyncio.CancelledError:
            future.cancel()
            raise


    async def _processQuery(self) -> None:
        """处理队首查询，并把异常或过期任务收敛为空分数结果。"""
        job = self._queryJobs.popleft()
        self._activeQueryJob = job
        try:
            if job.future.cancelled():
                return
            if time.monotonic() >= job.deadline:
                if not job.future.done():
                    job.future.set_result([dict() for _ in job.queryTexts])
                return

            try:
                result = await self._runNative(
                    self._scoreQuerySync,
                    job.queryTexts,
                    job.cacheSnapshot,
                )
            except Exception:
                self._stats["encodeFailures"] += 1
                self._stats["lastReason"] = "queryEncodeFailed"
                result = [dict() for _ in job.queryTexts]
            if not self._running:
                result = [dict() for _ in job.queryTexts]
            if not job.future.done():
                job.future.set_result(result)
        finally:
            self._activeQueryJob = None


    async def _processIndex(self) -> None:
        """取队首待办，重新编码该记忆并发布到缓存。

        发布前回数据库重读并比对指纹：编码耗时几秒，期间记录可能
        再次变更；指纹不一致则丢弃本次结果重新排队，防止旧内容的
        向量覆盖新版本。
        """
        memoryID, job = self._pendingIndex.popitem(last=False)
        if (
            not job.allowEviction
            and self._reconcileCapacitySaturated
            and memoryID not in self._cache
        ):
            return
        memory = await getMemoryByID(memoryID)
        if (
            not memory
            or not memory.get("enabled")
            or memory.get("mode", MEMORY_MODE_CONTEXTUAL) != MEMORY_MODE_CONTEXTUAL
        ):
            self._blockedFingerprints.pop(memoryID, None)
            self._evict(memoryID)
            return

        fingerprint = self._fingerprint(memory)
        entry = self._cache.get(memoryID)
        if entry is not None and entry.fingerprint == fingerprint:
            return
        if self._blockedFingerprints.get(memoryID) == fingerprint:
            return
        self._evict(memoryID)

        try:
            matrix = await self._runNative(self._encoder.encodeMemory, memory)
        except Exception:
            self._stats["encodeFailures"] += 1
            self._stats["lastReason"] = "memoryEncodeFailed"
            return

        if not self._running:
            return
        # 编码期间记录可能再次变化；发布前重新读取并比较指纹，丢弃旧结果而不是
        # 用慢任务覆盖新内容。
        current = await getMemoryByID(memoryID)
        if (
            not current
            or not current.get("enabled")
            or current.get("mode", MEMORY_MODE_CONTEXTUAL) != MEMORY_MODE_CONTEXTUAL
            or self._fingerprint(current) != fingerprint
        ):
            self._stats["staleResults"] += 1
            return
        self._publish(
            memoryID,
            fingerprint,
            matrix,
            allowEviction=job.allowEviction,
        )


    async def _reconcileStep(self) -> None:
        """周期巡检的单步：分页读取下一批启用记忆。

        巡检补两件事：通知丢失或队列满导致漏编码的记忆在此补排；
        一轮完整扫描结束后，缓存中存在但本轮未出现的记忆即为已
        删除/禁用，从缓存中清除。
        """
        page = await getEnabledContextualMemoryPage(
            self._reconcileAfterID,
            LLM_MEMORY_INDEX_PAGE_SIZE,
        )
        if page is None:
            self._stats["lastReason"] = "reconcileReadFailed"
            self._nextReconcile = time.monotonic() + LLM_MEMORY_RECONCILE_SECONDS
            return
        if not page:
            for memoryID in list(self._cache):
                if memoryID not in self._reconcileSeen:
                    self._evict(memoryID)
            self._reconcileSeen.clear()
            self._reconcileAfterID = 0
            self._nextReconcile = time.monotonic() + LLM_MEMORY_RECONCILE_SECONDS
            return

        if self._stats["lastReason"] == "reconcileReadFailed":
            self._stats["lastReason"] = None

        for memory in page:
            memoryID = int(memory["id"])
            self._reconcileSeen.add(memoryID)
            entry = self._cache.get(memoryID)
            fingerprint = self._fingerprint(memory)
            if entry is not None and entry.fingerprint == fingerprint:
                continue
            if self._blockedFingerprints.get(memoryID) == fingerprint:
                continue
            if entry is None and self._reconcileCapacitySaturated:
                continue
            self._enqueueIndex(memoryID, allowEviction=False)
        self._reconcileAfterID = int(page[-1]["id"])


    async def _waitForWork(self, timeout: float | None = None) -> None:
        """阻塞等待唤醒事件；带 timeout 时最多等到下一次巡检时间点。"""
        self._wakeEvent.clear()
        try:
            if timeout is None:
                await self._wakeEvent.wait()
            else:
                await asyncio.wait_for(self._wakeEvent.wait(), timeout=max(timeout, 0.01))
        except asyncio.TimeoutError:
            pass


    async def run(self) -> None:
        """后台调度主循环（backgroundTasks 驱动，进程内仅一份）。

        每轮按优先级执行：排队中的查询评分（延迟敏感，优先；但连做
        8 笔后强制让位一次，防止索引长期饥饿）→ 待编码队列 → 周期
        巡检，无事可做则休眠。legacy 模式下持续休眠，等待
        notifyModeChanged 唤醒后重新评估。
        """
        currentTask = asyncio.current_task()
        if self._workerTask is not None and self._workerTask is not currentTask:
            return
        if not self._running:
            return
        self._workerTask = currentTask
        self._workerStarted = True
        try:
            while self._running:
                try:
                    if getMemoryRetrievalMode() != "hybrid":
                        await self._waitForWork()
                        continue
                    if not await self._ensureEncoder():
                        await self._waitForWork(
                            self._nextEncoderAttempt - time.monotonic()
                        )
                        continue

                    # 查询优先但受 burst 限制，避免持续聊天流量让后台索引永远饥饿。
                    if self._queryJobs and (
                        self._queryBurst < LLM_MEMORY_QUERY_BURST_LIMIT
                        or not self._pendingIndex
                    ):
                        await self._processQuery()
                        self._queryBurst += 1
                        continue
                    if self._pendingIndex:
                        await self._processIndex()
                        self._queryBurst = 0
                        continue
                    if time.monotonic() >= self._nextReconcile:
                        await self._reconcileStep()
                        continue

                    await self._waitForWork(self._nextReconcile - time.monotonic())
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._stats["workerFailures"] += 1
                    self._stats["lastReason"] = type(exc).__name__
                    await logSystemEvent(
                        "LLM memory 后台任务异常",
                        type(exc).__name__,
                        LogLevel.ERROR,
                        exception=exc,
                    )
                    if self._running:
                        await asyncio.sleep(LLM_MEMORY_WORKER_ERROR_BACKOFF_SECONDS)
        except asyncio.CancelledError:
            raise
        finally:
            if self._running:
                self._running = False
                self._finishPendingQueries()
            self._workerStarted = False
            if self._workerTask is currentTask:
                self._workerTask = None


    def _finishPendingQueries(self) -> None:
        """以空结果完成所有等待中的查询，供 worker 退出和 close 共用。"""
        activeQuery = self._activeQueryJob
        if activeQuery is not None and not activeQuery.future.done():
            activeQuery.future.set_result([
                dict() for _ in activeQuery.queryTexts
            ])
        while self._queryJobs:
            job = self._queryJobs.popleft()
            if not job.future.done():
                job.future.set_result([dict() for _ in job.queryTexts])


    async def close(self) -> None:
        """
        进程关停时调用（resourceManager 登记），按依赖顺序释放资源。

        有顺序约束：先停止主循环并向等待中的查询返回空结果，等待工作
        线程完成在途的 ONNX 调用，最后释放 encoder 与 executor——
        顺序颠倒会使 ONNX 线程访问已释放的模型对象，导致段错误。
        """
        if self._closing:
            await self._closedEvent.wait()
            return
        self._closing = True
        self._running = False
        self._wakeEvent.set()
        self._pendingIndex.clear()
        self._finishPendingQueries()

        try:
            # 先停止接收新工作并完成正在运行的 native 调用，再关闭 encoder 和
            # executor；否则 ONNX 线程可能仍在访问已释放的模型对象。
            workerTask = self._workerTask
            if workerTask is not None and workerTask is not asyncio.current_task():
                await asyncio.gather(workerTask, return_exceptions=True)
            await self._nativeIdleEvent.wait()

            if self._encoder is not None:
                try:
                    await self._runNative(self._encoder.close)
                except Exception:
                    pass
                self._encoder = None
            self._executor.shutdown(wait=True, cancel_futures=True)
        finally:
            self._cache.clear()
            self._cacheBytes = 0
            self._blockedFingerprints.clear()
            self._reconcileSeen.clear()
            self._reconcileCapacitySaturated = False
            state = getStateManager()
            if state.getMemoryRuntime() is self:
                state.setMemoryRuntime(None)
            self._closedEvent.set()




def registerMemoryRuntime() -> None:
    """
    bot 启动时创建 runtime 并注册到 stateManager。

    调用方是模块系统的字符串反射而非 Python 调用层：modulesRegistry
    的 llm 条目 initFunctions 列有 "utils.llm.memory.runtime:registerMemoryRuntime"，
    appLifecycle 启动时按该字符串 importlib 动态加载并调用本函数。
    仅创建空对象、不加载模型——模型延迟到 hybrid 模式首次使用时
    加载。
    
    若已存在实例，直接返回。
    """
    state = getStateManager()
    if state.getMemoryRuntime() is not None:
        return
    runtime = MemoryRuntime()
    state.setMemoryRuntime(runtime)
    getResourceManager().register(
        "LLM memory runtime",
        runtime.close,
        priority=30,
    )


async def runMemoryIndexWorker() -> None:
    """
    后台主循环的启动壳，真正的循环在 MemoryRuntime.run() 里。

    调用方同样不是 Python 代码，而是 modulesRegistry 的 backgroundTasks
    条目 "utils.llm.memory.runtime:runMemoryIndexWorker"——appLifecycle
    把它 asyncio.create_task 成一个长期任务，与控制台同生命周期：
    控制台退出或收到关机信号时被 task.cancel()，循环经 CancelledError
    结束；资源释放则由 registerMemoryRuntime 登记的 close() 在关停
    流程中负责。
    
    runtime 未注册（模块被禁用）时空跑直接返回。
    """
    runtime = getStateManager().getMemoryRuntime()
    if runtime is None:
        return
    await runtime.run()
