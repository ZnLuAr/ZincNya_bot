"""测试 memory 离线评测入口：fixture、calibration/holdout、pinned gate 与 benchmark。"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.evaluateMemory import (
    EvaluationError,
    _chooseThreshold,
    _calibrationObservations,
    _evaluateSingleCase,
    _scoreCaseResult,
    _writeReport,
    calibrateRetrievalThresholds,
    evaluateRetrievalCases,
    loadEvaluationCases,
    loadApprovedCalibration,
    scoreCaseChannels,
    validateEvaluationCases,
    runEncoderBenchmark,
    runRetrievalBenchmark,
)


SMOKE_CASES_PATH = (
    Path(__file__).resolve().parents[1]
    / "utils"
    / "llm"
    / "memory"
    / "fixtures"
    / "retrievalSmokeCases.json"
)




class _FakeVectors:
    def __init__(self, rows):
        self.shape = (rows, 4)


class _FakeSimilarities:
    def __init__(self, score):
        self.score = score

    def max(self):
        return self.score


class _FakeMatrix:
    nbytes = 4

    def __matmul__(self, vector):
        return _FakeSimilarities(0.5)


class _FakeEncoder:
    def __init__(self, **kwargs):
        self.closed = False

    def encodeMemory(self, memory):
        return _FakeVectors(1)

    def encodeQueries(self, texts):
        return _FakeVectors(len(texts))

    def close(self):
        self.closed = True


class _FailingEncoder(_FakeEncoder):
    instances = []

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.instances.append(self)

    def encodeMemory(self, memory):
        raise RuntimeError("encode failed")


class _FakeProcess:
    def __init__(self):
        self._values = iter((100, 140, 180, 160))

    def memory_info(self):
        return SimpleNamespace(rss=next(self._values))


class _BenchmarkEncoder(_FakeEncoder):
    def encodeMemory(self, memory):
        return _FakeMatrix()

    def encodeQueries(self, texts):
        return [object() for _ in texts]


def _case(caseID, split, *, hint=None, currentText="当前问题"):
    memory = {
        "id": 1,
        "scope_type": "global",
        "scope_id": "global",
        "content": "用户正在准备考试",
        "tags": ["学习"],
    }
    if hint is not None:
        memory["retrieval_hint"] = hint
    return {
        "caseID": caseID,
        "groupID": f"group-{caseID}",
        "split": split,
        "scope": {},
        "memories": [memory, {
            "id": 2,
            "scope_type": "global",
            "scope_id": "global",
            "content": "用户正在计划旅行",
            "tags": ["出行"],
        }],
        "query": {"currentText": currentText},
        "requiredIDs": [1],
        "allowedIDs": [],
        "forbiddenIDs": [2],
        "allowAbstain": False,
    }


def _writeManifest(tmpPath):
    manifest = {
        "schemaVersion": 1,
        "repository": "example/model",
        "revision": "a" * 40,
        "encodingVersion": "test-v1",
        "queryPrefix": "q",
        "maxTokens": 8,
        "embeddingDimension": 4,
        "pooling": "cls",
        "normalization": "l2",
        "specialTokens": {"cls": "[CLS]", "sep": "[SEP]", "pad": "[PAD]"},
        "artifacts": [{"path": "model", "size": 0, "sha256": "0" * 64}],
    }
    path = tmpPath / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_runEncoderBenchmarkReportsCountsAndObservedRss(tmp_path):
    report = runEncoderBenchmark(
        memoryCount=3,
        queryCount=2,
        manifestPath=_writeManifest(tmp_path),
        encoderFactory=_FakeEncoder,
        processFactory=_FakeProcess,
    )

    assert report["memoryCount"] == 3
    assert report["memoryChunkCount"] == 3
    assert report["queryCount"] == 2
    assert report["rssBytes"]["maxObservedDelta"] == 80
    assert "瞬时峰值" in report["note"]


def test_runEncoderBenchmarkRejectsEmptyWorkload(tmp_path):
    with pytest.raises(ValueError, match="必须大于 0"):
        runEncoderBenchmark(
            memoryCount=0,
            queryCount=1,
            manifestPath=_writeManifest(tmp_path),
            encoderFactory=_FakeEncoder,
            processFactory=_FakeProcess,
        )


def test_runEncoderBenchmarkClosesEncoderWhenEncodingFails(tmp_path):
    _FailingEncoder.instances.clear()

    with pytest.raises(RuntimeError, match="encode failed"):
        runEncoderBenchmark(
            memoryCount=1,
            queryCount=1,
            manifestPath=_writeManifest(tmp_path),
            encoderFactory=_FailingEncoder,
            processFactory=lambda: _FakeProcess(),
        )

    assert _FailingEncoder.instances[0].closed is True


def test_writeReportUsesUtf8Json(tmp_path):
    outputPath = tmp_path / "report.json"

    _writeReport({"状态": "完成"}, outputPath)

    assert json.loads(outputPath.read_text(encoding="utf-8")) == {"状态": "完成"}
    assert not (tmp_path / "report.json.tmp").exists()


def test_validateEvaluationCasesRejectsGroupSplitLeakage():
    first = _case("calibration-case", "calibration")
    second = _case("holdout-case", "holdout")
    second["groupID"] = first["groupID"]

    with pytest.raises(EvaluationError, match="跨 split"):
        validateEvaluationCases([first, second])


def test_validateEvaluationCasesRequiresBooleanAbstentionFlag():
    case = _case("case", "calibration")
    case["allowAbstain"] = "false"

    with pytest.raises(EvaluationError, match="allowAbstain"):
        validateEvaluationCases([case])


def test_scoreCaseChannelsUsesFixtureClockForRecentHistory():
    case = _case("stable-history", "calibration", currentText="这个呢")
    case["queryNow"] = "2026-01-01T12:00:00"
    case["query"]["history"] = [{
        "direction": "incoming",
        "sender": "用户",
        "content": "前面在讨论资格考试",
        "timestamp": "2026-01-01T11:55:00",
    }]
    normalized = validateEvaluationCases([case])[0]
    captured = []

    class _Scorer:
        def score(self, queryTexts, memories, *, includeHint=True):
            captured.extend(queryTexts)
            return [{1: 0.9, 2: 0.1} for _ in queryTexts]

    scoreCaseChannels(normalized, semanticScorer=_Scorer())

    assert any("前面在讨论资格考试" in text for text in captured)


def test_scoreCaseChannelsUsesCurrentAsCanonicalForEqualTexts():
    case = validateEvaluationCases([
        _case("equal-text", "calibration", currentText="当前问题"),
    ])[0]
    captured = []

    class _Scorer:
        def score(self, queryTexts, memories, *, includeHint=True):
            captured.extend(queryTexts)
            return [{1: 0.9, 2: 0.1} for _ in queryTexts]

    scores = scoreCaseChannels(case, semanticScorer=_Scorer())

    assert captured == ["当前问题"]
    assert scores["semanticCurrent"] == {1: 0.9, 2: 0.1}
    assert scores["semanticAssisted"] == {}


def test_scoreCaseChannelsLetsAssistedTakeOverWhenCurrentIsDisabled():
    case = validateEvaluationCases([
        _case("assisted-fallback", "calibration", currentText="当前问题"),
    ])[0]
    captured = []

    class _Scorer:
        def score(self, queryTexts, memories, *, includeHint=True):
            captured.extend(queryTexts)
            return [{1: 0.9} for _ in queryTexts]

    scores = scoreCaseChannels(
        case,
        semanticScorer=_Scorer(),
        thresholds={
            "semanticCurrent": None,
            "semanticAssisted": 0.8,
        },
    )

    assert captured == ["当前问题"]
    assert scores["semanticCurrent"] == {}
    assert scores["semanticAssisted"] == {1: 0.9}


def test_calibrationObservationsKeepBothEqualTextFallbackDistributions():
    case = validateEvaluationCases([
        _case("calibration-fallback", "calibration"),
    ])[0]

    class _Scorer:
        def score(self, queryTexts, memories, *, includeHint=True):
            return [{1: 0.9, 2: 0.1} for _ in queryTexts]

    observations = _calibrationObservations(
        [case],
        semanticScorer=_Scorer(),
    )

    assert {item["memoryID"] for item in observations["semanticCurrent"]} == {1, 2}
    assert {item["memoryID"] for item in observations["semanticAssisted"]} == {1, 2}


def test_validateEvaluationCasesRequiresClockWhenHistoryIsPresent():
    case = _case("missing-clock", "calibration")
    case["query"]["history"] = [{
        "content": "之前的消息",
        "timestamp": "2026-01-01T11:55:00",
    }]

    with pytest.raises(EvaluationError, match="queryNow"):
        validateEvaluationCases([case])


def test_loadEvaluationCasesValidatesDeclaredSplitCounts(tmp_path):
    path = tmp_path / "cases.json"
    path.write_text(json.dumps({
        "schemaVersion": 1,
        "calibrationCaseCount": 2,
        "holdoutCaseCount": 0,
        "cases": [_case("one", "calibration")],
    }), encoding="utf-8")

    with pytest.raises(EvaluationError, match="calibrationCaseCount"):
        loadEvaluationCases(path)


def test_chooseThresholdDisablesChannelWithoutValidPositive():
    threshold, report = _chooseThreshold([
        {"score": 0.9, "positive": False, "forbidden": True},
    ])

    assert threshold is None
    assert report["enabled"] is False


def test_calibrationUsesOnlyCalibrationCasesAndBindsNormalizedDataset(tmp_path):
    rawCases = [_case("calibration-case", "calibration")]
    cases = validateEvaluationCases(rawCases)

    manifestPath = _writeManifest(tmp_path)

    class _Scorer:
        def score(self, queryTexts, memories, *, includeHint=True):
            assert all("retrievalHint" in memory for memory in memories) is includeHint
            return [{
                1: 0.9,
                2: 0.1,
            } for _ in queryTexts]

    report = calibrateRetrievalThresholds(
        cases,
        semanticScorer=_Scorer(),
        manifestPath=manifestPath,
    )

    assert report["calibrationCaseCount"] == 1
    assert report["thresholds"]["semanticCurrent"] == 0.9
    assert len(report["datasetSha256"]) == 64
    assert report["status"] == "candidate"


def test_evaluateRetrievalCasesDoesNotReadHoldoutForCalibration():
    cases = validateEvaluationCases([
        _case("calibration-case", "calibration"),
        _case("holdout-case", "holdout"),
    ])

    class _Scorer:
        def score(self, queryTexts, memories, *, includeHint=True):
            return [{1: 0.9, 2: 0.1} for _ in queryTexts]

    report = evaluateRetrievalCases(
        cases,
        {
            "semanticCurrent": 0.8,
            "semanticAssisted": 0.8,
            "lexical": None,
        },
        semanticScorer=_Scorer(),
        split="holdout",
    )

    assert report["caseCount"] == 1
    assert list(report["modes"]["hybrid"]["cases"]) == [
        report["modes"]["hybrid"]["cases"][0]
    ]
    assert report["modes"]["hybrid"]["cases"][0]["caseID"] == "holdout-case"


def test_loadApprovedCalibrationRejectsCandidate(tmp_path):
    path = tmp_path / "candidate.json"
    path.write_text(json.dumps({"status": "candidate"}), encoding="utf-8")

    with pytest.raises(EvaluationError, match="candidate"):
        loadApprovedCalibration(path)


def test_runRetrievalBenchmarkUsesOneEncoderAndReportsCleanup(tmp_path):
    encoder = _BenchmarkEncoder()
    factoryCalls = []

    def _factory(**kwargs):
        factoryCalls.append(kwargs)
        return encoder

    report = runRetrievalBenchmark(
        memoryCount=3,
        queryCount=2,
        concurrency=(1, 2),
        manifestPath=_writeManifest(tmp_path),
        encoderFactory=_factory,
        processFactory=_FakeProcess,
    )

    assert len(factoryCalls) == 1
    assert encoder.closed is True
    assert report["isolated"] is False
    assert report["matrixBytes"] == 12
    assert set(report["concurrencyReports"]) == {"1", "2"}


def test_retrievalSmokeFixtureValidatesSplitAndPinnedLabels():
    """边界 fixture 可加载，且 pinned 标注不混入 contextual 指标。"""
    cases, datasetSha256 = loadEvaluationCases(SMOKE_CASES_PATH)

    assert len(cases) == 6
    assert len([case for case in cases if case["split"] == "calibration"]) == 3
    assert len([case for case in cases if case["split"] == "holdout"]) == 3
    assert len(datasetSha256) == 64

    casesByID = {case["caseID"]: case for case in cases}
    assert casesByID["pinned-disabled"]["requiredPinnedIDs"] == {5001}
    assert casesByID["pinned-budget"]["forbiddenPinnedIDs"] == {5042}
    assert casesByID["empty-disabled"]["allowAbstain"] is True

    scored = _scoreCaseResult(
        casesByID["pinned-budget"],
        {"contextualIDs": [5043], "pinnedIDs": [5041]},
    )

    assert scored["precision"] == 1.0
    assert scored["recall"] == 1.0
    assert scored["pinnedRecall"] == 1.0
    assert scored["pinnedForbiddenHitIDs"] == []


def test_retrievalSmokeFixtureExercisesBudgetScopeAndEmptyQueryPaths():
    """用确定性通道分数验证冒烟 fixture 的关键结构分支。"""
    cases, _ = loadEvaluationCases(SMOKE_CASES_PATH)
    casesByID = {case["caseID"]: case for case in cases}
    thresholds = {
        "semanticCurrent": 0.8,
        "semanticAssisted": None,
        "lexical": None,
    }

    budgetResult = _evaluateSingleCase(
        casesByID["pinned-budget"],
        "hybrid",
        thresholds,
        channelScores={
            "semanticCurrent": {5043: 0.95},
            "semanticAssisted": {},
            "lexical": {},
        },
    )
    emptyResult = _evaluateSingleCase(
        casesByID["empty-disabled"],
        "hybrid",
        thresholds,
        channelScores={
            "semanticCurrent": {},
            "semanticAssisted": {},
            "lexical": {},
        },
    )
    scopeResult = _evaluateSingleCase(
        casesByID["scope-isolation"],
        "hybrid",
        thresholds,
        channelScores={
            "semanticCurrent": {5061: 0.81, 5062: 0.99, 5063: 0.1},
            "semanticAssisted": {},
            "lexical": {},
        },
    )
    widePoolResult = _evaluateSingleCase(
        casesByID["wide-pool"],
        "hybrid",
        thresholds,
        channelScores={
            "semanticCurrent": {5020: 0.99},
            "semanticAssisted": {},
            "lexical": {},
        },
    )

    assert budgetResult["pinnedIDs"] == [5041]
    assert budgetResult["contextualIDs"] == [5043]
    assert emptyResult["pinnedIDs"] == [5051]
    assert emptyResult["contextualIDs"] == []
    assert scopeResult["contextualIDs"] == [5061]
    assert widePoolResult["contextualIDs"] == [5020]
