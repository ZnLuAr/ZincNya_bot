"""
tests/utils/llm/memory/test_retrieval.py

测试 utils/llm/memory/retrieval.py 与 database.py 的 legacy/hybrid 检索和呈现逻辑。

验证：
    ① legacy 的 scope/priority 兼容排序与完整候选读取
    ② hybrid 的 lexical/semantic/RRF 准入、pinned 独立预算与 fail-closed
    ③ buildMemoryContextBlock 的低信任边界、ID/source 与字符预算
    ④ 数据库快照复核、去重、超时和异常降级
"""

import asyncio
import json
import threading
import re
from copy import deepcopy

import pytest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

from utils.llm.memory.database import (
    buildMemoryContextBlock,
    retrieveMemories,
    selectLegacyMemoryCandidates,
)
from utils.llm.memory.retrieval import (
    buildQueryTexts,
    deduplicateMemoryCandidates,
    loadCalibratedThresholds,
    renderMemoryContext,
    retrieveMemoryContext,
    selectContextualCandidates,
)
from utils.llm.memory import retrieval as retrievalModule
from utils.llm.memory.runtime import MemoryRuntime
from utils.llm.memory.types import MemoryQuery, MemoryTurn
from utils.llm.client import memorySelection as selectionClient
from utils.llm.client.memorySelection import requestMemorySelection as realSelectionRequest


# ============================================================================
# buildMemoryContextBlock() 呈现层测试
# ============================================================================


@pytest.fixture
async def llmHarness(monkeypatch):
    """使用真实协议/渲染和合成 DB/runtime，只在传输边界注入 fake。"""
    rows = [_candidate(1, mode="pinned"), _candidate(2, content="白框孔距 75×75"),
            _candidate(3, content="黑框孔距 100×100")]
    harness = SimpleNamespace(rows=rows, primary=[2], optional=[3], requests=[], mutate=None,
                              error=None, wait=False, entered=asyncio.Event(), closed=0)
    harness.runtime = MemoryRuntime()
    harness.runtime.scoreSemantic = AsyncMock(return_value=[{
        "base": {2: .2, 3: .1}, "enhanced": {3: 1},
    }])
    harness.shutdown = asyncio.Event()

    async def readCandidates(**scopes):
        """传入 scope 记录于本地，模拟候选读取时的独立快照。"""
        harness.scopes = scopes
        return deepcopy(harness.rows)

    async def readSnapshots(ids):
        """模拟远程等待后数据库的当前状态。"""
        selected = set(ids)
        return deepcopy([row for row in harness.rows if row["id"] in selected])

    async def selectResponse(body, **options):
        """将测试指定的原 ID 转成当次匿名句柄，构造合法服务端响应。"""
        harness.requests.append((deepcopy(body), options))
        harness.entered.set()
        try:
            if harness.mutate:
                harness.mutate()
            if harness.error:
                raise harness.error
            if harness.wait:
                await asyncio.Event().wait()
            protocol = options.get("protocol", "responses")
            payload = json.loads(body["input"] if protocol == "responses" else body["messages"][0]["content"])
            instructions = body["instructions"] if protocol == "responses" else body["system"]
            byContent = {row["content"]: row["handle"] for row in payload["candidates"]}
            mapping = {row["id"]: byContent.get(row["content"]) for row in rows}
            value = {
                "primaryOrder": [mapping[index] for index in harness.primary],
                "optionalOrder": [mapping[index] for index in harness.optional],
                "instructionMarker": re.search(r'"([a-f0-9]{12})"', instructions)[1],
            }
            if protocol == "messages":
                return {"type": "message", "role": "assistant", "model": body["model"], "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "```json\n" + json.dumps(value) + "\n```"}],
                    "usage": {"input_tokens": 5, "cache_creation_input_tokens": 10,
                              "cache_read_input_tokens": 20, "output_tokens": 3}}
            return {
                "model": body["model"], "status": "completed",
                "usage": {"input_tokens": 90, "output_tokens": 10, "total_tokens": 100},
                "output": [{"type": "message", "role": "assistant", "content": [
                    {"type": "output_text", "text": json.dumps(value)},
                ]}],
            }
        finally:
            harness.closed += 1

    harness.dbRead = AsyncMock(side_effect=readCandidates)
    harness.dbCheck = AsyncMock(side_effect=readSnapshots)
    monkeypatch.setattr(retrievalModule, "getMemoryCandidates", harness.dbRead)
    monkeypatch.setattr(retrievalModule, "getMemorySnapshots", harness.dbCheck)
    monkeypatch.setattr(retrievalModule, "scoreLexicalCandidates", lambda *args: {2: 1, 3: 0})
    monkeypatch.setattr(retrievalModule, "getStateManager", lambda: SimpleNamespace(
        getMemoryRuntime=lambda: harness.runtime, getShutdownEvent=lambda: harness.shutdown))
    monkeypatch.setattr(retrievalModule, "loadCalibratedThresholds", lambda: ({name: None for name in (
        "semanticCurrent", "semanticAssisted", "lexical")}, "calibrationUnconfigured"))
    monkeypatch.setattr(retrievalModule, "LLM_MEMORY_SELECTOR_BASE_URL", "https://selector.invalid/v1")
    monkeypatch.setattr(retrievalModule, "LLM_MEMORY_SELECTOR_API_KEY", "test-secret")
    monkeypatch.setattr(selectionClient, "requestMemorySelection", selectResponse)
    harness.response = selectResponse
    yield harness
    await harness.runtime.close()


def _useRealLLMTransport(monkeypatch, harness, *, closeGate=None):
    """时序测试使用正式控制器与模拟HTTP流，不能用无期限fake自证取消。"""
    import httpx

    class ResponseStream(httpx.AsyncByteStream):
        """完整正文立即可读，底层关闭可单独阻塞以验证延期容量。"""

        def __init__(self, content):
            """保存本次合成响应，不预先消费或关闭流。"""
            self.content = content

        async def __aiter__(self):
            """通过正式HTTPX读取路径提供响应正文。"""
            yield self.content

        async def aclose(self):
            """真实关闭结束后才增加计数，不能用控制器状态自证释放。"""
            if closeGate is not None:
                await closeGate.wait()
            harness.streamClosed = getattr(harness, "streamClosed", 0) + 1

    async def request(body, **options):
        """只替换HTTP边界；正式client负责任务、期限和资源关闭。"""
        async def respond(wireRequest):
            """将当次匿名选择转为未提前消费的响应字节流。"""
            value = await harness.response(json.loads(wireRequest.content), **options)
            return httpx.Response(200, stream=ResponseStream(json.dumps(value).encode("utf-8")))

        return await realSelectionRequest(body, **options, transport=httpx.MockTransport(respond))

    monkeypatch.setattr(selectionClient, "requestMemorySelection", request)


async def _retrieveWithLLM(**overrides):
    """显式开启测试请求的 llm 分支，生产默认配置不变。"""
    params = dict(chatID="chat-test", userID="user-test", sessionID="session-test",
                  query=MemoryQuery(turns=(MemoryTurn(currentText="白框孔距多少？"),)),
                  llmConfig={"memoryRetrievalMode": "hybrid", "memoryHybridSelector": "llm"})
    params.update(overrides)
    return await retrieveMemoryContext(**params)


