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
    loadCalibratedThresholds,
    renderMemoryContext,
    retrieveMemoryContext,
    selectContextualCandidates,
)
from utils.llm.memory import retrieval as retrievalModule
from utils.llm.memory.types import MemoryQuery, MemoryTurn


# ============================================================================
# buildMemoryContextBlock() 呈现层测试
# ============================================================================

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
        "schemaVersion": 1,
        "status": "approved",
        "modelRevision": "revision",
        "encodingVersion": "encoding",
        "lexicalVersion": "memory-bm25-v1",
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


def test_priorityCannotLowerAdmissionThreshold():
    candidates = [_candidate(1, priority=3), _candidate(2, priority=0)]

    selected, _ = selectContextualCandidates(
        candidates,
        {"semanticCurrent": {1: 0.79, 2: 0.81}},
        {"semanticCurrent": 0.8, "semanticAssisted": None, "lexical": None},
    )

    assert [item["id"] for item in selected] == [2]


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
    state = SimpleNamespace(getMemoryRuntime=lambda: runtime)
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
    monkeypatch.setattr(retrievalModule, "LLM_MEMORY_RETRIEVAL_TIMEOUT_SECONDS", 0.08)
    monkeypatch.setattr(retrievalModule, "LLM_MEMORY_FINALIZE_RESERVE_SECONDS", 0.04)
    pinned = _candidate(1, mode="pinned")
    contextual = _candidate(2)
    started = threading.Event()
    release = threading.Event()

    def _blockedLexical(*args):
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
        assert await asyncio.to_thread(started.wait, 1)
        result = await retrievalTask
        release.set()
        await asyncio.sleep(0)

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
        scoreSemantic=AsyncMock(return_value=[{1: 0.9}]),
    )
    state = SimpleNamespace(getMemoryRuntime=lambda: runtime)
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
        scoreSemantic=AsyncMock(return_value=[{1: 0.9}]),
    )
    state = SimpleNamespace(getMemoryRuntime=lambda: runtime)
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
