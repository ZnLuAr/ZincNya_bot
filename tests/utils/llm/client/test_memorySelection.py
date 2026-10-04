"""单次选择传输的离线契约、真实流关闭及取消所有权测试。"""

import asyncio
import gzip
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from utils.llm.client import memorySelection as client
from utils.llm.memory import retrieval as retrievalModule
from utils.llm.memory import runtime as runtimeModule




class TrackingStream(httpx.AsyncByteStream):
    """真正异步迭代的响应流，分别控制读取、取消与底层关闭。"""

    def __init__(self, content=b'{"ok":true}', *, blocked=False, slowClose=False,
                 closeError=False, suppressCancellation=False):
        """不借用 HTTPX eager content 路径，避免已预先关闭流掩盖资源问题。"""
        self.chunks = [content[:len(content) // 2], content[len(content) // 2:]]
        self.readGate = asyncio.Event()
        self.closeGate = asyncio.Event()
        self.readStarted = asyncio.Event()
        self.readCancelled = asyncio.Event()
        self.closeStarted = asyncio.Event()
        self.closeError = closeError
        self.suppressCancellation = suppressCancellation
        self.cancelCount = 0
        self.closeCalls = 0
        self.closeCancelled = False
        self.closed = False
        if not blocked:
            self.readGate.set()
        if not slowClose:
            self.closeGate.set()


    async def __aiter__(self):
        """允许模拟遵守取消的读取，或收到取消后仍晚到的服务成功响应。"""
        self.readStarted.set()
        try:
            await self.readGate.wait()
        except asyncio.CancelledError:
            self.cancelCount += 1
            self.readCancelled.set()
            if not self.suppressCancellation:
                raise
            await self.readGate.wait()
        for chunk in self.chunks:
            yield chunk


    async def aclose(self):
        """只有底层关闭确实完成才置 closed；异常不包含真实服务数据。"""
        self.closeCalls += 1
        self.closeStarted.set()
        try:
            await self.closeGate.wait()
        except asyncio.CancelledError:
            self.closeCancelled = True
            raise
        if self.closeError:
            raise RuntimeError("synthetic private body must not escape")
        self.closed = True


class TrackingTransport(httpx.AsyncBaseTransport):
    """无网络的单次传输，返回尚未读取、尚未关闭的异步响应流。"""

    def __init__(self, status=200, content=b'{"ok":true}', *, stream=None,
                 headers=None, error=None, slowClose=False, closeError=False):
        """提供可控 HTTP 状态、编码、底层超时及传输关闭故障。"""
        self.calls = 0
        self.closeCalls = 0
        self.closed = False
        self.status = status
        self.stream = stream or TrackingStream(content)
        self.headers = headers or {}
        self.error = error
        self.closeError = closeError
        self.started = asyncio.Event()
        self.closeStarted = asyncio.Event()
        self.closeGate = asyncio.Event()
        if not slowClose:
            self.closeGate.set()


    async def handle_async_request(self, request):
        """记录原请求，响应流在实际 aiter_bytes 时才会读取。"""
        self.calls += 1
        self.request = request
        self.started.set()
        if self.error:
            raise self.error
        return httpx.Response(self.status, headers=self.headers, stream=self.stream)


    async def aclose(self):
        """关闭传输和关闭响应流是独立事实，不用一个布尔量代替两者。"""
        self.closeCalls += 1
        self.closeStarted.set()
        await self.closeGate.wait()
        if self.closeError:
            raise RuntimeError("synthetic transport close failed")
        self.closed = True


class SelectionHarness:
    """使用真实 runtime 与 lease，测试调用方退出后仍在进行的资源记账。"""

    def __init__(self):
        """仅创建空 runtime，不启动编码器、线程工作或数据库查询。"""
        self.runtime = runtimeModule.MemoryRuntime()
        self.leases = []
        self.transports = []
        self.receipts = []
        self.tasks = []


    async def send(self, transport, **options):
        """模拟统一检索入口占位、登记、解除调用方引用和延期归还容量。"""
        lease = retrievalModule._RetrievalLease()
        retrievalModule._activeRetrievals += 1
        self.leases.append(lease)
        if transport is not None:
            self.transports.append(transport)
        receipt = options.pop("lifecycle", {})
        self.receipts.append(receipt)
        assert self.runtime.registerSelectorRetrieval(lease)
        args = dict(baseURL="https://selector.invalid/v1", apiKey="test-secret", timeoutSeconds=1,
                    lease=lease, owner=self.runtime, lifecycle=receipt)
        args.update(options)
        try:
            return await client.requestMemorySelection({"model": "test-model"}, transport=transport, **args)
        finally:
            # 调用方返回仅释放自己的部分，清理仍持有同次 lease。
            record = self.runtime._selectorRetrievals[lease]
            self.tasks.extend(task for task in (record.requestTask, record.cleanupTask) if task is not None)
            self.runtime.finishSelectorRetrieval(lease)
            lease.release()


    async def join(self):
        """显式等待所有 lease 完成，也等 runtime 的完成回调移除任务所有权。"""
        if self.tasks:
            done, pending = await asyncio.wait(self.tasks, timeout=2)
            assert not pending, "synthetic request or cleanup did not finish"
            await asyncio.gather(*done, return_exceptions=True)
        completions = [lease.completion for lease in self.leases]
        if completions:
            done, pending = await asyncio.wait(completions, timeout=2)
            assert not pending, "synthetic cleanup did not finish"
            await asyncio.gather(*done)
        await asyncio.sleep(0)
        assert retrievalModule._activeRetrievals == 0
        assert self.runtime._selectorRetrievals == {}


    async def close(self):
        """测试失败也打开所有合成闸门，显式收尾任务再关闭空 runtime。"""
        for transport in self.transports:
            transport.stream.readGate.set()
            transport.stream.closeGate.set()
            transport.closeGate.set()
        await self.join()
        await self.runtime.close()


@pytest.fixture
async def harness(monkeypatch):
    """隔离全局容量与故障日志，所有请求和清理任务必须在 teardown 结束。"""
    monkeypatch.setattr(retrievalModule, "_activeRetrievals", 0)
    monkeypatch.setattr(runtimeModule, "logSystemEvent", AsyncMock())
    instance = SelectionHarness()
    try:
        yield instance
    finally:
        await instance.close()




@pytest.mark.parametrize("protocol", ["responses", "messages"])
async def test_singleRequestClosesRealStreamAndTransport(harness, protocol):
    """协议路径及认证准确，只有一次请求，底层流和传输各关闭一次。"""
    transport = TrackingTransport()
    assert await harness.send(transport, protocol=protocol) == {"ok": True}
    await harness.join()
    assert transport.calls == transport.closeCalls == transport.stream.closeCalls == 1
    assert transport.closed and transport.stream.closed
    assert str(transport.request.url) == "https://selector.invalid/v1/" + protocol
    assert transport.request.headers["authorization"] == "Bearer test-secret"
    assert transport.request.headers["content-type"] == "application/json"
    assert json.loads(transport.request.content) == {"model": "test-model"}
    if protocol == "messages":
        assert transport.request.headers["anthropic-version"] == "2023-06-01"
    else:
        assert "anthropic-version" not in transport.request.headers
    assert harness.receipts[-1]["cleanupVerifiedAtDecision"] is True


@pytest.mark.parametrize("status", [302, 429, 502, 524])
async def test_httpFailureDoesNotReadRetryOrExposeBody(harness, status):
    """HTTP 故障不读取敏感正文，也不会跟随重定向或尝试重发。"""
    transport = TrackingTransport(status, b"test-secret private memory")
    with pytest.raises(client.MemorySelectionError, match="^selectorHTTP$"):
        await harness.send(transport)
    await harness.join()
    assert transport.calls == 1 and transport.closed and transport.stream.closed
    assert not transport.stream.readStarted.is_set()
    assert harness.receipts[-1]["requestFailure"] == "selectorHTTP"


@pytest.mark.parametrize("content", [b'{}{}', b'{"a":1,"a":2}', b'{"a":NaN}', b'\xff'])
async def test_badJsonClosesWithoutRetry(harness, content):
    """JSON 编码与重复字段错误只保留静态原因，原流仍须关闭。"""
    transport = TrackingTransport(content=content)
    with pytest.raises(client.MemorySelectionError, match="^selectorTransport$"):
        await harness.send(transport)
    await harness.join()
    assert transport.calls == 1 and transport.closed and transport.stream.closed


async def test_gzipUsesDecodedBytesForJson(harness):
    """保存原始流的包装不能绕开 HTTPX 解压，压缩响应仍可严格解析。"""
    content = json.dumps({"text": "可解压的选择结果"}, ensure_ascii=False).encode("utf-8")
    transport = TrackingTransport(content=gzip.compress(content), headers={"content-encoding": "gzip"})
    assert await harness.send(transport) == {"text": "可解压的选择结果"}
    await harness.join()
    assert transport.stream.closed and transport.stream.closeCalls == 1


async def test_decodedResponseByteBoundRejectsSmallCompressedPayload(harness, monkeypatch):
    """压缩体积很小也不能绕过解压后字节上限。"""
    content = json.dumps({"text": "a" * 5000}).encode("utf-8")
    compressed = gzip.compress(content)
    assert len(compressed) < 100
    monkeypatch.setattr(client, "LLM_MEMORY_SELECTOR_MAX_RESPONSE_BYTES", 100)
    transport = TrackingTransport(content=compressed, headers={"content-encoding": "gzip"})
    with pytest.raises(client.MemorySelectionError, match="^selectorResponseTooLarge$"):
        await harness.send(transport)
    await harness.join()
    assert transport.closed and transport.stream.closed


async def test_timeoutCancelsRequestAndClosesBeforeReleasingCapacity(harness):
    """控制器期限取消真实流读取，清理完成才归还容量。"""
    stream = TrackingStream(blocked=True)
    transport = TrackingTransport(stream=stream)
    with pytest.raises(client.MemorySelectionError, match="^selectorTimeout$"):
        await harness.send(transport, timeoutSeconds=.2)
    await harness.join()
    receipt = harness.receipts[-1]
    assert transport.calls == 1 and transport.closed and stream.closed
    assert stream.cancelCount == receipt["cancelRequests"] == 1
    assert receipt["requestDeadlineExpired"] is True
    assert receipt["decision"] == "selectorTimeout"


async def test_transportTimeoutHasDistinctReasonAndDoesNotBecomeControllerTimeout(harness):
    """库内部超时不能冒充受控期限；原静态原因保留且传输正常收尾。"""
    transport = TrackingTransport(error=httpx.ReadTimeout("synthetic private server error"))
    with pytest.raises(client.MemorySelectionError, match="^selectorTransportTimeout$"):
        await harness.send(transport)
    await harness.join()
    receipt = harness.receipts[-1]
    assert receipt["requestDeadlineExpired"] is False
    assert receipt["requestFailure"] == receipt["decision"] == "selectorTransportTimeout"
    assert transport.calls == 1 and transport.closed


async def test_httpFailureSurvivesCleanupDeadlineAndRetainsLease(harness):
    """HTTP 502 后关闭超时不覆盖原始失败；独立清理继续占同一次容量。"""
    stream = TrackingStream(slowClose=True)
    transport = TrackingTransport(502, stream=stream)
    with pytest.raises(client.MemorySelectionError, match="^selectorHTTP$"):
        await harness.send(transport, timeoutSeconds=.1)
    receipt, lease = harness.receipts[-1], harness.leases[-1]
    record = harness.runtime._selectorRetrievals[lease]
    assert receipt["requestFailure"] == receipt["decision"] == "selectorHTTP"
    assert receipt["cleanupVerifiedAtDecision"] is False and receipt["cleanupDone"] is False
    assert record.callerTask is None and not record.cleanupTask.done()
    assert receipt is record.receipt and not lease.completion.done()
    assert retrievalModule._activeRetrievals == 1
    assert stream.closeStarted.is_set() and not stream.closed and not transport.closed
    stream.closeGate.set()
    await harness.join()
    assert receipt["cleanupDone"] and stream.closed and transport.closed
    assert receipt["decision"] == "selectorHTTP"


async def test_successfulRequestWithSlowCloseFailsWithoutDiscardingCleanup(harness):
    """正文成功不能覆盖未完成的清理，也不能提前放开并发容量。"""
    stream = TrackingStream(slowClose=True)
    transport = TrackingTransport(stream=stream)
    with pytest.raises(client.MemorySelectionError, match="^selectorCleanupPending$"):
        await harness.send(transport, timeoutSeconds=.1)
    receipt = harness.receipts[-1]
    assert receipt["requestOutcome"] == "success"
    assert receipt["decision"] == "selectorCleanupPending"
    assert retrievalModule._activeRetrievals == 1
    stream.closeGate.set()
    await harness.join()
    assert receipt["responseClosed"] and receipt["transportClosed"]
    assert receipt["decision"] == "selectorCleanupPending"


@pytest.mark.parametrize("failingResource", ["response", "transport"])
async def test_cleanupFailureRetainsReceiptAndStillAttemptsOtherResources(harness, failingResource):
    """关闭失败以静态类型留痕；任务结束不会伪报资源成功关闭。"""
    stream = TrackingStream(closeError=failingResource == "response")
    transport = TrackingTransport(stream=stream, closeError=failingResource == "transport")
    with pytest.raises(client.MemorySelectionError, match="^selectorCleanupFailed$"):
        await harness.send(transport)
    await harness.join()
    receipt = harness.receipts[-1]
    assert receipt[failingResource + "CloseError"] == "RuntimeError"
    assert receipt[failingResource + "Closed"] is False
    assert receipt["cleanupDone"] is True and receipt["cleanupVerifiedAtDecision"] is False
    assert transport.closeCalls == stream.closeCalls == 1
    assert receipt["transportCloseStarted"] is True
    assert harness.runtime._selectorCleanupFailures == 1
    assert "synthetic private" not in str(receipt)


async def test_repeatedCallerCancellationDoesNotCancelCleanupOrDoubleCancelRequest(harness):
    """重复取消调用方只取消请求一次，独立关闭继续被 runtime 和 lease 持有。"""
    stream = TrackingStream(blocked=True, slowClose=True)
    transport = TrackingTransport(stream=stream)
    task = asyncio.create_task(harness.send(transport))
    await stream.readStarted.wait()
    task.cancel()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    task.cancel()
    await stream.closeStarted.wait()
    receipt, lease = harness.receipts[-1], harness.leases[-1]
    record = harness.runtime._selectorRetrievals[lease]
    assert receipt["decision"] == "externalCancelled" and receipt["externalCancelled"] is True
    assert receipt["cancelRequests"] == stream.cancelCount == 1
    assert record.callerTask is None and not record.cleanupTask.done()
    assert not stream.closeCancelled and retrievalModule._activeRetrievals == 1
    stream.closeGate.set()
    await harness.join()
    assert stream.closed and transport.closed and not stream.closeCancelled


async def test_lateSuccessCannotReviveTimeoutDecision(harness):
    """服务吞掉取消后晚到的合法 JSON 只供收尾，不能改写已经返回的超时。"""
    stream = TrackingStream(blocked=True, suppressCancellation=True)
    transport = TrackingTransport(stream=stream)
    with pytest.raises(client.MemorySelectionError, match="^selectorTimeout$"):
        await harness.send(transport, timeoutSeconds=.1)
    receipt = harness.receipts[-1]
    assert stream.readCancelled.is_set() and receipt["requestDone"] is False
    assert receipt["cleanupDone"] is False and retrievalModule._activeRetrievals == 1
    stream.readGate.set()
    await harness.join()
    assert receipt["requestOutcome"] == "success" and receipt["cleanupDone"] is True
    assert receipt["decision"] == "selectorTimeout" and receipt["cleanupVerifiedAtDecision"] is False
    assert stream.closed and transport.closed


@pytest.mark.parametrize("options", [
    {"baseURL": None}, {"apiKey": None}, {"baseURL": "http://selector.invalid"},
    {"baseURL": "https://user:secret@selector.invalid"}, {"baseURL": "https://selector.invalid?a=b"},
    {"timeoutSeconds": float("nan")}, {"timeoutSeconds": float("inf")},
    {"timeoutSeconds": True}, {"timeoutSeconds": 31}, {"timeoutSeconds": 0},
    {"protocol": "message"}, {"protocol": None},
])
async def test_invalidConfigurationDoesNotSend(harness, options):
    """缺配置、非法协议或时限在建立请求前拒绝。"""
    transport = TrackingTransport()
    with pytest.raises(client.MemorySelectionError):
        await harness.send(transport, **options)
    await harness.join()
    assert transport.calls == 0


async def test_requestAndResponseSizeCaps(harness, monkeypatch):
    """请求字节上限在发送前检查，响应超限仍关闭真实流。"""
    transport = TrackingTransport()
    monkeypatch.setattr(client, "LLM_MEMORY_SELECTOR_MAX_REQUEST_BYTES", 1)
    with pytest.raises(client.MemorySelectionError, match="selectorRequestTooLarge"):
        await harness.send(transport)
    assert transport.calls == 0
    monkeypatch.setattr(client, "LLM_MEMORY_SELECTOR_MAX_REQUEST_BYTES", 100)
    monkeypatch.setattr(client, "LLM_MEMORY_SELECTOR_MAX_RESPONSE_BYTES", 1)
    with pytest.raises(client.MemorySelectionError, match="selectorResponseTooLarge"):
        await harness.send(transport)
    await harness.join()
    assert transport.calls == 1 and transport.closed and transport.stream.closed


async def test_realClientOptionsDisableRetriesRedirectsAndEnvironment(harness, monkeypatch):
    """拦截构造参数，仍通过真实 AsyncClient 检验不采用环境代理或重试。"""
    seen = {}
    transport = TrackingTransport()
    realClient = httpx.AsyncClient
    harness.transports.append(transport)

    def makeTransport(**options):
        """截获网络传输参数并返回 mock，测试中不建立真实连接。"""
        seen["transport"] = options
        return transport

    def makeClient(**options):
        """保留真实 HTTPX 读取及清理行为，仅记录构造选项。"""
        seen["client"] = options
        return realClient(**options)

    monkeypatch.setattr(client.httpx, "AsyncHTTPTransport", makeTransport)
    monkeypatch.setattr(client.httpx, "AsyncClient", makeClient)
    await harness.send(None, proxy="http://127.0.0.1:7897")
    await harness.join()
    assert seen["transport"] == {"proxy": "http://127.0.0.1:7897", "retries": 0}
    assert seen["client"]["trust_env"] is False and seen["client"]["follow_redirects"] is False
    assert transport.calls == 1 and transport.closed and transport.stream.closed