async def test_llmIntegrationUsesPrimaryOriginalAndIndependentCalibration(llmHarness):
    """未审阈值不妨碍独立候选；只恢复 primary 原文并保留 pinned。"""
    result = await _retrieveWithLLM()
    assert [row["id"] for row in result.items] == [1, 2]
    assert "白框孔距 75×75" in result.contextBlock
    assert "黑框孔距" not in result.contextBlock
    assert result.diagnostics["selectorOptionalIgnored"] == 1
    assert result.diagnostics["calibrationPolicy"] == "independentLlmSelector"
    assert llmHarness.scopes == {"chatID": "chat-test", "userID": "user-test", "sessionID": "session-test"}
    assert len(llmHarness.requests) == 1
    assert llmHarness.runtime.scoreSemantic.await_count == 1
    assert retrievalModule._activeRetrievals == 0


async def test_llmPoolUsesLowBaseAndAssistedRatherThanEnhanced(llmHarness):
    """低 base 仍可入 top32；增强表示不能把另一个 ID 偷换进候选池。"""
    llmHarness.rows = [_candidate(index) for index in range(1, 35)]
    llmHarness.runtime.scoreSemantic.return_value = [
        {"base": {index: -.5 for index in range(1, 34)}, "enhanced": {34: 99}},
        {"base": {33: -.9}, "enhanced": {34: 99}},
    ]
    llmHarness.primary, llmHarness.optional = [], []
    result = await _retrieveWithLLM(query=MemoryQuery(turns=(MemoryTurn(currentText="孔距", replyText="白框"),)))
    payload = json.loads(llmHarness.requests[0][0]["input"])
    assert {row["content"] for row in payload["candidates"]} == {f"事实 {index}" for index in range(1, 34)}
    assert result.items == []
    assert len(llmHarness.runtime.scoreSemantic.call_args.args[0]) == 2


async def test_llmProtocolFailureKeepsPinnedWithStaticReason(llmHarness):
    """模型身份与 ID 协议失败会丢弃 contextual，并留下可定位的静态原因。"""
    original = selectionClient.requestMemorySelection

    async def wrongModel(body, **options):
        """模拟中转静默返回另一模型。"""
        response = await original(body, **options)
        response["model"] = "different-model"
        return response

    with patch.object(selectionClient, "requestMemorySelection", wrongModel):
        result = await _retrieveWithLLM()
    assert [row["id"] for row in result.items] == [1]
    assert result.diagnostics["selectorFailure"] == "response_model_mismatch"


async def test_llmUnconfiguredTransportMakesNoNetworkAndRetainsPinned(llmHarness, monkeypatch):
    """独立凭据未设时恢复真实传输入口，仍不得创建 HTTP 客户端。"""
    monkeypatch.setattr(selectionClient, "requestMemorySelection", realSelectionRequest)
    monkeypatch.setattr(retrievalModule, "LLM_MEMORY_SELECTOR_API_KEY", None)
    with patch.object(selectionClient.httpx, "AsyncClient", side_effect=AssertionError("network prohibited")) as client:
        result = await _retrieveWithLLM()
        client.assert_not_called()
    assert [row["id"] for row in result.items] == [1]
    assert result.diagnostics["selectorFailure"] == "selectorUnconfigured"


async def test_llmInvalidBackendAndDeadlineDoNotSend(llmHarness):
    """无效后端与超过授权的时限均在读取候选之前拒绝。"""
    for override in ({"memoryHybridSelector": "typo"}, {"memorySelectorTimeoutSeconds": 31}):
        await _retrieveWithLLM(llmConfig={"memoryRetrievalMode": "hybrid", "memoryHybridSelector": "llm", **override})
    assert not llmHarness.requests and llmHarness.dbRead.await_count == 0
    assert retrievalModule._activeRetrievals == 0


async def test_llmLocalTimeoutStillFinalizesPinned(llmHarness, monkeypatch):
    """本地评分耗尽预算时不借远程时限延长评分，常驻仍可单独复核。"""
    async def blockedSemantic(*args, **kwargs):
        """模拟尚未返回分数的运行时。"""
        await asyncio.Event().wait()

    llmHarness.runtime.scoreSemantic.side_effect = blockedSemantic
    monkeypatch.setattr(retrievalModule, "scoreLexicalCandidates", lambda *args: {})
    monkeypatch.setattr(retrievalModule, "LLM_MEMORY_RETRIEVAL_TIMEOUT_SECONDS", .02)
    result = await _retrieveWithLLM()
    assert [row["id"] for row in result.items] == [1]
    assert "semanticTimeout" in result.diagnostics["degradedReasons"]
    assert not llmHarness.requests and retrievalModule._activeRetrievals == 0


@pytest.mark.parametrize("config", [
    {}, {"memoryRetrievalMode": "legacy", "memoryHybridSelector": "llm"},
    {"memoryRetrievalMode": "hybrid"},
])
async def test_defaultOrLocalNeverUsesSelectorNetwork(llmHarness, config):
    """只开启 hybrid 仍走 null 校准门控，本地/legacy 均不发选择请求。"""
    await _retrieveWithLLM(llmConfig=config)
    assert not llmHarness.requests
    assert llmHarness.runtime.scoreSemantic.await_count == 0


async def test_llmConfigurationSnapshotSurvivesConcurrentMutation(llmHarness):
    """等待数据库期间切换配置不改变本次选择后端与模型。"""
    settings = {"memoryRetrievalMode": "hybrid", "memoryHybridSelector": "llm"}
    originalRead = llmHarness.dbRead.side_effect

    async def readAndChange(**scopes):
        """在第一个 await 内模拟运维修改共享配置。"""
        settings.update(memoryHybridSelector="local", memorySelectorModel="unapproved-other-model",
                        memorySelectorTimeoutSeconds=100)
        return await originalRead(**scopes)

    llmHarness.dbRead.side_effect = readAndChange
    result = await _retrieveWithLLM(llmConfig=settings)
    assert result.diagnostics["selectorStatus"] == "ready"
    assert llmHarness.requests[0][0]["model"] == "gpt-5.6-terra"
    assert 29 < llmHarness.requests[0][1]["timeoutSeconds"] <= 30


@pytest.mark.parametrize("preparationSeconds", [.025, .2])
async def test_selectorPreparationConsumesSameDeadline(llmHarness, monkeypatch, preparationSeconds):
    """同步准备从网络预算扣除；准备已超时不能发送请求或返回情境记忆。"""
    originalClock = retrievalModule.time.monotonic
    offset = [0.0]
    monkeypatch.setattr(retrievalModule, "time", SimpleNamespace(monotonic=lambda: originalClock() + offset[0]))
    originalBuilder = retrievalModule.buildSelectorRequest

    def delayedBuilder(*args, **kwargs):
        """推进局部时钟模拟准备成本，不阻塞测试线程或影响HTTP控制器时钟。"""
        result = originalBuilder(*args, **kwargs)
        offset[0] += preparationSeconds
        return result

    monkeypatch.setattr(retrievalModule, "buildSelectorRequest", delayedBuilder)
    result = await _retrieveWithLLM(llmConfig={"memoryRetrievalMode": "hybrid", "memoryHybridSelector": "llm",
        "memorySelectorTimeoutSeconds": .1})
    if preparationSeconds < .1:
        assert [row["id"] for row in result.items] == [1, 2]
        assert 0 < llmHarness.requests[0][1]["timeoutSeconds"] <= .1 - preparationSeconds
    else:
        assert not llmHarness.requests
        assert [row["id"] for row in result.items] == [1]
        assert result.diagnostics["selectorFailure"] == "selectorTimeout"


