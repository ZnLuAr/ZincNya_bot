"""测试 memory 离线评测入口：fixture、calibration/holdout、pinned gate 与 benchmark。"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.llmMemory.evaluateMemory import (
    EvaluationError,
    _chooseThreshold,
    _calibrationObservations,
    _evaluateSingleCase,
    _scoreCaseResult,
    _writeReport,
    _topMarginEvidence,
    _marginStudyMetrics,
    calibrateRetrievalThresholds,
    evaluateRetrievalCases,
    loadEvaluationCases,
    loadApprovedCalibration,
    scoreCaseChannels,
    studyTopMargins,
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
RETRIEVAL_CASES_PATH = SMOKE_CASES_PATH.with_name("retrievalCases.json")




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

    def encodeMemoryRepresentations(self, memory):
        return SimpleNamespace(
            base=_FakeVectors(1),
            enhanced=_FakeVectors(1),
        )

    def encodeQueries(self, texts):
        return _FakeVectors(len(texts))

    def close(self):
        self.closed = True


class _FailingEncoder(_FakeEncoder):
    instances = []

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.instances.append(self)

    def encodeMemoryRepresentations(self, memory):
        raise RuntimeError("encode failed")


class _FakeProcess:
    def __init__(self):
        self._values = iter((100, 140, 180, 160))

    def memory_info(self):
        return SimpleNamespace(rss=next(self._values))


class _BenchmarkEncoder(_FakeEncoder):
    def encodeMemoryRepresentations(self, memory):
        return SimpleNamespace(
            base=_FakeMatrix(),
            enhanced=_FakeMatrix(),
        )

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
    assert report["memoryChunkCount"] == 6
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

    scores, rankingScores = scoreCaseChannels(
        case,
        semanticScorer=_Scorer(),
    )

    assert captured == ["当前问题"]
    assert scores["semanticCurrent"] == {1: 0.9, 2: 0.1}
    assert scores["semanticAssisted"] == {}
    assert rankingScores["semanticCurrent"] == {1: 0.9, 2: 0.1}


def test_scoreCaseChannelsLetsAssistedTakeOverWhenCurrentIsDisabled():
    case = validateEvaluationCases([
        _case("assisted-fallback", "calibration", currentText="当前问题"),
    ])[0]
    captured = []

    class _Scorer:
        def score(self, queryTexts, memories, *, includeHint=True):
            captured.extend(queryTexts)
            return [{1: 0.9} for _ in queryTexts]

    scores, rankingScores = scoreCaseChannels(
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
    assert rankingScores["semanticAssisted"] == {1: 0.9}


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


def test_chooseThresholdKeepsEqualScoresTogether():
    """大样本优化仍按完整同分桶评估，不能制造不存在的阈值。"""
    threshold, report = _chooseThreshold([
        {"score": 0.9, "positive": True, "forbidden": False},
        {"score": 0.8, "positive": False, "forbidden": False},
        {"score": 0.8, "positive": True, "forbidden": False},
    ], precisionTarget=0.5)
    assert threshold == 0.8
    assert report["qualifiedCount"] == 3
    assert report["acceptedCount"] == 2


def test_calibrationUsesOnlyCalibrationCasesAndBindsNormalizedDataset(tmp_path):
    rawCases = [_case("calibration-case", "calibration")]
    cases = validateEvaluationCases(rawCases)

    manifestPath = _writeManifest(tmp_path)

    class _Scorer:
        def score(self, queryTexts, memories):
            return [{
                "base": {1: 0.9, 2: 0.1},
                # 若 calibration 错把 enhanced 当准入分，阈值会变成 0.99。
                "enhanced": {1: 0.99, 2: 0.98},
            } for _ in queryTexts]

    report = calibrateRetrievalThresholds(
        cases,
        semanticScorer=_Scorer(),
        manifestPath=manifestPath,
    )

    assert report["calibrationCaseCount"] == 1
    assert report["thresholds"]["semanticCurrent"] == 0.9
    assert report["semanticAdmissionRepresentation"] == "base"
    assert report["semanticRankingRepresentation"] == "enhanced"
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


def test_evaluateSingleCaseUsesProductionDeduplicationAndReportsOutcome():
    """离线结果不得计入线上会在渲染前移除的同 scope 重复事实。"""
    case = _case("duplicate", "holdout")
    case["memories"][0]["priority"] = 1
    case["memories"].append({
        **case["memories"][0],
        "id": 3,
        "priority": 0,
    })
    case["allowedIDs"] = [3]
    normalized = validateEvaluationCases([case])[0]

    result = _evaluateSingleCase(
        normalized,
        "hybrid",
        {
            "semanticCurrent": 0.8,
            "semanticAssisted": None,
            "lexical": None,
        },
        channelScores={
            "semanticCurrent": {1: 0.9, 3: 0.85},
            "semanticAssisted": {},
            "lexical": {},
        },
    )

    assert result["contextualIDs"] == [1]
    evidenceByID = {
        item["memoryID"]: item
        for item in result["diagnostics"]["candidateEvidence"]
    }
    assert evidenceByID[1]["survivedDeduplication"] is True
    assert evidenceByID[1]["rendered"] is True
    assert evidenceByID[3]["fusedQualified"] is True
    assert evidenceByID[3]["survivedDeduplication"] is False
    assert evidenceByID[3]["rendered"] is False

    unqualified = evidenceByID[2]
    assert unqualified["fusedQualified"] is False
    assert unqualified["survivedDeduplication"] is None
    assert unqualified["rendered"] is False
    serialized = json.dumps(result, ensure_ascii=False)
    assert case["memories"][0]["content"] not in serialized
    assert "retrievalHint" not in serialized


def test_hybridHintChangesRankingWithoutChangingBaseAdmission():
    """hybrid+hint 只能给两条已合格记忆换序，不能改变准入集合。"""
    normalized = validateEvaluationCases([
        _case("hint-ranking", "holdout")
    ])[0]
    thresholds = {
        "semanticCurrent": 0.8,
        "semanticAssisted": None,
        "lexical": None,
    }
    channelScores = {
        "semanticCurrent": {1: 0.9, 2: 0.85},
        "semanticAssisted": {},
        "lexical": {},
    }
    semanticRankingScores = {
        "semanticCurrent": {1: 0.2, 2: 0.95},
        "semanticAssisted": {},
    }

    baseResult = _evaluateSingleCase(
        normalized,
        "hybrid",
        thresholds,
        channelScores=channelScores,
        semanticRankingScores=semanticRankingScores,
    )
    hintResult = _evaluateSingleCase(
        normalized,
        "hybrid+hint",
        thresholds,
        channelScores=channelScores,
        semanticRankingScores=semanticRankingScores,
    )

    assert baseResult["contextualIDs"] == [1, 2]
    assert hintResult["contextualIDs"] == [2, 1]
    assert (
        hintResult["diagnostics"]["channelEvidence"]["semanticCurrent"]
        ["admissionRepresentation"]
        == "base"
    )
    assert (
        hintResult["diagnostics"]["channelEvidence"]["semanticCurrent"]
        ["rankingRepresentation"]
        == "enhanced"
    )


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
    assert report["matrixBytes"] == 24
    assert set(report["concurrencyReports"]) == {"1", "2"}


def test_retrievalSmokeFixtureValidatesSplitAndPinnedLabels():
    """边界 fixture 可加载，且 pinned 标注不混入 contextual 指标。"""
    cases, datasetSha256 = loadEvaluationCases(SMOKE_CASES_PATH)

    assert len(cases) == 6
    assert len([case for case in cases if case["split"] == "calibration"]) == 3
    assert len([case for case in cases if case["split"] == "holdout"]) == 3
    assert len(datasetSha256) == 64


def test_retrievalCalibrationFixtureCoversAdmissionBoundaryDistributions():
    """正式 calibration 必须同时约束拒绝、多记忆和语义断层场景。"""
    cases, _ = loadEvaluationCases(RETRIEVAL_CASES_PATH)
    calibrationCases = [
        case for case in cases if case["split"] == "calibration"
    ]
    abstainCases = [
        case for case in calibrationCases if not case["requiredIDs"]
    ]
    multiRequiredCases = [
        case for case in calibrationCases if len(case["requiredIDs"]) > 1
    ]
    requiredSubsets = {
        "abstain",
        "multiRequired",
        "wrongHint",
        "topicSwitch",
        "assistedRecall",
        "contradictoryNeighbor",
    }
    coveredSubsets = {
        subset
        for case in calibrationCases
        for subset in case["subsets"]
    }
    boundaryCases = [
        case for case in calibrationCases
        if requiredSubsets.intersection(case["subsets"])
    ]

    assert len(calibrationCases) >= 66
    assert len(abstainCases) >= 8
    assert all(case["allowAbstain"] for case in abstainCases)
    assert len(multiRequiredCases) >= 4
    assert requiredSubsets <= coveredSubsets
    assert all(23 <= len(case["memories"]) <= 24 for case in boundaryCases)
    assert all(case["metadata"].get("reviewStatus") for case in boundaryCases)
    assert all(case["metadata"].get("purpose") for case in boundaryCases)


def test_retrievalSmokeFixtureKeepsPinnedLabelsOutOfContextualMetrics():
    """Pinned 标注与指标必须继续由独立字段和统计口径承载。"""
    cases, _ = loadEvaluationCases(SMOKE_CASES_PATH)
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




@pytest.mark.parametrize("scores, expectedGap", [
    ({}, None),
    ({1: 0.9}, None),
    ({1: 0.9, 2: float("nan")}, None),
    ({1: 0.9, 2: 0.9}, 0.0),
    ({1: 0.9, 2: 0.2, 3: float("inf")}, 0.7),
])
def test_topMarginRequiresTwoFiniteScores(scores, expectedGap):
    """缺少竞争者不等于强证据；同分和非有限分数不得产生虚假领先。"""
    result = _topMarginEvidence(scores)
    if expectedGap is None:
        assert result["topGap"] is None
    else:
        assert result["topGap"] == pytest.approx(expectedGap)


def test_marginKeepsAllQualifiedAndTopOneControlMeasuresLoss():
    """通过 margin 后仍选两条 required；top-1 对照单独量出损失。"""
    raw = _case("multi", "calibration")
    raw["requiredIDs"] = [1, 2]
    raw["forbiddenIDs"] = []
    raw["memories"].append({
        **raw["memories"][0], "id": 3, "mode": "pinned", "content": "常驻事实",
    })
    case = validateEvaluationCases([raw])[0]
    thresholds = {"semanticCurrent": 0.8, "semanticAssisted": None, "lexical": None}
    scores = {"semanticCurrent": {1: 0.95, 2: 0.81}}
    result = _evaluateSingleCase(
        case, "hybrid+hint", thresholds, channelScores=scores,
        channelMargins={"semanticCurrent": 0.10},
    )
    topOne = _evaluateSingleCase(
        case, "hybrid+hint", thresholds, channelScores=scores, topOneOnly=True,
    )
    assert result["contextualIDs"] == [1, 2]
    assert topOne["contextualIDs"] == [1]
    assert result["pinnedIDs"] == topOne["pinnedIDs"] == [3]
    metrics = _marginStudyMetrics([_scoreCaseResult(case, topOne)])
    assert metrics["multiRequiredCompleteCount"] == 0
    assert metrics["recall"] == 0.5
    assert "qualityGate" not in metrics


def test_marginUsesBaseAndCompetitorBelowAbsoluteThreshold():
    """第二名低于绝对阈值仍参与差值；enhanced 的领先不能代替 base。"""
    case = validateEvaluationCases([_case("hint", "calibration")])[0]
    thresholds = {"semanticCurrent": 0.8, "semanticAssisted": None, "lexical": 1.0}
    result = _evaluateSingleCase(
        case, "hybrid+hint", thresholds,
        channelScores={"semanticCurrent": {1: 0.81, 2: 0.79}, "lexical": {1: 2.0}},
        semanticRankingScores={"semanticCurrent": {1: 0.99, 2: 0.1}},
        channelMargins={"semanticCurrent": 0.05},
    )
    assert result["contextualIDs"] == [1]
    evidence = result["diagnostics"]["marginEvidence"]["semanticCurrent"]
    assert evidence["topGap"] == pytest.approx(0.02)
    assert evidence["passed"] is False
    assert result["diagnostics"]["semanticCurrentQualified"] == 0
    assert result["diagnostics"]["lexicalQualified"] == 1


def test_largeMarginDoesNotBypassAbsoluteThreshold():
    """没有相关记忆时，错误第一名即使明显领先也必须受绝对阈值约束。"""
    raw = _case("no-answer", "calibration")
    raw.update(requiredIDs=[], forbiddenIDs=[1, 2], allowAbstain=True)
    case = validateEvaluationCases([raw])[0]
    result = _evaluateSingleCase(
        case, "hybrid+hint",
        {"semanticCurrent": 0.8, "semanticAssisted": None, "lexical": None},
        channelScores={"semanticCurrent": {1: 0.5, 2: 0.1}},
        channelMargins={"semanticCurrent": 0.1},
    )
    assert result["contextualIDs"] == []
    assert result["diagnostics"]["marginEvidence"]["semanticCurrent"]["passed"] is True
    metrics = _marginStudyMetrics([_scoreCaseResult(case, result)])
    assert metrics["noAnswerCaseCount"] == 1
    assert metrics["noAnswerFalseRecallCount"] == 0
    assert metrics["precision"] is None


def test_marginStudyShowsJointThresholdTradeoffsWithoutScoringHoldout(tmp_path, monkeypatch):
    """真实回放应同时暴露重校准救回的事实和 margin 删掉的共同必要事实。"""
    from scripts.llmMemory import evaluateMemory as evaluationModule

    rawCases = []
    for caseID in ("multi", "clear", "recover", "ambiguous-wrong", "clear-wrong"):
        case = _case(caseID, "calibration", currentText=caseID)
        case["metadata"] = {"reviewStatus": "draft"}
        if caseID == "multi":
            case.update(requiredIDs=[1, 2], forbiddenIDs=[])
        elif "wrong" in caseID:
            case.update(requiredIDs=[], forbiddenIDs=[1, 2], allowAbstain=True)
        rawCases.append(case)
    rawCases.append(_case("holdout", "holdout", currentText="must-not-score"))
    cases = validateEvaluationCases(rawCases)
    queries = []
    scoresByQuery = {
        "multi": {1: 0.95, 2: 0.94},
        "clear": {1: 0.9, 2: 0.1},
        "recover": {1: 0.75, 2: 0.1},
        "ambiguous-wrong": {1: 0.8, 2: 0.79},
        "clear-wrong": {1: 0.4, 2: 0.1},
    }

    def scorer(queryTexts, memories):
        queries.extend(queryTexts)
        return [scoresByQuery[query] for query in queryTexts]

    monkeypatch.setattr(evaluationModule, "scoreLexicalCandidates", lambda *args: {})
    report = studyTopMargins(
        cases, semanticScorer=scorer, manifestPath=_writeManifest(tmp_path),
        semanticMargins=(0.1,), lexicalMargins=(1.0,),
    )
    trials = {trial["variantID"]: trial for trial in report["trials"]}
    baseline = trials["absolute-only"]
    joint = trials["semanticCurrent:recalibrated:0.1"]
    assert baseline["thresholds"]["semanticCurrent"] == 0.9
    assert baseline["metrics"]["requiredHitCount"] == 3
    assert baseline["metrics"]["multiRequiredCompleteCount"] == 1
    assert "candidateEvidence" in baseline["cases"][0]["diagnostics"]
    assert trials["top-one-control"]["metrics"]["lostRequiredCount"] == 1
    assert joint["thresholds"]["semanticCurrent"] == 0.75
    assert joint["metrics"]["recoveredRequiredCount"] == 1
    assert joint["metrics"]["lostRequiredCount"] == 2
    assert joint["metrics"]["forbiddenHitCount"] == 0
    assert joint["metrics"]["multiRequiredCompleteCount"] == 0
    assert "must-not-score" not in queries
    assert report["status"] == "experimental"
    assert report["productionEligible"] is False
    assert report["reviewStatusCounts"] == {"draft": 5}
    wrong = next(
        item for item in report["channelEvidence"]["semanticCurrent"]
        if item["caseID"] == "clear-wrong"
    )
    assert wrong["topForbidden"] is True
    assert wrong["topGap"] == pytest.approx(0.3)
    assert wrong["passesBaselineAbsolute"] is False
    serialized = json.dumps(report, ensure_ascii=False, allow_nan=False)
    assert "qualityGate" not in serialized
    assert rawCases[0]["memories"][0]["content"] not in serialized


@pytest.mark.parametrize("grid", [(), (-0.1,), (0.0,), (float("nan"),), (float("inf"),)])
def test_marginStudyRejectsInvalidGridBeforeScoring(grid):
    """实验参数必须可解释且可写入严格 JSON。"""
    with pytest.raises(EvaluationError, match="有限正数"):
        studyTopMargins([], semanticMargins=grid)


def test_marginStudyRequiresBothBoundaryDistributions():
    """缺失无答案或多 required 时不能产出具有误导性的实验报告。"""
    cases = validateEvaluationCases([_case("ordinary", "calibration")])
    with pytest.raises(EvaluationError, match="无答案和多 required"):
        studyTopMargins(cases)


@pytest.mark.parametrize("target", ["fixture", "manifest", "calibration"])
def test_marginCliProtectsInputsBeforeLoadingEncoder(tmp_path, monkeypatch, target):
    """报告不能覆盖输入或正式阈值，路径错误须在加载 encoder 前发现。"""
    from scripts.llmMemory import evaluateMemory as evaluationModule

    casesPath = tmp_path / "cases.json"
    casesPath.write_text("input sentinel", encoding="utf-8")
    outputPath = casesPath if target == "fixture" else tmp_path / f"{target}.json"
    outputPath.write_text("input sentinel", encoding="utf-8")
    monkeypatch.setattr(evaluationModule, "LLM_MEMORY_CALIBRATION_PATH", tmp_path / "calibration.json")
    monkeypatch.setattr("sys.argv", [
        "evaluateMemory.py", "margin", "--cases", str(casesPath),
        "--manifest", str(tmp_path / "manifest.json"),
        "--output", str(outputPath),
    ])
    monkeypatch.setattr(evaluationModule, "_createScorer", lambda *args: pytest.fail("loaded encoder"))
    assert evaluationModule.main() == 1
    assert outputPath.read_text(encoding="utf-8") == "input sentinel"