@pytest.mark.parametrize("change", ["scope", "content", "delete", "disable"])
async def test_llmSelectionRechecksConcurrentMemoryChanges(llmHarness, change):
    """模型返回的 ID 不能绕过作用域、删除与正文变更复核。"""
    def mutate():
        """只改当前 DB 状态，保留已经发送的候选快照。"""
        if change == "delete":
            llmHarness.rows = [row for row in llmHarness.rows if row["id"] != 2]
        else:
            field, value = {"scope": ("scope_id", "different-user"), "content": ("content", "已改正文"),
                            "disable": ("enabled", False)}[change]
            llmHarness.rows = deepcopy(llmHarness.rows)
            llmHarness.rows[1][field] = value

    llmHarness.mutate = mutate
    result = await _retrieveWithLLM()
    assert [row["id"] for row in result.items] == [1]
    assert "id=2" not in result.contextBlock


async def test_llmFailureRetainsOnlyRevalidatedPinned(llmHarness):
    """选择故障不回退 legacy；pinned 也可能因变更被剔除。"""
    llmHarness.error = RuntimeError("sensitive response body")
    result = await _retrieveWithLLM()
    assert [row["id"] for row in result.items] == [1]
    assert "sensitive" not in str(result.diagnostics)
    llmHarness.mutate = lambda: llmHarness.rows[0].update(enabled=False)
    result = await _retrieveWithLLM()
    assert result.items == [] and result.contextBlock == ""


async def test_llmTimeoutCancelsContextualAndKeepsPinned(llmHarness, monkeypatch):
    """选择 deadline 独立于两秒本地预算，超时不使用迟到结果。"""
    llmHarness.wait = True
    _useRealLLMTransport(monkeypatch, llmHarness)
    result = await _retrieveWithLLM(llmConfig={
        "memoryRetrievalMode": "hybrid", "memoryHybridSelector": "llm", "memorySelectorTimeoutSeconds": .08,
    })
    assert [row["id"] for row in result.items] == [1]
    assert result.diagnostics["degradedReason"] == "selectorTimeout"
    for _ in range(20):
        if retrievalModule._activeRetrievals == 0:
            break
        await asyncio.sleep(.005)
    assert llmHarness.closed == 1 and retrievalModule._activeRetrievals == 0


async def test_llmCancellationAndCapacityAreReleased(llmHarness, monkeypatch):
    """四个慢选择占满容量，第五个不排队；取消传播且每个名额归还。"""
    llmHarness.wait = True
    _useRealLLMTransport(monkeypatch, llmHarness)
    tasks = [asyncio.create_task(_retrieveWithLLM()) for _ in range(4)]
    try:
        for _ in range(100):
            if len(llmHarness.requests) == 4:
                break
            await asyncio.sleep(.005)
        assert len(llmHarness.requests) == 4
        result = await _retrieveWithLLM()
        assert result.items == [] and result.diagnostics["degradedReason"] == "retrievalCapacity"
    finally:
        for task in tasks:
            task.cancel()
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(value, asyncio.CancelledError) for value in outcomes)
    for _ in range(20):
        if retrievalModule._activeRetrievals == 0:
            break
        await asyncio.sleep(.005)
    assert llmHarness.closed == 4 and retrievalModule._activeRetrievals == 0
    assert llmHarness.dbCheck.await_count == 0


async def test_messagesRetrievalUsesSameBudgetAndFinalValidation(llmHarness, monkeypatch):
    """Messages通过真实HTTP控制器和最终复核，optional仍不注入。"""
    _useRealLLMTransport(monkeypatch, llmHarness)
    result = await _retrieveWithLLM(llmConfig={"memoryRetrievalMode": "hybrid", "memoryHybridSelector": "llm",
        "memorySelectorProtocol": "messages", "memorySelectorModel": "test-messages"})
    assert [row["id"] for row in result.items] == [1, 2]
    assert result.diagnostics["selectorProtocol"] == "messages"
    assert result.diagnostics["selectorEffortApplied"] is None
    assert result.diagnostics["selectorTextFormat"] == "fenced_json"
    assert result.diagnostics["selectorUsage"]["input_tokens"] == 35
    assert result.diagnostics["selectorLifecycle"]["cleanupVerifiedAtDecision"]
    assert llmHarness.dbCheck.await_count == 1


async def test_slowCleanupKeepsFourSlotsAndCannotReviveSelection(llmHarness, monkeypatch):
    """调用方已返回也不释放慢清理名额；后台完成不改诊断或补注入记忆。"""
    closeGate = asyncio.Event()
    _useRealLLMTransport(monkeypatch, llmHarness, closeGate=closeGate)
    config = {"memoryRetrievalMode": "hybrid", "memoryHybridSelector": "llm",
              "memorySelectorTimeoutSeconds": .1}
    tasks = [asyncio.create_task(_retrieveWithLLM(llmConfig=config)) for _ in range(4)]
    completions = []
    try:
        results = await asyncio.wait_for(asyncio.gather(*tasks), 2)
        assert len(llmHarness.requests) == 4
        assert all([row["id"] for row in result.items] == [1] for result in results)
        assert all(result.diagnostics["degradedReason"] == "selectorFailed" for result in results)
        assert all(result.diagnostics["selectorFailure"] == "selectorCleanupPending" for result in results)
        assert retrievalModule._activeRetrievals == 4
        assert getattr(llmHarness, "streamClosed", 0) == 0
        frozen = deepcopy([result.diagnostics for result in results])
        readCount = llmHarness.dbRead.await_count
        blocked = await _retrieveWithLLM()
        assert blocked.diagnostics["degradedReason"] == "retrievalCapacity"
        assert llmHarness.dbRead.await_count == readCount and len(llmHarness.requests) == 4
        completions = [lease.completion for lease in llmHarness.runtime._selectorRetrievals]
    finally:
        # 断言失败也放开关闭闸门，避免测试自身留下清理任务。
        closeGate.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        completions.extend(lease.completion for lease in llmHarness.runtime._selectorRetrievals)
        if completions:
            await asyncio.wait_for(asyncio.gather(*completions), 2)
        await asyncio.sleep(0)
    assert llmHarness.streamClosed == 4 and retrievalModule._activeRetrievals == 0
    assert [result.diagnostics for result in results] == frozen
    assert all([row["id"] for row in result.items] == [1] for result in results)
    recovered = await _retrieveWithLLM()
    assert [row["id"] for row in recovered.items] == [1, 2]
    assert len(llmHarness.requests) == 5


async def test_shutdownGateBlocksReadsAndSendAfterCandidates(llmHarness):
    """关停入口零读取；候选等待期间开始关停也不能继续发送或最终复核。"""
    llmHarness.shutdown.set()
    assert (await _retrieveWithLLM()).diagnostics["degradedReason"] == "retrievalStopping"
    assert llmHarness.dbRead.await_count == 0
    llmHarness.shutdown.clear()
    original = llmHarness.dbRead.side_effect

    async def readDuringShutdown(**scopes):
        """候选返回前设置关停事件，模拟生命周期窗口。"""
        result = await original(**scopes)
        llmHarness.shutdown.set()
        return result

    llmHarness.dbRead.side_effect = readDuringShutdown
    result = await _retrieveWithLLM()
    assert not result.items and not llmHarness.requests
    assert llmHarness.dbCheck.await_count == 0


async def test_shutdownCancelsRealSelectionAndCompletesLease(llmHarness, monkeypatch):
    """资源回调取消完整检索并等cleanup，不能进入最终数据库复核。"""
    _useRealLLMTransport(monkeypatch, llmHarness)
    llmHarness.wait = True
    task = asyncio.create_task(_retrieveWithLLM())
    await asyncio.wait_for(llmHarness.entered.wait(), 1)
    await llmHarness.runtime.closeSelectors()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert retrievalModule._activeRetrievals == 0
    assert llmHarness.runtime.getStatus()["selectorRetrievals"] == 0
    assert llmHarness.dbCheck.await_count == 0


async def test_llmDbFailuresReturnNoMemory(llmHarness):
    """候选读取或最终复核不可用时不声称能保留 pinned。"""
    llmHarness.dbRead.side_effect = RuntimeError("db unavailable")
    assert not (await _retrieveWithLLM()).items
    assert not llmHarness.requests
    llmHarness.dbRead.side_effect = None
    llmHarness.dbRead.return_value = deepcopy(llmHarness.rows)
    llmHarness.dbCheck.side_effect = RuntimeError("db unavailable")
    assert not (await _retrieveWithLLM()).items


async def test_llmHistoryUsesSameWindowAndAllowlist(llmHarness):
    """历史保留指代所需近期消息，不带 reaction、未来、过期或内部 ID。"""
    now = datetime.now()
    query = MemoryQuery(turns=(MemoryTurn(currentText="这个孔距呢", replyText="白框"),), history=(
        {"content": "过期", "timestamp": now - timedelta(hours=1)},
        {"content": "表情", "direction": "reaction", "timestamp": now},
        {"content": "未来", "timestamp": now + timedelta(days=1)},
        {"content": "白框", "timestamp": now},
        {"content": "先前黑框，后来改问白框", "sender": "甲", "timestamp": now, "chat_id": "hidden"},
    ))
    await _retrieveWithLLM(query=query)
    payload = json.loads(llmHarness.requests[0][0]["input"])
    assert [row["content"] for row in payload["query"]["history"]] == ["先前黑框,后来改问白框"]
    assert "hidden" not in json.dumps(payload)
    assert payload["query"]["turns"][0]["replyText"] == "白框"
    assert "queryNow" in payload


async def test_llmEmptyQueryNeverRequestsNetwork(llmHarness):
    """无查询时只有常驻条目，不诱导模型强行选择。"""
    result = await _retrieveWithLLM(query=MemoryQuery())
    assert [row["id"] for row in result.items] == [1]
    assert not llmHarness.requests


async def test_llmRenderingDropsWholeItemsAndMatchesBlock(llmHarness):
    """多条长记忆预算不足时整条丢弃，items 与最终块一致。"""
    llmHarness.rows = [_candidate(index, content=f"记忆{index}：" + "正文" * 200) for index in range(2, 6)]
    llmHarness.primary, llmHarness.optional = [2, 3, 4, 5], []
    # fake 响应映射须使用同一组内容；此处用协议边界返回实际句柄。
    async def allPrimary(body, **options):
        """按匿名池原样全选，交由真实渲染器裁预算。"""
        payload = json.loads(body["input"])
        marker = re.search(r'"([a-f0-9]{12})"', body["instructions"])[1]
        value = {"primaryOrder": [row["handle"] for row in payload["candidates"]],
                 "optionalOrder": [], "instructionMarker": marker}
        return {"status": "completed", "model": body["model"],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                "output": [{"type": "message", "role": "assistant", "content": [
                    {"type": "output_text", "text": json.dumps(value)},
                ]}]}

    llmHarness.runtime.scoreSemantic.return_value = [{"base": {index: .5 for index in range(2, 6)}, "enhanced": {}}]
    with patch.object(selectionClient, "requestMemorySelection", allPrimary):
        result = await _retrieveWithLLM()
    assert 0 < len(result.items) < 4
    assert len(result.contextBlock) <= 1500
    assert {int(value) for value in re.findall(r"id=(\d+)", result.contextBlock)} == {row["id"] for row in result.items}


async def test_leaseWaitsForRequestAndAllDeferredTasks(monkeypatch):
    """后台任务先结束不能释放仍在远程等待的名额，多任务也必须全部结束。"""
    monkeypatch.setattr(retrievalModule, "_activeRetrievals", 1)
    lease = retrievalModule._RetrievalLease()
    first, second = asyncio.Future(), asyncio.Future()
    lease.deferUntil(first)
    lease.deferUntil(second)
    first.set_result(None)
    await asyncio.sleep(0)
    assert retrievalModule._activeRetrievals == 1
    lease.release()
    assert retrievalModule._activeRetrievals == 1
    second.set_result(None)
    await asyncio.sleep(0)
    assert retrievalModule._activeRetrievals == 0

def test_build_memory_context_block_output_format():
    """① 输出含 w=（不含 p=）、② 含 id=/src=、③ 块头有门控措辞"""
    memories = [
        {
            "id": 42,
            "scope_type": "global",
            "scope_id": "global",
            "content": "用户偏好简体中文",
            "tags": ["偏好"],
            "priority": 10,
            "source": "manual",
        },
        {
            "id": 57,
            "scope_type": "chat",
            "scope_id": "123456",
            "content": "这个群主要讨论编程",
            "tags": [],
            "priority": 0,
            "source": "inferred",
        },
    ]

    result = buildMemoryContextBlock(memories)

    # ① 不含 p=，含 w=
    assert "p=" not in result
    assert "w=10" in result
    assert "w=0" in result

    # ② 含 id= / src=
    assert "id=42" in result
    assert "id=57" in result
    assert "src=manual" in result
    assert "src=inferred" in result

    # ③ 块头含相关性门控关键词
    assert "仅在与当前对话直接相关时才引用" in result
    assert "不要为了提及而提及" in result
    assert "w= 是内部召回权重" in result


def test_build_memory_context_block_empty():
    """空列表返回空字符串"""
    assert buildMemoryContextBlock([]) == ""


# ============================================================================
# retrieveMemories() 检索逻辑测试
# ============================================================================

@pytest.mark.asyncio
async def test_retrieve_memories_same_scope_overflow():
    """④ 同 scope 溢出判别：global 有 4 条 [p3, p3, p3, p2]，
    perScopeLimit=20 全量入池，p2 入选（旧逻辑 limit=3 会砍掉 p2）"""
    with patch("utils.llm.memory.database.getMemories", new_callable=AsyncMock) as mock_get:
        # global scope 返 4 条，3×p3 + 1×p2
        mock_get.return_value = [
            {"id": 1, "priority": 3, "scope_type": "global", "scope_id": "global", "updated_at": datetime(2024, 1, 1)},
            {"id": 2, "priority": 3, "scope_type": "global", "scope_id": "global", "updated_at": datetime(2024, 1, 2)},
            {"id": 3, "priority": 3, "scope_type": "global", "scope_id": "global", "updated_at": datetime(2024, 1, 3)},
            {"id": 4, "priority": 2, "scope_type": "global", "scope_id": "global", "updated_at": datetime(2024, 1, 4)},
        ]

        result = await retrieveMemories(totalLimit=10)

        # 新逻辑：4 条全入池，p2 也入选
        assert len(result) == 4
        ids = [m["id"] for m in result]
        assert 4 in ids  # p2 的那条


@pytest.mark.asyncio
async def test_retrieve_memories_scope_rank_tiebreaker():
    """⑤ scope 专属度兜底：同 priority=2 时，chat 排在 global 之前"""
    with patch("utils.llm.memory.database.getMemories", new_callable=AsyncMock) as mock_get:
        async def _mock_get(scopeType, scopeID, enabledOnly, limit):
            if scopeType == "global":
                return [{"id": 10, "priority": 2, "scope_type": "global", "scope_id": "global", "updated_at": datetime(2024, 1, 1)}]
            if scopeType == "chat":
                return [{"id": 20, "priority": 2, "scope_type": "chat", "scope_id": "123", "updated_at": datetime(2024, 1, 1)}]
            return []

        mock_get.side_effect = _mock_get

        result = await retrieveMemories(chatID="123", totalLimit=10)

        # 同 p2，chat(rank=1) > global(rank=0)，chat 条目排前
        assert len(result) == 2
        assert result[0]["id"] == 20  # chat
        assert result[1]["id"] == 10  # global


@pytest.mark.asyncio
async def test_retrieve_memories_exception_fallback():
    """⑥ 异常路径降级：getMemories 抛异常，返回 []（不冒泡）"""
    with patch("utils.llm.memory.database.getMemories", new_callable=AsyncMock) as mock_get:
        with patch("utils.llm.memory.database.logSystemEvent", new_callable=AsyncMock):
            mock_get.side_effect = RuntimeError("DB explosion")

            result = await retrieveMemories(totalLimit=10)

            # 降级返空，不抛异常
            assert result == []


@pytest.mark.asyncio
async def test_retrieve_memories_dirty_data_does_not_crash_sort():
    """⑥ 脏数据兜底：updated_at=None 的记忆混在正常记忆中，sort 不抛 TypeError"""
    with patch("utils.llm.memory.database.getMemories", new_callable=AsyncMock) as mock_get:
        # 1 条脏数据（updated_at=None）+ 1 条正常
        mock_get.return_value = [
            {"id": 1, "priority": 1, "scope_type": "global", "scope_id": "global", "updated_at": None},
            {"id": 2, "priority": 1, "scope_type": "global", "scope_id": "global", "updated_at": datetime(2024, 1, 1)},
        ]

        result = await retrieveMemories(totalLimit=10)

        # 不抛异常，正常返回 2 条
        assert len(result) == 2


def test_select_legacy_candidates_reproduces_per_scope_limit():
    """逐 scope 截断由纯函数复现，供完整候选读取路径复用。"""
    memories = [
        {
            "id": 1,
            "scope_type": "global",
            "scope_id": "global",
            "priority": 3,
            "updated_at": datetime(2026, 1, 1),
        },
        {
            "id": 2,
            "scope_type": "global",
            "scope_id": "global",
            "priority": 1,
            "updated_at": datetime(2026, 1, 2),
        },
        {
            "id": 3,
            "scope_type": "chat",
            "scope_id": "123",
            "priority": 2,
            "updated_at": datetime(2026, 1, 1),
        },
        {
            "id": 4,
            "scope_type": "chat",
            "scope_id": "123",
            "priority": 0,
            "updated_at": datetime(2026, 1, 2),
        },
    ]

    selected = selectLegacyMemoryCandidates(
        memories,
        perScopeLimit=1,
        totalLimit=10,
    )

    assert [memory["id"] for memory in selected] == [1, 3]


# ============================================================================
# Hybrid 检索纯函数与统一入口
# ============================================================================

def _candidate(memoryID, *, content=None, mode="contextual", priority=0):
    return {
        "id": memoryID,
        "scope_type": "global",
        "scope_id": "global",
        "content": content or f"事实 {memoryID}",
        "tags": [],
        "retrievalHint": "不应进入最终上下文",
        "enabled": True,
        "priority": priority,
        "source": "inferred",
        "mode": mode,
        "created_at": datetime(2026, 1, 1),
        "updated_at": datetime(2026, 1, 1),
    }


def _writeCalibration(tmpPath, *, datasetHash="a" * 64, lexicalThreshold=1.0):
    calibration = {
        "schemaVersion": 2,
        "status": "approved",
        "modelRevision": "revision",
        "encodingVersion": "encoding",
        "lexicalVersion": "memory-bm25-v1",
        "semanticAdmissionRepresentation": "base",
        "semanticRankingRepresentation": "enhanced",
        "datasetSha256": datasetHash,
        "thresholds": {
            "semanticCurrent": 0.8,
            "semanticAssisted": None,
            "lexical": lexicalThreshold,
        },
    }
    path = tmpPath / "calibration.json"
    path.write_text(
        json.dumps(calibration, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def test_loadCalibratedThresholdsAcceptsBoundFiniteValues(tmp_path):
    path = _writeCalibration(tmp_path)

    with patch(
        "utils.llm.memory.retrieval.loadModelManifest",
        return_value={"revision": "revision", "encodingVersion": "encoding"},
    ):
        thresholds, reason = loadCalibratedThresholds(path)

    assert reason is None
    assert thresholds == {
        "semanticCurrent": 0.8,
        "semanticAssisted": None,
        "lexical": 1.0,
    }


def test_loadCalibratedThresholdsRejectsOldSingleRepresentationSchema(tmp_path):
    path = _writeCalibration(tmp_path)
    calibration = json.loads(path.read_text(encoding="utf-8"))
    calibration["schemaVersion"] = 1
    path.write_text(json.dumps(calibration), encoding="utf-8")

    with patch(
        "utils.llm.memory.retrieval.loadModelManifest",
        return_value={"revision": "revision", "encodingVersion": "encoding"},
    ):
        thresholds, reason = loadCalibratedThresholds(path)

    assert reason == "calibrationSchemaMismatch"
    assert all(value is None for value in thresholds.values())


def test_loadCalibratedThresholdsRejectsWrongRepresentationContract(tmp_path):
    path = _writeCalibration(tmp_path)
    calibration = json.loads(path.read_text(encoding="utf-8"))
    calibration["semanticAdmissionRepresentation"] = "enhanced"
    path.write_text(json.dumps(calibration), encoding="utf-8")

    with patch(
        "utils.llm.memory.retrieval.loadModelManifest",
        return_value={"revision": "revision", "encodingVersion": "encoding"},
    ):
        thresholds, reason = loadCalibratedThresholds(path)

    assert reason == "calibrationRepresentationMismatch"
    assert all(value is None for value in thresholds.values())


def test_loadCalibratedThresholdsRejectsMissingApprovalStatus(tmp_path):
    path = _writeCalibration(tmp_path)
    calibration = json.loads(path.read_text(encoding="utf-8"))
    calibration.pop("status")
    path.write_text(json.dumps(calibration), encoding="utf-8")

    with patch(
        "utils.llm.memory.retrieval.loadModelManifest",
        return_value={"revision": "revision", "encodingVersion": "encoding"},
    ):
        thresholds, reason = loadCalibratedThresholds(path)

    assert reason == "calibrationStatusInvalid"
    assert all(value is None for value in thresholds.values())


def test_loadCalibratedThresholdsUsesStableReasonForMalformedJson(tmp_path):
    path = tmp_path / "calibration.json"
    path.write_text("{", encoding="utf-8")

    thresholds, reason = loadCalibratedThresholds(path)

    assert reason == "JSONDecodeError"
    assert all(value is None for value in thresholds.values())


@pytest.mark.parametrize("datasetHash", ["A" * 64, "abc", 123])
def test_loadCalibratedThresholdsRejectsInvalidDatasetDigest(
    tmp_path, datasetHash,
):
    path = _writeCalibration(tmp_path, datasetHash=datasetHash)

    with patch(
        "utils.llm.memory.retrieval.loadModelManifest",
        return_value={"revision": "revision", "encodingVersion": "encoding"},
    ):
        thresholds, reason = loadCalibratedThresholds(path)

    assert reason == "calibrationDatasetInvalid"
    assert all(value is None for value in thresholds.values())


@pytest.mark.parametrize("threshold", [float("nan"), float("inf"), float("-inf")])
def test_loadCalibratedThresholdsRejectsNonFiniteValues(tmp_path, threshold):
    path = _writeCalibration(tmp_path, lexicalThreshold=threshold)

    with patch(
        "utils.llm.memory.retrieval.loadModelManifest",
        return_value={"revision": "revision", "encodingVersion": "encoding"},
    ):
        thresholds, reason = loadCalibratedThresholds(path)

    assert reason == "calibrationThresholdsInvalid"
    assert all(value is None for value in thresholds.values())


def test_loadCalibratedThresholdsRejectsNonObjectRoot(tmp_path):
    path = tmp_path / "calibration.json"
    path.write_text("[]", encoding="utf-8")

    thresholds, reason = loadCalibratedThresholds(path)

    assert reason == "calibrationRootInvalid"
    assert all(value is None for value in thresholds.values())


def test_loadCalibratedThresholdsDegradesOnInvalidManifest(tmp_path):
    path = _writeCalibration(tmp_path)

    with patch(
        "utils.llm.memory.retrieval.loadModelManifest",
        side_effect=RuntimeError("模型清单必须是 JSON 对象"),
    ):
        thresholds, reason = loadCalibratedThresholds(path)

    # 外部异常（此处为中文消息的 RuntimeError）的原因码取异常类名，
    # 消息语种不进入 diagnostics。
    assert reason == "RuntimeError"
    assert all(value is None for value in thresholds.values())


def test_buildQueryTextsUsesRecentTwentyHistoryFromTailAndKeepsOrder():
    now = datetime(2026, 1, 1, 12, 0, 0)
    history = tuple(
        {
            "direction": "incoming",
            "sender": "u",
            "content": f"历史{i}",
            "timestamp": now - timedelta(minutes=24 - i),
        }
        for i in range(25)
    )
    query = MemoryQuery(
        turns=(MemoryTurn(currentText="当前", replyText="明确引用"),),
        history=history,
    )

    current, assisted, lexical = buildQueryTexts(query, now=now)

    assert current == "当前"
    assert "明确引用" in assisted
    assert "历史5" in assisted and "历史24" in assisted
    assert "历史4" not in assisted
    assert assisted.index("历史5") < assisted.index("历史24")
    assert "明确引用" in lexical
    assert "历史9" not in lexical


def test_semanticChannelDoesNotRequireLexicalOverlap():
    candidates = [_candidate(1), _candidate(2)]

    selected, diagnostics = selectContextualCandidates(
        candidates,
        {
            "semanticCurrent": {1: 0.82},
            "semanticAssisted": {},
            "lexical": {},
        },
        {
            "semanticCurrent": 0.8,
            "semanticAssisted": None,
            "lexical": 3.0,
        },
    )

    assert [item["id"] for item in selected] == [1]
    assert diagnostics["semanticCurrentQualified"] == 1
    assert diagnostics["lexicalQualified"] == 0
    assert "candidateEvidence" not in diagnostics


def test_selectContextualCandidatesReportsOfflineEvidence():
    """离线证据同时解释绝对阈值、top gap 与 RRF 支持来源。"""
    candidates = [_candidate(1), _candidate(2)]

    selected, diagnostics = selectContextualCandidates(
        candidates,
        {
            "semanticCurrent": {1: 0.82, 2: 0.79},
            "semanticAssisted": {},
            "lexical": {1: 4.0, 2: 5.0},
        },
        {
            "semanticCurrent": 0.8,
            "semanticAssisted": None,
            "lexical": 4.5,
        },
        includeEvidence=True,
    )

    assert [item["id"] for item in selected] == [2, 1]
    channel = diagnostics["channelEvidence"]["semanticCurrent"]
    assert channel["threshold"] == 0.8
    assert channel["topMemoryID"] == 1
    assert channel["secondMemoryID"] == 2
    assert channel["topSecondGap"] == pytest.approx(0.03)
    evidenceByID = {
        item["memoryID"]: item
        for item in diagnostics["candidateEvidence"]
    }
    first = evidenceByID[1]
    assert first["qualifiedChannels"] == ["semanticCurrent"]
    assert first["supportCount"] == 1
    assert first["selectionPosition"] == 2
    assert first["channels"]["semanticCurrent"]["rank"] == 1
    assert first["channels"]["semanticCurrent"]["qualifiedRank"] == 1
    assert (
        first["channels"]["semanticCurrent"]["thresholdMargin"]
        == pytest.approx(0.02)
    )
    assert first["channels"]["semanticCurrent"]["gapFromTop"] == 0.0
    assert first["rrfContributions"]["semanticCurrent"] == pytest.approx(1 / 61)


def test_deduplicateMemoryCandidatesKeepsFirstWithinSameScope():
    """去重使用规范化正文，但不会跨越 scope 合并事实。"""
    first = _candidate(1, content="A")
    duplicate = _candidate(2, content="Ａ")
    otherScope = {
        **_candidate(3, content="A"),
        "scope_type": "user",
        "scope_id": "7",
    }

    result = deduplicateMemoryCandidates([first, duplicate, otherScope])

    assert [memory["id"] for memory in result] == [1, 3]


def test_priorityCannotLowerAdmissionThreshold():
    candidates = [_candidate(1, priority=3), _candidate(2, priority=0)]

    selected, _ = selectContextualCandidates(
        candidates,
        {"semanticCurrent": {1: 0.79, 2: 0.81}},
        {"semanticCurrent": 0.8, "semanticAssisted": None, "lexical": None},
    )

    assert [item["id"] for item in selected] == [2]


def test_hintCannotAdmitMemoryRejectedByBaseRepresentation():
    candidates = [_candidate(1), _candidate(2)]

    selected, diagnostics = selectContextualCandidates(
        candidates,
        {
            "semanticCurrent": {1: 0.79, 2: 0.81},
            "semanticAssisted": {},
            "lexical": {},
        },
        {
            "semanticCurrent": 0.8,
            "semanticAssisted": None,
            "lexical": None,
        },
        semanticRankingScores={
            "semanticCurrent": {1: 0.99, 2: 0.1},
            "semanticAssisted": {},
        },
        includeEvidence=True,
    )

    assert [item["id"] for item in selected] == [2]
    evidenceByID = {
        item["memoryID"]: item
        for item in diagnostics["candidateEvidence"]
    }
    rejected = evidenceByID[1]["channels"]["semanticCurrent"]
    assert rejected["admissionScore"] == 0.79
    assert rejected["rankingScore"] == 0.99
    assert rejected["qualified"] is False
    assert rejected["rankingBlockedByAdmission"] is True


def test_hintCanReorderOnlyBaseQualifiedMemories():
    candidates = [_candidate(1), _candidate(2)]

    selected, diagnostics = selectContextualCandidates(
        candidates,
        {
            "semanticCurrent": {1: 0.9, 2: 0.85},
            "semanticAssisted": {},
            "lexical": {},
        },
        {
            "semanticCurrent": 0.8,
            "semanticAssisted": None,
            "lexical": None,
        },
        semanticRankingScores={
            "semanticCurrent": {1: 0.2, 2: 0.95},
            "semanticAssisted": {},
        },
        includeEvidence=True,
    )

    assert [item["id"] for item in selected] == [2, 1]
    channel = diagnostics["channelEvidence"]["semanticCurrent"]
    assert channel["admissionRepresentation"] == "base"
    assert channel["rankingRepresentation"] == "enhanced"
    assert channel["topAdmissionMemoryID"] == 1
    assert channel["topMemoryID"] == 2


def test_lexicalAdmissionDoesNotCreateSemanticRrfContribution():
    candidate = _candidate(1)

    selected, diagnostics = selectContextualCandidates(
        [candidate],
        {
            "semanticCurrent": {1: 0.79},
            "semanticAssisted": {},
            "lexical": {1: 5.0},
        },
        {
            "semanticCurrent": 0.8,
            "semanticAssisted": None,
            "lexical": 4.0,
        },
        semanticRankingScores={
            "semanticCurrent": {1: 0.99},
            "semanticAssisted": {},
        },
        includeEvidence=True,
    )

    assert [item["id"] for item in selected] == [1]
    evidence = diagnostics["candidateEvidence"][0]
    assert evidence["qualifiedChannels"] == ["lexical"]
    assert "semanticCurrent" not in evidence["rrfContributions"]
    assert evidence["channels"]["semanticCurrent"]["rankingBlockedByAdmission"] is True


def test_renderMemoryContextHonorsHardBudgetAndSkipsOversizedFact():
    contextual = [
        _candidate(1, content="长" * 1500),
        _candidate(2, content="短事实"),
    ]

    items, block, diagnostics = renderMemoryContext([], contextual)

    assert [item["id"] for item in items] == [2]
    assert len(block) <= 1500
    assert "短事实" in block
    assert "不应进入最终上下文" not in block
    assert diagnostics["contextualBudgetDropped"] == 1


def test_renderMemoryContextCanSelectMoreThanTenShortFacts():
    contextual = [_candidate(index, content=f"偏好{index}") for index in range(1, 16)]

    items, block, _ = renderMemoryContext([], contextual)

    assert len(items) > 10
    assert len(block) <= 1500


@pytest.mark.asyncio
async def test_hybridUsesCandidateBeyondLegacyPool():
    candidates = [_candidate(index) for index in range(1, 251)]
    target = candidates[-1]
    with (
        patch(
            "utils.llm.memory.retrieval.getMemoryCandidates",
            new_callable=AsyncMock,
            return_value=candidates,
        ),
        patch(
            "utils.llm.memory.retrieval.getMemorySnapshots",
            new_callable=AsyncMock,
            return_value=[target],
        ),
        patch(
            "utils.llm.memory.retrieval.loadCalibratedThresholds",
            return_value=(
                {"semanticCurrent": None, "semanticAssisted": None, "lexical": 1.0},
                None,
            ),
        ),
        patch(
            "utils.llm.memory.retrieval.scoreLexicalCandidates",
            return_value={250: 2.0},
        ),
    ):
        result = await retrieveMemoryContext(
            chatID="1",
            query=MemoryQuery(turns=(MemoryTurn(currentText="目标"),)),
            llmConfig={"memoryRetrievalMode": "hybrid"},
        )

    assert [item["id"] for item in result.items] == [250]
    assert result.diagnostics["candidateCount"] == 250


@pytest.mark.asyncio
async def test_legacy_reuses_full_candidate_read_for_pinned_and_contextual():
    """legacy 读取一次完整候选，且 pinned 不占 contextual 的旧配额。"""
    candidates = [
        _candidate(1, priority=3),
        _candidate(2, priority=1),
        # 故意让 pinned priority 更高：它不能因此挤掉 contextual 的
        # perScopeLimit 名额，否则线上 legacy 与离线评测会产生不同语义。
        _candidate(3, mode="pinned", priority=99),
    ]
    getCandidates = AsyncMock(return_value=candidates)
    with (
        patch(
            "utils.llm.memory.retrieval.getMemoryCandidates",
            getCandidates,
        ),
        patch(
            "utils.llm.memory.retrieval.getMemorySnapshots",
            new_callable=AsyncMock,
            return_value=candidates,
        ),
    ):
        result = await retrieveMemoryContext(
            chatID="1",
            query=MemoryQuery(),
            llmConfig={"memoryRetrievalMode": "legacy"},
            legacyLimits=(1, 10),
        )

    assert getCandidates.await_count == 1
    assert [item["id"] for item in result.items] == [3, 1]
    assert result.diagnostics["contextualCandidateCount"] == 2


@pytest.mark.asyncio
async def test_lexicalFailureKeepsPinnedMemory():
    pinned = _candidate(1, mode="pinned")
    contextual = _candidate(2)
    with (
        patch(
            "utils.llm.memory.retrieval.getMemoryCandidates",
            new_callable=AsyncMock,
            return_value=[pinned, contextual],
        ),
        patch(
            "utils.llm.memory.retrieval.getMemorySnapshots",
            new_callable=AsyncMock,
            return_value=[pinned],
        ),
        patch(
            "utils.llm.memory.retrieval.loadCalibratedThresholds",
            return_value=(
                {"semanticCurrent": None, "semanticAssisted": None, "lexical": 1.0},
                None,
            ),
        ),
        patch(
            "utils.llm.memory.retrieval.scoreLexicalCandidates",
            side_effect=RuntimeError("lexical unavailable"),
        ),
    ):
        result = await retrieveMemoryContext(
            chatID="1",
            query=MemoryQuery(turns=(MemoryTurn(currentText="目标"),)),
            llmConfig={"memoryRetrievalMode": "hybrid"},
        )

    assert [item["id"] for item in result.items] == [1]
    assert result.diagnostics["degradedReason"] == "lexical:RuntimeError"


@pytest.mark.asyncio
async def test_degradedReasonsKeepIndependentLexicalAndSemanticFailures():
    """多个通道同时失败时保留完整原因，而不是只留下最后一次赋值。"""
    candidate = _candidate(1)
    runtime = SimpleNamespace(
        scoreSemantic=AsyncMock(side_effect=RuntimeError("semantic unavailable")),
        getSemanticCacheStatus=lambda candidates: {
            "status": "cold",
            "candidateCount": len(candidates),
            "readyCount": 0,
            "missingCount": len(candidates),
            "staleCount": 0,
            "pendingCount": 0,
            "cacheEntries": 0,
            "cacheBytes": 0,
            "reconcileCapacitySaturated": False,
        },
    )
    state = SimpleNamespace(getMemoryRuntime=lambda: runtime, getShutdownEvent=asyncio.Event)
    with (
        patch(
            "utils.llm.memory.retrieval.getMemoryCandidates",
            new_callable=AsyncMock,
            return_value=[candidate],
        ),
        patch(
            "utils.llm.memory.retrieval.scoreLexicalCandidates",
            side_effect=RuntimeError("lexical unavailable"),
        ),
        patch(
            "utils.llm.memory.retrieval.loadCalibratedThresholds",
            return_value=(
                {"semanticCurrent": 0.8, "semanticAssisted": None, "lexical": 1.0},
                None,
            ),
        ),
        patch("utils.llm.memory.retrieval.getStateManager", return_value=state),
    ):
        result = await retrieveMemoryContext(
            chatID="1",
            query=MemoryQuery(turns=(MemoryTurn(currentText="目标"),)),
            llmConfig={"memoryRetrievalMode": "hybrid"},
        )

    assert result.diagnostics["degradedReason"] == "lexical:RuntimeError"
    assert result.diagnostics["degradedReasons"] == [
        "lexical:RuntimeError",
        "semantic:RuntimeError",
    ]
    assert result.diagnostics["channelDiagnostics"]["lexical"]["status"] == "error"
    assert result.diagnostics["channelDiagnostics"]["semanticCurrent"]["status"] == "error"
    assert result.diagnostics["semanticCache"]["status"] == "cold"


@pytest.mark.asyncio
async def test_lexicalTimeoutKeepsFinalizeBudgetForPinned(monkeypatch):
    """超时保留常驻复核预算，测试结束前收齐无法立即取消的词面线程。"""
    monkeypatch.setattr(retrievalModule, "LLM_MEMORY_RETRIEVAL_TIMEOUT_SECONDS", 0.08)
    monkeypatch.setattr(retrievalModule, "LLM_MEMORY_FINALIZE_RESERVE_SECONDS", 0.04)
    pinned = _candidate(1, mode="pinned")
    contextual = _candidate(2)
    started = threading.Event()
    release = threading.Event()

    def _blockedLexical(*args):
        """等待测试明确释放，不让线程越过其事件循环生命周期。"""
        started.set()
        release.wait(timeout=1)
        return {2: 2.0}

    with (
        patch(
            "utils.llm.memory.retrieval.getMemoryCandidates",
            new_callable=AsyncMock,
            return_value=[pinned, contextual],
        ),
        patch(
            "utils.llm.memory.retrieval.getMemorySnapshots",
            new_callable=AsyncMock,
            return_value=[pinned],
        ),
        patch(
            "utils.llm.memory.retrieval.loadCalibratedThresholds",
            return_value=(
                {"semanticCurrent": None, "semanticAssisted": None, "lexical": 1.0},
                None,
            ),
        ),
        patch(
            "utils.llm.memory.retrieval.scoreLexicalCandidates",
            side_effect=_blockedLexical,
        ),
    ):
        retrievalTask = asyncio.create_task(retrieveMemoryContext(
            chatID="1",
            query=MemoryQuery(turns=(MemoryTurn(currentText="目标"),)),
            llmConfig={"memoryRetrievalMode": "hybrid"},
        ))
        try:
            assert await asyncio.to_thread(started.wait, 1)
            result = await retrievalTask
        finally:
            release.set()
            for _ in range(100):
                if retrievalModule._activeRetrievals == 0:
                    break
                await asyncio.sleep(.005)
            assert retrievalModule._activeRetrievals == 0

    assert [item["id"] for item in result.items] == [1]
    assert result.diagnostics["degradedReason"] == "lexicalTimeout"


@pytest.mark.asyncio
async def test_emptyHybridQueryReturnsOnlyPinned():
    pinned = _candidate(1, mode="pinned")
    contextual = _candidate(2)
    with (
        patch(
            "utils.llm.memory.retrieval.getMemoryCandidates",
            new_callable=AsyncMock,
            return_value=[pinned, contextual],
        ),
        patch(
            "utils.llm.memory.retrieval.getMemorySnapshots",
            new_callable=AsyncMock,
            return_value=[pinned],
        ),
        patch(
            "utils.llm.memory.retrieval.loadCalibratedThresholds",
            return_value=(
                {"semanticCurrent": 0.8, "semanticAssisted": 0.8, "lexical": 1.0},
                None,
            ),
        ),
    ):
        result = await retrieveMemoryContext(
            chatID="1",
            query=MemoryQuery(),
            llmConfig={"memoryRetrievalMode": "hybrid"},
        )

    assert [item["id"] for item in result.items] == [1]


@pytest.mark.asyncio
async def test_assisted_channel_handles_text_equal_to_disabled_current_channel():
    candidate = _candidate(1)
    runtime = SimpleNamespace(
        scoreSemantic=AsyncMock(return_value=[{
            "base": {1: 0.9},
            "enhanced": {1: 0.9},
        }]),
    )
    state = SimpleNamespace(getMemoryRuntime=lambda: runtime, getShutdownEvent=asyncio.Event)
    with (
        patch(
            "utils.llm.memory.retrieval.getMemoryCandidates",
            new_callable=AsyncMock,
            return_value=[candidate],
        ),
        patch(
            "utils.llm.memory.retrieval.getMemorySnapshots",
            new_callable=AsyncMock,
            return_value=[candidate],
        ),
        patch(
            "utils.llm.memory.retrieval.loadCalibratedThresholds",
            return_value=(
                {"semanticCurrent": None, "semanticAssisted": 0.8, "lexical": None},
                None,
            ),
        ),
        patch("utils.llm.memory.retrieval.getStateManager", return_value=state),
    ):
        result = await retrieveMemoryContext(
            chatID="1",
            query=MemoryQuery(turns=(MemoryTurn(currentText="目标"),)),
            llmConfig={"memoryRetrievalMode": "hybrid"},
        )

    assert [item["id"] for item in result.items] == [1]
    assert runtime.scoreSemantic.await_args.args[0] == ["目标"]
    assert result.diagnostics["semanticCurrentQualified"] == 0
    assert result.diagnostics["semanticAssistedQualified"] == 1


@pytest.mark.asyncio
async def test_equal_semantic_text_uses_current_as_single_canonical_channel():
    candidate = _candidate(1)
    runtime = SimpleNamespace(
        scoreSemantic=AsyncMock(return_value=[{
            "base": {1: 0.9},
            "enhanced": {1: 0.9},
        }]),
    )
    state = SimpleNamespace(getMemoryRuntime=lambda: runtime, getShutdownEvent=asyncio.Event)
    with (
        patch(
            "utils.llm.memory.retrieval.getMemoryCandidates",
            new_callable=AsyncMock,
            return_value=[candidate],
        ),
        patch(
            "utils.llm.memory.retrieval.getMemorySnapshots",
            new_callable=AsyncMock,
            return_value=[candidate],
        ),
        patch(
            "utils.llm.memory.retrieval.loadCalibratedThresholds",
            return_value=(
                {"semanticCurrent": 0.8, "semanticAssisted": 0.8, "lexical": None},
                None,
            ),
        ),
        patch("utils.llm.memory.retrieval.getStateManager", return_value=state),
    ):
        result = await retrieveMemoryContext(
            chatID="1",
            query=MemoryQuery(turns=(MemoryTurn(currentText="目标"),)),
            llmConfig={"memoryRetrievalMode": "hybrid"},
        )

    assert [item["id"] for item in result.items] == [1]
    assert runtime.scoreSemantic.await_args.args[0] == ["目标"]
    assert result.diagnostics["semanticCurrentQualified"] == 1
    assert result.diagnostics["semanticAssistedQualified"] == 0


@pytest.mark.asyncio
async def test_timeout_keeps_retrieval_slot_until_thread_finishes():
    started = threading.Event()
    release = threading.Event()

    def _blockingCandidates():
        started.set()
        release.wait(timeout=2)
        return []

    async def _getCandidates(**kwargs):
        return await asyncio.to_thread(_blockingCandidates)

    retrievalModule._activeRetrievals = 0
    try:
        with (
            patch(
                "utils.llm.memory.retrieval.LLM_MEMORY_RETRIEVAL_TIMEOUT_SECONDS",
                0.02,
            ),
            patch(
                "utils.llm.memory.retrieval.LLM_MEMORY_MAX_ACTIVE_RETRIEVALS",
                1,
            ),
            patch(
                "utils.llm.memory.retrieval.getMemoryCandidates",
                side_effect=_getCandidates,
            ),
        ):
            first = await retrieveMemoryContext(
                chatID="1",
                query=MemoryQuery(),
                llmConfig={"memoryRetrievalMode": "hybrid"},
            )
            assert started.is_set()
            assert first.diagnostics["degradedReason"] == "retrievalTimeout"
            assert retrievalModule._activeRetrievals == 1

            second = await retrieveMemoryContext(
                chatID="2",
                query=MemoryQuery(),
                llmConfig={"memoryRetrievalMode": "hybrid"},
            )
            assert second.diagnostics["degradedReason"] == "retrievalCapacity"

            release.set()
            for _ in range(100):
                if retrievalModule._activeRetrievals == 0:
                    break
                await asyncio.sleep(0.01)
            assert retrievalModule._activeRetrievals == 0
    finally:
        release.set()
        retrievalModule._activeRetrievals = 0
