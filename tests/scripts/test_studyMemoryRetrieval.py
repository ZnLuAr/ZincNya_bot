"""实验必须隔离 holdout、分组与 hint，不能据此批准上线。"""

import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from scripts import studyMemoryRetrieval as study
from tests.scripts.test_evaluateMemory import _case


def _record(*, sameQuery=False):
    """构造 hint 高分但 current 基础证据不足的候选。"""
    return {
        "sameQuery": sameQuery,
        "scores": {"semanticCurrent": {1: 0.1, 2: 0.4}, "semanticAssisted": {1: 0.9, 2: 0.8}, "lexical": {}},
        "ranking": {"semanticCurrent": {1: 0.95, 2: 0.95}, "semanticAssisted": {1: 0.95, 2: 0.95}},
    }


def test_loadCalibrationCasesDoesNotValidateHoldout(tmp_path):
    """损坏 holdout 也不应影响仅 calibration 的实验。"""
    cases = [_case("multi", "calibration"), _case("empty", "calibration")]
    cases[0].update(requiredIDs=[1, 2], forbiddenIDs=[])
    cases[1].update(requiredIDs=[], forbiddenIDs=[1, 2], allowAbstain=True)
    cases.append({"split": "holdout", "answers": "must-not-be-read"})
    path = tmp_path / "cases.json"
    path.write_text(json.dumps({"cases": cases}), encoding="utf-8")
    assert len(study.loadCalibrationCases(path)) == 2


def test_admissionScoresUsesBaseWithoutMutation():
    """高 enhanced 不能绕过 current base；原始分数仍供下一组对照。"""
    record = _record()
    assert study.admissionScores(record, 0.3)["semanticAssisted"] == {2: 0.8}
    assert record["scores"]["semanticAssisted"] == {1: 0.9, 2: 0.8}
    assert study.admissionScores(record, None) == record["scores"]


def test_sameTextDoesNotApplyHistoryGuard():
    """同文本接管不是历史扩展，不应被附加门槛误伤。"""
    record = _record(sameQuery=True)
    assert study.admissionScores(record, 0.5) == record["scores"]


def test_replayDeduplicatesSameTextButAllowsFallback(monkeypatch):
    """current 关闭时允许 assisted 接管；开启时仅留一份证据。"""
    case = study.evaluation.validateEvaluationCases([_case("same", "calibration")])[0]
    record = _record(sameQuery=True)
    seen = []

    def capture(case, mode, thresholds, **kwargs):
        seen.append(kwargs["channelScores"])
        return {}

    monkeypatch.setattr(study.evaluation, "_evaluateSingleCase", capture)
    monkeypatch.setattr(study.evaluation, "_scoreCaseResult", lambda case, result: result)
    for threshold in (0.5, None):
        study.replayCases([case], {"same": record}, {"semanticCurrent": threshold, "semanticAssisted": 0.5, "lexical": None}, None)
    assert seen[0]["semanticAssisted"] == {}
    assert seen[1]["semanticAssisted"] == record["scores"]["semanticAssisted"]


def test_relatedCasesStayInSameFold(monkeypatch):
    """同主题问法不跨折，共享背景仍需披露，输入顺序不改变分折。"""
    rawCases = [_case(f"case-{index}", "calibration") for index in range(12)]
    for index, case in enumerate(rawCases):
        case["groupID"] = f"group-{index // 2}"
    cases = study.evaluation.validateEvaluationCases(rawCases)
    trained = []
    tested = []

    def calibrate(cases, records, floor):
        trained.append({case["groupID"] for case in cases})
        return {}

    def replay(cases, records, thresholds, floor):
        tested.append({case["groupID"] for case in cases})
        return []

    monkeypatch.setattr(study, "chooseThresholds", calibrate)
    monkeypatch.setattr(study, "replayCases", replay)
    monkeypatch.setattr(study, "summarizeResults", lambda results: {})
    report = study.studyScores(cases, {}, None)
    assert len(report["folds"]) == 5
    assert sum(fold["testCaseCount"] for fold in report["folds"]) == len(cases)
    for trainGroups, testGroups in zip(trained[1:], tested[1:]):
        assert not trainGroups.intersection(testGroups)
        assert trainGroups | testGroups == trained[0]
    for fold, trainGroups, testGroups in zip(report["folds"], trained[1:], tested[1:]):
        assert fold["trainGroupIDs"] == sorted(trainGroups)
        assert fold["testGroupIDs"] == sorted(testGroups)
        assert fold["sharedCandidateMemoryCount"] == 2
        assert fold["sharedCandidateContentCount"] == 2
    assert "不是事实独立验证" in report["validationNote"]
    assert study.studyScores(list(reversed(cases)), {}, None)["folds"] == report["folds"]


def test_plainRepresentationKeepsHintOutOfBase():
    """格式变化仍必须遵守双表示安全边界。"""
    encoder = object.__new__(study.StudyEncoder)
    encoder.variant = "plain"
    memory = {"content": "这是正文", "tags": ["标签"], "retrievalHint": "仅增强"}
    assert encoder._formatMemoryText(memory, includeHint=False) == "这是正文\n标签"
    assert encoder._formatMemoryText(memory, includeHint=True) == "这是正文\n标签\n仅增强"


def test_candidateOverlapDistinguishesReusedIDsFromReusedContent(monkeypatch):
    """同 ID 的不同正文不算同一候选，换 ID 的相同正文仍要暴露。"""
    rawCases = [_case(f"case-{index}", "calibration") for index in range(5)]
    for index, case in enumerate(rawCases):
        case["memories"][0]["id"] = 100 + index
        case["requiredIDs"] = [100 + index]
        case["memories"][1]["content"] = f"每组独有事实{index}"
    cases = study.evaluation.validateEvaluationCases(rawCases)
    monkeypatch.setattr(study, "chooseThresholds", lambda cases, records, floor: {})
    monkeypatch.setattr(study, "replayCases", lambda cases, records, thresholds, floor: [])
    report = study.studyScores(cases, {}, None)
    for fold in report["folds"]:
        assert fold["sharedCandidateMemoryCount"] == 0
        assert fold["sharedCandidateContentCount"] == 1
    for section in ("inSample", "groupValidation"):
        assert set(report[section]["cohorts"]) == {"oldCalibration", "expandedCalibration"}


@pytest.mark.parametrize("target", ["cases", "calibration", "manifest"])
def test_cliProtectsInputsBeforeLoadingEncoder(tmp_path, monkeypatch, target):
    """输出路径错误时在读输入和加载模型之前拒绝。"""
    paths = {name: tmp_path / f"{name}.json" for name in ("cases", "calibration", "manifest")}
    monkeypatch.setattr(study, "LLM_MEMORY_CALIBRATION_PATH", paths["calibration"])
    monkeypatch.setattr(study, "LLM_MEMORY_MODEL_MANIFEST_PATH", paths["manifest"])
    monkeypatch.setattr(study, "StudyEncoder", lambda: pytest.fail("loaded encoder"))
    monkeypatch.setattr("sys.argv", ["studyMemoryRetrieval.py", "--cases", str(paths["cases"]), "--output", str(paths[target])])
    assert study.main() == 1


def test_pairScorerShortlistsBaseAndNeverSendsHint():
    """高 hint 不能挤入短名单，成对模型也只能看到正文与标签。"""
    encoder = object.__new__(study.StudyEncoder)
    encoder.variant = "baseline"
    memories = [{"id": index, "content": f"正文{index}", "tags": ["标签"], "retrievalHint": "不能发送"} for index in (1, 2, 3)]
    seen = []

    def rank(pairs):
        seen.extend(pairs)
        return [0.7, 0.8]

    dense = SimpleNamespace(encoder=encoder, score=lambda texts, candidates: [{"base": {1: 0.9, 2: 0.8, 3: 0.1}, "enhanced": {1: 0.8, 2: 0.7, 3: 0.99}}])
    scorer = study.StudyPairScorer(dense, SimpleNamespace(scorePairs=rank), 2)
    result = scorer.score(["问题"], memories)[0]
    assert result["base"] == {1: 0.7, 2: 0.8}
    assert result["enhanced"][3] == 0.99
    assert len(seen) == 2
    assert all("不能发送" not in passage for query, passage in seen)
    assert all("标签" in passage for query, passage in seen)


@pytest.mark.parametrize("logits,valid", [([[0.0], [2.0]], True), ([[float('nan')], [0.0]], False), ([[0.0, 1.0], [0.0, 1.0]], False)])
def test_pairLogitsMustBeFiniteScalarPerPair(logits, valid):
    """形状或数值错误不能伪装成可校准的相关性分数。"""
    numpy = pytest.importorskip("numpy")
    ranker = object.__new__(study.StudyReranker)
    ranker.numpy = numpy
    ranker.manifest = {"maxTokens": 256}
    ranker.timings = []
    ranker.pairCount = 0
    encoded = SimpleNamespace(ids=[1, 2], attention_mask=[1, 1], type_ids=[0, 0])
    ranker.tokenizer = SimpleNamespace(encode_batch=lambda pairs: [encoded] * len(pairs))
    ranker.session = SimpleNamespace(
        get_inputs=lambda: [SimpleNamespace(name="input_ids", type="tensor(int64)")],
        run=lambda names, inputs: [numpy.asarray(logits)],
    )
    if valid:
        assert ranker.scorePairs([("问题", "正文"), ("问题", "事实")]) == pytest.approx([0.5, 0.880797])
        assert ranker.pairCount == 2
    else:
        with pytest.raises(study.evaluation.EvaluationError, match="logit"):
            ranker.scorePairs([("问题", "正文"), ("问题", "事实")])


def test_shortlistCoverageCountsAllRequiredWithoutPromotingAllowed():
    """短名单覆盖率不能把 allowed 当 required，也不能靠同通道重复计数。"""
    cases = [{"caseID": "multi", "requiredIDs": {1, 2, 3}}]
    records = {"multi": {"scores": {"semanticCurrent": {1: 0.5}, "semanticAssisted": {1: 0.8, 2: 0.4, 4: 0.9}}}}
    report = study.shortlistCoverage(cases, records)
    assert report["coverage"] == 2 / 3
    assert report["missedCases"] == [{"caseID": "multi", "requiredNotScoredIDs": [3]}]


def test_pairManifestRejectsBadHashBeforeImportingModelStack(tmp_path, monkeypatch):
    """公开下载也必须先验散列；坏产物不能交给 ONNX 解析。"""
    artifact = tmp_path / "model.onnx"
    artifact.write_bytes(b"not-a-model")
    manifest = {"schemaVersion": 1, "task": "text-pair-relevance", "modelFile": "model.onnx", "tokenizerFile": "model.onnx", "artifacts": [{"path": "model.onnx", "size": artifact.stat().st_size, "sha256": "0" * 64}]}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(study.MemoryEncoder, "_importOptionalDependency", lambda name: pytest.fail("loaded model stack"))
    with pytest.raises(study.evaluation.EvaluationError, match="校验失败"):
        study.StudyReranker(tmp_path, path)


@pytest.mark.parametrize("currentText,replyText,currentThreshold", [
    ("当前问题", "", 0.7),
    ("当前问题", "", None),
    ("当前问题", "引用背景", 0.7),
    ("", "", 0.7),
])
def test_baselineMatchesOfficialEvaluation(currentText, replyText, currentThreshold):
    """差分比较完整选择链路，锁住同文本、接管、不同查询和空查询。"""
    rawCase = _case("parity", "calibration", currentText=currentText)
    rawCase["query"]["replyText"] = replyText
    cases = study.evaluation.validateEvaluationCases([rawCase])

    def scorer(texts, candidates):
        return [{"base": {1: 0.8, 2: 0.6}, "enhanced": {1: 0.7, 2: 0.99}} for text in texts]

    thresholds = {"semanticCurrent": currentThreshold, "semanticAssisted": 0.7, "lexical": None}
    records = study.collectScores(cases, SimpleNamespace(score=scorer))
    actual = study.replayCases(cases, records, thresholds, None)
    expected = study.evaluation.evaluateRetrievalCases(
        cases, thresholds, semanticScorer=scorer, split="calibration", modes=("hybrid+hint",),
    )["modes"]["hybrid+hint"]["cases"]
    for result in expected:
        result.pop("diagnostics", None)
    assert actual == expected


def test_filteredAssistedStillNeedsItsAbsoluteThreshold():
    """当前支持只是一道附加条件，不能代替 assisted 自己的基础阈值。"""
    case = study.evaluation.validateEvaluationCases([_case("guard", "calibration")])[0]
    thresholds = {"semanticCurrent": None, "semanticAssisted": 0.85, "lexical": None}
    result = study.replayCases([case], {"guard": _record()}, thresholds, 0.4)[0]
    assert result["contextualIDs"] == []
    assert study.admissionScores(_record(), 0.4)["semanticAssisted"] == {2: 0.8}


def test_metricsDoNotRewardAbstentionOrAllowedOnlyAsRequiredRecall():
    """弃权不创造 precision，多 required 完整率不能用 allowed 命中代替。"""
    rawCases = [_case("multi", "calibration"), _case("allowed", "calibration"), _case("empty", "calibration")]
    rawCases[0].update(requiredIDs=[1, 2], forbiddenIDs=[])
    rawCases[1].update(requiredIDs=[], allowedIDs=[1])
    rawCases[2].update(requiredIDs=[], forbiddenIDs=[1, 2], allowAbstain=True)
    cases = study.evaluation.validateEvaluationCases(rawCases)
    records = {case["caseID"]: _record() for case in cases}
    thresholds = {name: None for name in study.evaluation.CHANNEL_NAMES}
    metrics = study.summarizeResults(study.replayCases(cases, records, thresholds, None))
    assert metrics["precision"] is None
    assert metrics["recall"] == 0
    assert metrics["noAnswerCaseCount"] == 1
    assert metrics["noAnswerFalseRecallCount"] == 0
    assert metrics["multiRequiredCaseCount"] == 1
    assert metrics["multiRequiredCompleteCount"] == 0
    assert "qualityGate" not in metrics


@pytest.mark.parametrize("expanded", [False, True])
def test_cohortMetricsKeepErrorsAndDenominatorsSeparate(expanded):
    """交换 cohort 归属时，完整召回和无答案误召回必须随原场景移动。"""
    rawCases = [_case("multi", "calibration"), _case("allowed", "calibration"), _case("empty", "calibration")]
    rawCases[0].update(requiredIDs=[1, 2], forbiddenIDs=[])
    rawCases[1].update(requiredIDs=[], allowedIDs=[1])
    rawCases[2].update(requiredIDs=[], forbiddenIDs=[1, 2], allowAbstain=True)
    for index, case in enumerate(rawCases):
        case["subsets"] = ["expandedCalibration"] if (index == 0) == expanded else []
    cases = study.evaluation.validateEvaluationCases(rawCases)
    selected = ([1, 2], [1], [2])
    results = [
        study.evaluation._scoreCaseResult(case, {"contextualIDs": predictedIDs})
        for case, predictedIDs in zip(cases, selected)
    ]
    metrics = study.summarizeResults(results)
    complete = metrics["cohorts"]["expandedCalibration" if expanded else "oldCalibration"]
    errors = metrics["cohorts"]["oldCalibration" if expanded else "expandedCalibration"]
    assert complete["caseCount"] == 1
    assert complete["precision"] == 1
    assert complete["recall"] == 1
    assert complete["forbiddenHitCount"] == 0
    assert complete["multiRequiredCaseCount"] == 1
    assert complete["multiRequiredCompleteRate"] == 1
    assert complete["noAnswerCaseCount"] == 0
    assert errors["caseCount"] == 2
    assert errors["precision"] == 0.5
    assert errors["recall"] is None
    assert errors["forbiddenHitCount"] == 1
    assert errors["multiRequiredCompleteRate"] is None
    assert errors["noAnswerCaseCount"] == 1
    assert errors["noAnswerFalseRecallRate"] == 1
    assert metrics["cohorts"]["expandedCalibration"] == metrics["subsets"]["expandedCalibration"]
    assert all("qualityGate" not in cohort for cohort in metrics["cohorts"].values())


def test_absentCohortKeepsMetricsUndefined():
    """旧 fixture 无扩充题时保留空 cohort，不能把零预测写成满分。"""
    case = study.evaluation.validateEvaluationCases([_case("old", "calibration")])[0]
    result = study.evaluation._scoreCaseResult(case, {"contextualIDs": []})
    metrics = study.summarizeResults([result])["cohorts"]["expandedCalibration"]
    assert metrics["caseCount"] == 0
    assert metrics["precision"] is None
    assert metrics["recall"] is None
    assert metrics["noAnswerFalseRecallRate"] is None
    assert metrics["multiRequiredCompleteRate"] is None


@pytest.mark.parametrize("target", ["manifest", "model"])
def test_cliProtectsExperimentalModelPaths(tmp_path, monkeypatch, target):
    """自选模型不应成为输出文件，实验也不能损坏已校验的模型产物。"""
    manifest = tmp_path / "manifest.json"
    modelDir = tmp_path / "model"
    output = manifest if target == "manifest" else modelDir / "onnx" / "model.onnx"
    monkeypatch.setattr(study, "StudyEncoder", lambda **kwargs: pytest.fail("loaded encoder"))
    monkeypatch.setattr("sys.argv", [
        "studyMemoryRetrieval.py", "--cases", str(tmp_path / "cases.json"),
        "--manifest", str(manifest), "--model-dir", str(modelDir), "--output", str(output),
    ])
    assert study.main() == 1


def test_candidateUnionUsesBaseAndPositiveLexicalWithPerChannelBudget():
    """互补事实进入并集；高 hint、词面零分和非有限分不能挤占候选。"""
    record = _record()
    record["scores"] = {
        "semanticCurrent": {1: 0.8, 2: 0.1, 4: float("nan")},
        "semanticAssisted": {1: 0.2, 2: 0.9},
        "lexical": {3: 1.0, 4: 0.0, 5: -1.0, 6: float("inf")},
    }
    record["ranking"] = {"semanticCurrent": {4: 1.0}}
    assert study.selectCandidateIDs(record, "dense", 1) == {1, 2}
    assert study.selectCandidateIDs(record, "lexical", 8) == {3}
    assert study.selectCandidateIDs(record, "union", 1) == {1, 2, 3}


def test_candidateTieOrderAndSameQueryAreDeterministic():
    """相同查询只用一份 dense 候选，分数相同时按 ID 稳定排序。"""
    record = _record(sameQuery=True)
    record["scores"]["semanticCurrent"] = {2: 0.8, 1: 0.8}
    record["scores"]["semanticAssisted"] = {3: 1.0}
    assert study.selectCandidateIDs(record, "dense", 1) == {1}
    record["scores"]["semanticCurrent"] = {1: 0.8, 2: 0.8}
    assert study.selectCandidateIDs(record, "dense", 1) == {1}
    record["scores"] = {channel: {} for channel in study.evaluation.CHANNEL_NAMES}
    assert study.selectCandidateIDs(record, "union", 8) == set()


@pytest.mark.parametrize("strategy,limit", [("bad", 8), ("dense", 0), ("union", -1)])
def test_candidateSelectionRejectsInvalidConfiguration(strategy, limit):
    """无效预算不能悄悄变成 Python 负切片或不明候选规则。"""
    with pytest.raises(study.evaluation.EvaluationError):
        study.selectCandidateIDs(_record(), strategy, limit)


def test_candidateCoverageSeparatesAllowedPartialAndNoAnswer():
    """allowed 不计必要覆盖，纯无答案候选与多事实完整覆盖分开统计。"""
    cases = [
        {"caseID": "multi", "requiredIDs": {1, 2}, "allowedIDs": {3}, "forbiddenIDs": {4}},
        {"caseID": "partial", "requiredIDs": set(), "allowedIDs": {3}, "forbiddenIDs": {4}},
        {"caseID": "empty", "requiredIDs": set(), "allowedIDs": set(), "forbiddenIDs": {4}},
    ]
    selected = {"multi": {1, 3}, "partial": {3}, "empty": {4}}
    metrics = study.candidateCoverageMetrics(cases, selected)
    assert metrics["requiredCoverage"] == 0.5
    assert metrics["multiRequiredCompleteCoverageCount"] == 0
    assert metrics["forbiddenCandidateCount"] == 1
    assert metrics["noAnswerCaseCount"] == metrics["noAnswerWithCandidatesCount"] == 1
    assert metrics["candidateCountTotal"] == 4
    assert metrics["missedCases"] == [{"caseID": "multi", "missingRequiredIDs": [2]}]
    assert "precision" not in metrics
    assert study.candidateCoverageMetrics([], {})["requiredCoverage"] is None


def test_candidateGateDisablesLexicalBypassWithoutMutation():
    """词面高分只负责入池，不能越过统一的 dense 准入门槛。"""
    case = study.evaluation.validateEvaluationCases([_case("gate", "calibration")])[0]
    record = _record(sameQuery=True)
    record["scores"]["lexical"] = {2: 999.0}
    records = {"gate": record}
    original = deepcopy(records)
    gated = study.restrictToCandidateGate(records, {"gate": {2}})
    thresholds = {"semanticCurrent": 0.5, "semanticAssisted": None, "lexical": 0.1}
    result = study.replayCases([case], gated, thresholds, None)[0]
    assert result["contextualIDs"] == []
    assert gated["gate"]["scores"]["semanticCurrent"] == {2: 0.4}
    assert records == original
    gated["gate"]["ranking"]["semanticCurrent"][1] = 0
    assert records == original


def test_candidateGridKeepsCohortsAndFoldsWithoutSelectingWinner():
    """固定九组对照复用相同分折，并保持新旧覆盖分母及错误分离。"""
    rawCases = [_case(f"case-{index}", "calibration") for index in range(5)]
    rawCases[0]["subsets"] = ["expandedCalibration"]
    cases = study.evaluation.validateEvaluationCases(rawCases)
    records = {case["caseID"]: _record(sameQuery=True) for case in cases}
    original = deepcopy(records)
    trials = study.studyCandidatePools(cases, records)
    assert len(trials) == 9
    for trial in trials:
        coverage = trial["coverage"]
        assert coverage["cohorts"]["oldCalibration"]["requiredCount"] == 4
        assert coverage["cohorts"]["expandedCalibration"]["requiredCount"] == 1
        assert coverage["subsets"]["expandedCalibration"] == coverage["cohorts"]["expandedCalibration"]
        assert trial["denseGate"]["thresholds"]["lexical"] is None
        for actual, expected in zip(trial["denseGate"]["folds"], trials[0]["denseGate"]["folds"]):
            assert actual["trainGroupIDs"] == expected["trainGroupIDs"]
            assert actual["testGroupIDs"] == expected["testGroupIDs"]
    assert records == original


@pytest.mark.parametrize("extra", [[], ["--variants", "baseline", "--reranker-manifest", "pair.json"]])
def test_candidateCliRejectsIncompatibleModesBeforeModelLoad(monkeypatch, extra):
    """候选实验不能无意扫描编码变体或与历史重排模式混合。"""
    monkeypatch.setattr(study, "StudyEncoder", lambda **kwargs: pytest.fail("loaded encoder"))
    monkeypatch.setattr("sys.argv", ["studyMemoryRetrieval.py", "--cases", "unused.json", "--output", "unused-report.json", "--candidate-study", *extra])
    assert study.main() == 1



def _pairFixture(*, sameQuery=True):
    """构造通道各自命中不同事实的 fixture，避免用标签决定缩池。"""
    rawCase = _case("pairs", "calibration", hint="PRIVATE_HINT_MARKER")
    rawCase["memories"].append({
        "id": 3, "scope_type": "global", "scope_id": "global",
        "content": "第三条独立事实", "tags": ["独立标签"],
    })
    rawCase.update(requiredIDs=[3], allowedIDs=[1], forbiddenIDs=[2])
    rawCase["subsets"] = ["expandedCalibration", "pairFixture"]
    if not sameQuery:
        rawCase["query"]["replyText"] = "引用上下文"
    cases = study.evaluation.validateEvaluationCases([rawCase])
    records = {"pairs": {
        "sameQuery": sameQuery,
        "scores": {
            "semanticCurrent": {1: 0.9, 2: 0.2, 3: 0.1},
            "semanticAssisted": {1: 0.1, 2: 0.8, 3: 0.2},
            "lexical": {3: 99.0},
        },
        "ranking": {"semanticCurrent": {2: 999.0}, "semanticAssisted": {3: 999.0}},
    }}
    encoder = object.__new__(study.StudyEncoder)
    encoder.variant = "baseline"
    return cases, records, encoder


def _memoryProcess(rss=100, peak=120):
    """用无副作用的进程替身检查预算，避免读取真实机器负载。"""
    return SimpleNamespace(memory_info=lambda: SimpleNamespace(rss=rss, peak_wset=peak))


@pytest.mark.parametrize("sameQuery", [True, False])
def test_candidatePairScoresKeepHintsOutAndInputsUnchanged(sameQuery):
    """各通道独立缩池、同查询去重；词面不得绕过逐对评分。"""
    cases, records, encoder = _pairFixture(sameQuery=sameQuery)
    originalCases, originalRecords = deepcopy(cases), deepcopy(records)
    seen = []

    def rank(pairs):
        """记录实际送入的文本和批量，返回固定有限分。"""
        seen.append(pairs)
        return [0.4]

    result, coverage = study.collectCandidatePairScores(
        cases, records, SimpleNamespace(scorePairs=rank), encoder, 1,
        process=_memoryProcess(), rssSamples=[],
    )
    expectedIDs = {1, 3} if sameQuery else {1, 2, 3}
    assert len(seen) == len(expectedIDs) * (1 if sameQuery else 2)
    assert all(len(batch) == 1 for batch in seen)
    assert all("PRIVATE_HINT_MARKER" not in passage for batch in seen for query, passage in batch)
    assert any("独立标签" in passage for batch in seen for query, passage in batch)
    assert result["pairs"]["scores"]["lexical"] == {}
    assert set(result["pairs"]["scores"]["semanticCurrent"]) == expectedIDs
    assert result["pairs"]["ranking"] == {
        name: result["pairs"]["scores"][name] for name in ("semanticCurrent", "semanticAssisted")
    }
    assert coverage["requiredCoverage"] == 1
    assert coverage["cohorts"]["expandedCalibration"]["requiredCount"] == 1
    assert coverage["cohorts"]["oldCalibration"]["requiredCount"] == 0
    assert coverage["subsets"]["pairFixture"]["requiredCoverage"] == 1
    assert (cases, records) == (originalCases, originalRecords)
    # 输出通道也应相互隔离，避免 replay 修改一个通道时污染另一个通道。
    result["pairs"]["scores"]["semanticCurrent"][1] = 0
    assert result["pairs"]["scores"]["semanticAssisted"][1] == 0.4
    assert (cases, records) == (originalCases, originalRecords)


def test_candidatePairSelectionDoesNotReadLabels():
    """交换 required 与 forbidden 只改变覆盖报告，不改变送入模型的候选。"""
    cases, records, encoder = _pairFixture(sameQuery=False)
    seen = []

    def rank(pairs):
        """保存本次候选文本以比较交换标签前后的调用。"""
        seen.extend(pairs)
        return [0.5]

    kwargs = {"process": _memoryProcess(), "rssSamples": []}
    first, _ = study.collectCandidatePairScores(cases, records, SimpleNamespace(scorePairs=rank), encoder, 1, **kwargs)
    originalPairs = list(seen)
    cases[0]["requiredIDs"], cases[0]["forbiddenIDs"] = cases[0]["forbiddenIDs"], cases[0]["requiredIDs"]
    seen.clear()
    second, _ = study.collectCandidatePairScores(cases, records, SimpleNamespace(scorePairs=rank), encoder, 1, **kwargs)
    assert seen == originalPairs
    assert second == first


@pytest.mark.parametrize("scores", [[], [0.3, 0.4], [float("nan")], [float("inf")]])
def test_candidatePairScoresRejectMalformedModelOutput(scores):
    """逐对输出必须是单个有限值，损坏输出不得用于阈值校准。"""
    cases, records, encoder = _pairFixture()
    with pytest.raises(study.evaluation.EvaluationError, match="有限分数"):
        study.collectCandidatePairScores(
            cases, records, SimpleNamespace(scorePairs=lambda pairs: scores), encoder, 1,
            process=_memoryProcess(), rssSamples=[],
        )


@pytest.mark.parametrize("emptyQuery", [True, False])
def test_candidatePairScoresHandleEmptyQueryOrPool(emptyQuery):
    """空查询或空候选均不调用模型，同时保留零覆盖及空分数。"""
    cases, records, encoder = _pairFixture()
    if emptyQuery:
        cases[0]["query"] = study.evaluation.validateEvaluationCases([
            _case("empty-query", "calibration", currentText=""),
        ])[0]["query"]
    else:
        records["pairs"]["scores"] = {name: {} for name in study.evaluation.CHANNEL_NAMES}
    result, coverage = study.collectCandidatePairScores(
        cases, records, SimpleNamespace(scorePairs=lambda pairs: pytest.fail("unexpected pair call")), encoder, 1,
        process=_memoryProcess(), rssSamples=[],
    )
    assert all(not values for values in result["pairs"]["scores"].values())
    if not emptyQuery:
        assert coverage["candidateCountTotal"] == 0
        assert coverage["requiredCoverage"] == 0


def test_candidatePairScoresRejectOutOfScopeCandidateBeforeModel():
    """记录中混入当前场景不可检索的 ID 时必须终止，不能直接格式化评分。"""
    cases, records, encoder = _pairFixture()
    records["pairs"]["scores"]["lexical"] = {999: 100.0}
    with pytest.raises(study.evaluation.EvaluationError, match="不可检索"):
        study.collectCandidatePairScores(
            cases, records, SimpleNamespace(scorePairs=lambda pairs: pytest.fail("unexpected pair call")), encoder, 1,
            process=_memoryProcess(), rssSamples=[],
        )


@pytest.mark.parametrize("rss,peak", [(0, None), (512 * 1024 * 1024, None), (10, 512 * 1024 * 1024)])
def test_pairMemoryBudgetAcceptsBoundaryAndKeepsRssMeaning(rss, peak):
    """恰好达到预算可继续；RSS 序列不能混入历史峰值。"""
    samples = []
    process = _memoryProcess(rss, peak) if peak is not None else SimpleNamespace(memory_info=lambda: SimpleNamespace(rss=rss))
    study.checkPairStudyMemory(process, samples)
    assert samples == [rss]


@pytest.mark.parametrize("rss,peak", [(512 * 1024 * 1024 + 1, None), (10, 512 * 1024 * 1024 + 1)])
def test_pairMemoryBudgetReportsCurrentAndHistoricalExcess(rss, peak):
    """当前值或 Windows 历史峰值超限均携带结构化观测值终止。"""
    samples = []
    with pytest.raises(study.PairStudyMemoryError) as caught:
        study.checkPairStudyMemory(_memoryProcess(rss, peak), samples)
    assert caught.value.rssBytes == rss
    assert caught.value.processPeakWorkingSet == peak
    assert caught.value.limitBytes == study.PAIR_STUDY_MEMORY_LIMIT
    assert samples == [rss]


@pytest.mark.parametrize("rss,peak", [(None, None), (-1, None), (float("nan"), None), (100, -1), (100, float("inf"))])
def test_pairMemoryBudgetRejectsUnknownOrInvalidObservations(rss, peak):
    """未知或无效观测不能被报告为预算内，也不能写入 RSS 采样序列。"""
    samples = []
    with pytest.raises(study.evaluation.EvaluationError):
        study.checkPairStudyMemory(_memoryProcess(rss, peak), samples)
    assert samples == []


def test_pairMemoryBudgetRejectsObservationFailure():
    """进程访问失败时停止受限实验，而非把未知内存当作零。"""
    def readMemory():
        """模拟进程在采样时消失。"""
        raise OSError("process gone")

    with pytest.raises(study.evaluation.EvaluationError, match="无法读取"):
        study.checkPairStudyMemory(SimpleNamespace(memory_info=readMemory), [])


@pytest.mark.parametrize("safeReads,expectedCalls", [(0, 0), (1, 0), (2, 1)])
def test_candidatePairScoresStopBeforeAndAfterOverBudgetInference(safeReads, expectedCalls):
    """入口、推理前或推理后超限均停止，不再启动下一次成对计算。"""
    cases, records, encoder = _pairFixture()
    observations = iter([100] * safeReads + [study.PAIR_STUDY_MEMORY_LIMIT + 1])
    process = SimpleNamespace(memory_info=lambda: SimpleNamespace(rss=next(observations)))
    calls = []

    def rank(pairs):
        """记录调用次数以检查超限后的停止位置。"""
        calls.append(pairs)
        return [0.5]

    with pytest.raises(study.PairStudyMemoryError):
        study.collectCandidatePairScores(
            cases, records, SimpleNamespace(scorePairs=rank), encoder, 1,
            process=process, rssSamples=[],
        )
    assert len(calls) == expectedCalls



def _studyCliHarness(tmpPath, monkeypatch, extra):
    """替换计算和进程观测，保留真实 fixture 读取、研究分折及报告写入。"""
    cases = [_case(f"cli-{index}", "calibration", hint="CLI_HINT_MARKER") for index in range(5)]
    cases[0].update(requiredIDs=[1, 2], forbiddenIDs=[], subsets=["expandedCalibration", "multiRequired"])
    cases[1].update(requiredIDs=[], forbiddenIDs=[1, 2], allowAbstain=True, subsets=["expandedCalibration", "noAnswer"])
    path = tmpPath / "cases.json"
    # 这个损坏的合成 holdout 不应进入校验、评分、摘要或交叉验证。
    path.write_text(json.dumps({"cases": cases + [{"split": "holdout", "bad": "IGNORE_HOLDOUT"}]}), encoding="utf-8")
    manifest = tmpPath / "manifest.json"
    manifest.write_text(json.dumps({"revision": "fake-dense-revision", "encodingVersion": "fake-encoding"}), encoding="utf-8")
    pairManifest = tmpPath / "pair-manifest.json"
    pairManifest.write_text(json.dumps({"repository": "fake/pair", "revision": "fake-pair-revision"}), encoding="utf-8")
    output = tmpPath / "report.json"
    events, batches = [], []
    formatter = object.__new__(study.StudyEncoder)
    formatter.variant = "baseline"
    encoder = SimpleNamespace(
        _manifest=json.loads(manifest.read_text(encoding="utf-8")), variant="baseline",
        _formatMemoryText=formatter._formatMemoryText,
        close=lambda: events.append("encoder.close"),
    )

    def makeEncoder(**kwargs):
        """记录 dense 模型生命周期；没有真实模型初始化。"""
        events.append("encoder.load")
        return encoder

    def denseScore(texts, candidates):
        """假分数覆盖所有候选，不从 required 或 forbidden 取分。"""
        events.append("dense.score")
        return [{"base": {int(memory["id"]): 0.9 / int(memory["id"]) for memory in candidates},
                 "enhanced": {int(memory["id"]): 0.95 for memory in candidates}} for text in texts]

    reranker = SimpleNamespace(
        manifest=json.loads(pairManifest.read_text(encoding="utf-8")), pairCount=0, timings=[],
        close=lambda: events.append("pair.close"),
    )

    def pairScore(pairs):
        """逐对返回固定分数，只观察实际输入，不使用答案标签。"""
        events.append("pair.score")
        batches.append(pairs)
        reranker.pairCount += len(pairs)
        reranker.timings.append(0.001)
        return [0.8 for pair in pairs]

    def makeReranker(modelDir, manifestPath):
        """记录 pair 加载，以验证 dense 已释放。"""
        events.append("pair.load")
        return reranker

    reranker.scorePairs = pairScore
    monkeypatch.setattr(study, "StudyEncoder", makeEncoder)
    monkeypatch.setattr(study, "StudyReranker", makeReranker)
    monkeypatch.setattr(study.evaluation, "EncoderSemanticScorer", lambda encoder: SimpleNamespace(
        score=denseScore, clear=lambda: events.append("scorer.clear"),
    ))
    monkeypatch.setattr(study.evaluation.psutil, "Process", _memoryProcess)
    argv = ["studyMemoryRetrieval.py", "--cases", str(path), "--output", str(output),
            "--manifest", str(manifest), "--model-dir", str(tmpPath / "dense-model"), "--variants", "baseline"]
    if "--two-stage-reranker" in extra:
        argv.extend(["--reranker-manifest", str(pairManifest), "--reranker-dir", str(tmpPath / "pair-model"), "--shortlist", "2"])
    monkeypatch.setattr("sys.argv", argv + extra)
    return SimpleNamespace(cases=cases, path=path, manifest=manifest, pairManifest=pairManifest,
                           output=output, events=events, batches=batches, reranker=reranker)


def test_candidateCliWritesCalibrationOnlyGridReport(tmp_path, monkeypatch):
    """成功路径只编码一次，再报告九个固定网格及同一 calibration 分折。"""
    fixture = _studyCliHarness(tmp_path, monkeypatch, ["--candidate-study"])
    assert study.main() == 0
    report = json.loads(fixture.output.read_text(encoding="utf-8"))
    assert report["productionEligible"] is False
    assert report["split"] == "calibration"
    assert report["calibrationSha256"] == study.evaluation._casesDigest(study.loadCalibrationCases(fixture.path))
    assert report["modelRevision"] == "fake-dense-revision"
    assert report["modelManifestSha256"] == hashlib.sha256(fixture.manifest.read_bytes()).hexdigest()
    assert fixture.events.count("dense.score") == 5
    assert fixture.events[-2:] == ["scorer.clear", "encoder.close"]
    assert "pair.load" not in fixture.events
    assert "IGNORE_HOLDOUT" not in fixture.output.read_text(encoding="utf-8")
    trials = report["candidateStudy"]["trials"]
    assert len(trials) == 9
    for trial in trials:
        assert trial["coverage"]["cohorts"]["oldCalibration"]["requiredCount"] == 3
        assert trial["coverage"]["cohorts"]["expandedCalibration"]["requiredCount"] == 2
        assert trial["denseGate"]["thresholds"]["lexical"] is None
        assert len(trial["denseGate"]["folds"]) == 5
        assert sum(fold["testCaseCount"] for fold in trial["denseGate"]["folds"]) == 5


def test_twoStageCliReleasesDenseBeforePairAndReportsConstraints(tmp_path, monkeypatch):
    """真实研究流程使用假模型，锁住加载顺序、关闭词面旁路及预算元数据。"""
    fixture = _studyCliHarness(tmp_path, monkeypatch, ["--two-stage-reranker"])
    assert study.main() == 0
    report = json.loads(fixture.output.read_text(encoding="utf-8"))
    events = fixture.events
    assert events.index("scorer.clear") < events.index("encoder.close") < events.index("pair.load") < events.index("pair.score")
    assert events[-2:] == ["pair.close", "encoder.close"]
    assert len(fixture.batches) == 10
    assert all(len(batch) == 1 for batch in fixture.batches)
    assert all("CLI_HINT_MARKER" not in passage for batch in fixture.batches for query, passage in batch)
    meta = report["reranker"]
    assert meta["revision"] == "fake-pair-revision"
    assert meta["manifestSha256"] == hashlib.sha256(fixture.pairManifest.read_bytes()).hexdigest()
    assert meta["candidateStrategy"] == "union-per-channel"
    assert meta["lexicalIndependentAdmission"] is False
    assert meta["inferenceBatchSize"] == 1
    assert meta["memoryLimitBytes"] == study.PAIR_STUDY_MEMORY_LIMIT
    assert meta["encoderReleasedBeforePairLoad"] is True
    assert meta["pairCount"] == 10
    assert meta["timingUnit"] == "single-pair"
    assert report["productionEligible"] is False
    assert report["split"] == "calibration"
    assert report["candidateStudy"] is None
    assert len(report["trials"]) == 1
    assert report["trials"][0]["thresholds"]["lexical"] is None
    assert meta["shortlistCoverage"]["cohorts"]["expandedCalibration"]["requiredCount"] == 2
    assert report["trials"][0]["groupValidation"]["multiRequiredCaseCount"] == 1
    assert report["trials"][0]["groupValidation"]["noAnswerCaseCount"] == 1


@pytest.mark.parametrize("trigger,phase,completedPairs", [
    ("encoder.load", "dense-load", 0),
    ("dense.score", "dense-scoring", 0),
    ("pair.load", "pair-load", 0),
    ("pair.score", "pair-scoring", 1),
    ("validation.complete", "validation", 10),
])
def test_twoStageCliWritesAbortEvidenceAndClosesModels(tmp_path, monkeypatch, trigger, phase, completedPairs):
    """加载、评分及验证超限只写资源中止证据，绝不保留部分质量成绩。"""
    fixture = _studyCliHarness(tmp_path, monkeypatch, ["--two-stage-reranker"])
    originalStudy = study.studyScores

    def validate(*args, **kwargs):
        """保留真实分折校准，在完成后模拟验证阶段的历史峰值。"""
        result = originalStudy(*args, **kwargs)
        fixture.events.append("validation.complete")
        return result

    def memoryInfo():
        """让当前 RSS 回落但历史峰值超限，防止只检查当前值的退化。"""
        peak = study.PAIR_STUDY_MEMORY_LIMIT + 1 if trigger in fixture.events else 120
        return SimpleNamespace(rss=100, peak_wset=peak)

    monkeypatch.setattr(study, "studyScores", validate)
    monkeypatch.setattr(study.evaluation.psutil, "Process", lambda: SimpleNamespace(memory_info=memoryInfo))
    assert study.main() == 1
    report = json.loads(fixture.output.read_text(encoding="utf-8"))
    assert report["status"] == "aborted"
    assert report["reason"] == "memory-budget-exceeded"
    assert report["productionEligible"] is False
    assert report["phase"] == phase
    assert report["split"] == "calibration"
    assert report["trials"] == []
    assert "qualityGate" not in report
    assert "thresholds" not in report
    assert report["completedPairCount"] == completedPairs
    assert report["rssBytes"] == {
        "atAbort": 100, "observedMaximum": 100,
        "processPeakWorkingSet": study.PAIR_STUDY_MEMORY_LIMIT + 1,
        "limit": study.PAIR_STUDY_MEMORY_LIMIT,
    }
    assert report["calibrationSha256"] == study.evaluation._casesDigest(study.loadCalibrationCases(fixture.path))
    assert report["modelManifestSha256"] == hashlib.sha256(fixture.manifest.read_bytes()).hexdigest()
    assert report["rerankerManifestSha256"] == hashlib.sha256(fixture.pairManifest.read_bytes()).hexdigest()
    assert fixture.events[-1] == "encoder.close"
    if phase.startswith("dense"):
        assert "pair.load" not in fixture.events
    else:
        assert fixture.events[-2:] == ["pair.close", "encoder.close"]
    assert fixture.events.count("pair.score") == completedPairs


@pytest.mark.parametrize("target", ["cases", "denseManifest", "pairManifest", "denseModel", "pairModel"])
def test_twoStageCliProtectsInputsEvenWhenAlreadyOverBudget(tmp_path, monkeypatch, target):
    """路径保护必须先于中止报告写入，否则超限分支可能覆盖输入。"""
    fixture = _studyCliHarness(tmp_path, monkeypatch, ["--two-stage-reranker"])
    paths = {"cases": fixture.path, "denseManifest": fixture.manifest, "pairManifest": fixture.pairManifest}
    for name, directory in (("denseModel", "dense-model"), ("pairModel", "pair-model")):
        modelFile = tmp_path / directory / "weights.onnx"
        modelFile.parent.mkdir()
        modelFile.write_bytes(b"keep-original-artifact")
        paths[name] = modelFile
    protected = paths[target]
    original = protected.read_bytes()
    argv = list(study.sys.argv)
    argv[argv.index("--output") + 1] = str(protected)
    monkeypatch.setattr("sys.argv", argv)
    monkeypatch.setattr(study.evaluation.psutil, "Process", lambda: _memoryProcess(study.PAIR_STUDY_MEMORY_LIMIT + 1))
    assert study.main() == 1
    assert protected.read_bytes() == original
    assert fixture.events == []
    assert not fixture.output.exists()


@pytest.mark.parametrize("target", ["cases", "manifest", "calibration", "formalManifest", "rerankerManifest"])
def test_cliRejectsTemporaryReportCollisionsBeforeReadingInputs(tmp_path, monkeypatch, target):
    """原子报告的临时文件也不得覆盖输入或正式配置，且应在读取数据前拒绝。"""
    output = tmp_path / "report.json"
    temporary = output.with_suffix(output.suffix + ".tmp")
    original = b"preserve-input-bytes"
    temporary.write_bytes(original)
    paths = {name: tmp_path / f"{name}.json" for name in (
        "cases", "manifest", "calibration", "formalManifest", "rerankerManifest",
    )}
    paths[target] = temporary
    monkeypatch.setattr(study, "LLM_MEMORY_CALIBRATION_PATH", paths["calibration"])
    monkeypatch.setattr(study, "LLM_MEMORY_MODEL_MANIFEST_PATH", paths["formalManifest"])
    monkeypatch.setattr(study, "loadCalibrationCases", lambda path: pytest.fail("read fixture before path protection"))
    monkeypatch.setattr(study, "StudyEncoder", lambda **kwargs: pytest.fail("loaded model before path protection"))
    monkeypatch.setattr(study.evaluation.psutil, "Process", _memoryProcess)
    monkeypatch.setattr("sys.argv", [
        "studyMemoryRetrieval.py", "--cases", str(paths["cases"]), "--output", str(output),
        "--manifest", str(paths["manifest"]), "--model-dir", str(tmp_path / "dense-model"),
        "--reranker-manifest", str(paths["rerankerManifest"]), "--reranker-dir", str(tmp_path / "pair-model"),
        "--variants", "baseline", "--two-stage-reranker",
    ])
    assert study.main() == 1
    assert temporary.read_bytes() == original
    assert not output.exists()




def _rerankerRuntimeFixture(tmpPath, monkeypatch):
    """Use validated tiny artifacts and fake dependencies without loading model libraries."""
    artifact = tmpPath / "model.onnx"
    artifact.write_bytes(b"fake-runtime-artifact")
    manifest = tmpPath / "manifest.json"
    manifest.write_text(json.dumps({
        "schemaVersion": 1, "task": "text-pair-relevance", "modelFile": artifact.name,
        "tokenizerFile": artifact.name, "maxTokens": 256, "padToken": "[PAD]",
        "artifacts": [{"path": artifact.name, "size": artifact.stat().st_size,
                       "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}],
    }), encoding="utf-8")
    events, configEntries = [], []
    options = SimpleNamespace(
        graph_optimization_level="runtime-default",
        add_session_config_entry=lambda key, value: configEntries.append((key, value)),
    )
    tokenizer = SimpleNamespace(
        enable_truncation=lambda **kwargs: events.append(("truncation", kwargs)),
        token_to_id=lambda token: 0,
        enable_padding=lambda **kwargs: events.append(("padding", kwargs)),
    )

    def loadTokenizer(path):
        """Record when tokenizer loading actually starts."""
        events.append("tokenizer.load")
        return tokenizer

    def loadSession(path, **kwargs):
        """Capture options and providers passed to native session creation."""
        events.append(("session.load", kwargs))
        return SimpleNamespace()

    runtime = SimpleNamespace(
        SessionOptions=lambda: options,
        ExecutionMode=SimpleNamespace(ORT_SEQUENTIAL="sequential"),
        GraphOptimizationLevel=SimpleNamespace(ORT_DISABLE_ALL="disabled"),
        InferenceSession=loadSession,
    )
    dependencies = {
        "numpy": SimpleNamespace(), "onnxruntime": runtime,
        "tokenizers": SimpleNamespace(Tokenizer=SimpleNamespace(from_file=loadTokenizer)),
    }

    def importDependency(name):
        """Reject missing fake dependencies instead of importing a real model library."""
        events.append("import." + name)
        return dependencies[name]

    monkeypatch.setattr(study.MemoryEncoder, "_importOptionalDependency", importDependency)
    return SimpleNamespace(manifest=manifest, events=events, options=options, configEntries=configEntries)


def test_rerankerUnknownRuntimeProfileFailsBeforeReadingManifest(tmp_path, monkeypatch):
    """Reject unknown profiles before reading any manifest or loading model libraries."""
    monkeypatch.setattr(study.MemoryEncoder, "_importOptionalDependency", lambda name: pytest.fail("loaded model stack"))
    with pytest.raises(study.evaluation.EvaluationError, match="\u8fd0\u884c\u65f6\u914d\u7f6e"):
        study.StudyReranker(tmp_path, tmp_path / "missing.json", runtimeProfile="unknown")


@pytest.mark.parametrize("profile", [None, "default", "low-memory"])
def test_rerankerRuntimeProfilePreservesDefaultAndOnlyOptsIntoLowMemory(tmp_path, monkeypatch, profile):
    """Default callers preserve runtime optimizations; memory changes require explicit opt-in."""
    fixture = _rerankerRuntimeFixture(tmp_path, monkeypatch)
    kwargs = {} if profile is None else {"runtimeProfile": profile}
    reranker = study.StudyReranker(tmp_path, fixture.manifest, stageObserver=fixture.events.append, **kwargs)
    assert reranker.runtimeProfile == (profile or "default")
    options = fixture.options
    assert options.intra_op_num_threads == options.inter_op_num_threads == 1
    assert options.execution_mode == "sequential"
    assert options.enable_cpu_mem_arena is False
    assert options.enable_mem_pattern is False
    assert options.graph_optimization_level == ("disabled" if profile == "low-memory" else "runtime-default")
    assert fixture.configEntries == ([("session.disable_prepacking", "1")] if profile == "low-memory" else [])
    assert fixture.events == [
        "pair-imports-start", "import.numpy", "import.onnxruntime",
        "pair-imports-complete", "pair-tokenizer-start", "import.tokenizers", "tokenizer.load",
        ("truncation", {"max_length": 256, "strategy": "longest_first"}),
        ("padding", {"pad_id": 0, "pad_token": "[PAD]"}),
        "pair-tokenizer-complete", "pair-session-start",
        ("session.load", {"sess_options": options, "providers": ["CPUExecutionProvider"]}),
        "pair-session-complete",
    ]
    reranker.close()


@pytest.mark.parametrize("stopStage", ["pair-imports-start", "pair-session-start"])
def test_rerankerStageObserverCanStopBeforeHeavyOperation(tmp_path, monkeypatch, stopStage):
    """Observer exceptions prevent the next expensive operation at a loading boundary."""
    fixture = _rerankerRuntimeFixture(tmp_path, monkeypatch)

    def observe(stage):
        """Raise a stop signal at the requested loading boundary."""
        fixture.events.append(stage)
        if stage == stopStage:
            raise RuntimeError("stop at budget boundary")

    with pytest.raises(RuntimeError, match="budget boundary"):
        study.StudyReranker(tmp_path, fixture.manifest, runtimeProfile="low-memory", stageObserver=observe)
    assert fixture.events[-1] == stopStage
    assert not any(isinstance(event, tuple) and event[0] == "session.load" for event in fixture.events)
    if stopStage == "pair-imports-start":
        assert not any(str(event).startswith("import.") for event in fixture.events)


@pytest.mark.parametrize("profile", ["default", "low-memory"])
def test_twoStageCliPassesAndReportsRuntimeProfile(tmp_path, monkeypatch, profile):
    """Forward explicit profiles and report them while preserving legacy default calls."""
    fixture = _studyCliHarness(tmp_path, monkeypatch, [
        "--two-stage-reranker", "--reranker-runtime-profile", profile,
    ])
    previousFactory = study.StudyReranker
    options = []

    def makeReranker(modelDir, manifestPath, **kwargs):
        """Capture new options and reuse the existing fake model execution path."""
        options.append(kwargs)
        return previousFactory(modelDir, manifestPath)

    monkeypatch.setattr(study, "StudyReranker", makeReranker)
    assert study.main() == 0
    report = json.loads(fixture.output.read_text(encoding="utf-8"))
    assert options == ([{"runtimeProfile": profile}] if profile == "low-memory" else [{}])
    assert report["reranker"]["runtimeProfile"] == profile
    assert report["reranker"]["inferenceBatchSize"] == 1
    assert report["reranker"]["memoryLimitBytes"] == 512 * 1024 * 1024
    assert report["productionEligible"] is False


@pytest.mark.parametrize("extra", [
    [], ["--candidate-study"], ["--reranker-dir", "model", "--reranker-manifest", "manifest.json"],
    ["--two-stage-reranker"], ["--two-stage-reranker", "--reranker-manifest", "manifest.json"],
])
def test_lowMemoryCliRequiresBudgetedPairModeBeforeInputRead(tmp_path, monkeypatch, extra):
    """Require budgeted pair mode and both model inputs before reading cases."""
    monkeypatch.setattr(study, "loadCalibrationCases", lambda path: pytest.fail("read cases for invalid mode"))
    monkeypatch.setattr(study, "StudyEncoder", lambda **kwargs: pytest.fail("loaded encoder for invalid mode"))
    monkeypatch.setattr(study.evaluation.psutil, "Process", _memoryProcess)
    output = tmp_path / "report.json"
    monkeypatch.setattr("sys.argv", [
        "studyMemoryRetrieval.py", "--cases", str(tmp_path / "cases.json"), "--output", str(output),
        "--variants", "baseline", "--reranker-runtime-profile", "low-memory", *extra,
    ])
    assert study.main() == 1
    assert not output.exists()


def test_lowMemoryCliBudgetAbortKeepsProfileAndNoQualityResults(tmp_path, monkeypatch):
    """Preserve selected profile in abort evidence without publishing partial quality results."""
    fixture = _studyCliHarness(tmp_path, monkeypatch, [
        "--two-stage-reranker", "--reranker-runtime-profile", "low-memory",
    ])
    monkeypatch.setattr(study.evaluation.psutil, "Process", lambda: _memoryProcess(study.PAIR_STUDY_MEMORY_LIMIT + 1))
    assert study.main() == 1
    report = json.loads(fixture.output.read_text(encoding="utf-8"))
    assert report["status"] == "aborted"
    assert report["reason"] == "memory-budget-exceeded"
    assert report["rerankerRuntimeProfile"] == "low-memory"
    assert report["trials"] == []
    assert "qualityGate" not in report
    assert "thresholds" not in report
    assert fixture.events == []




def test_rerankerTokenizerHelperImportsOnlyTokenizerDependency(tmp_path, monkeypatch):
    """串行分词阶段不应导入 numpy 或 ORT，配置仍使用相同清单。"""
    fixture = _rerankerRuntimeFixture(tmp_path, monkeypatch)
    manifest = study.readRerankerManifest(tmp_path, fixture.manifest)
    assert fixture.events == []
    tokenizer = study.loadRerankerTokenizer(tmp_path, manifest)
    assert tokenizer is not None
    assert fixture.events == [
        "import.tokenizers", "tokenizer.load",
        ("truncation", {"max_length": 256, "strategy": "longest_first"}),
        ("padding", {"pad_id": 0, "pad_token": "[PAD]"}),
    ]


@pytest.mark.parametrize("damage", ["size", "sha256", "undeclared", "outside"])
def test_rerankerManifestHelperKeepsIntegrityAndPathChecks(tmp_path, monkeypatch, damage):
    """提取校验函数后仍拒绝错误产物与越界路径，且不导入模型依赖。"""
    fixture = _rerankerRuntimeFixture(tmp_path, monkeypatch)
    manifest = json.loads(fixture.manifest.read_text(encoding="utf-8"))
    if damage == "size":
        manifest["artifacts"][0]["size"] += 1
    elif damage == "sha256":
        manifest["artifacts"][0]["sha256"] = "0" * 64
    elif damage == "undeclared":
        manifest["tokenizerFile"] = "undeclared.json"
    else:
        manifest["artifacts"][0]["path"] = "../outside.onnx"
        manifest["tokenizerFile"] = manifest["modelFile"] = "../outside.onnx"
    fixture.manifest.write_text(json.dumps(manifest), encoding="utf-8")
    expectedError = RuntimeError if damage == "outside" else study.evaluation.EvaluationError
    with pytest.raises(expectedError):
        study.readRerankerManifest(tmp_path, fixture.manifest)
    assert fixture.events == []


@pytest.mark.parametrize("profile", ["default", "low-memory"])
def test_rerankerSessionOnlyNeverImportsOrBuildsTokenizer(tmp_path, monkeypatch, profile):
    """仅推理的构造分支保留会话配置，且完全跳过分词依赖及分词事件。"""
    fixture = _rerankerRuntimeFixture(tmp_path, monkeypatch)
    ranker = study.StudyReranker(tmp_path, fixture.manifest, runtimeProfile=profile,
                                 loadTokenizer=False, stageObserver=fixture.events.append)
    assert ranker.tokenizer is None
    assert fixture.events == [
        "pair-imports-start", "import.numpy", "import.onnxruntime", "pair-imports-complete",
        "pair-session-start",
        ("session.load", {"sess_options": fixture.options, "providers": ["CPUExecutionProvider"]}),
        "pair-session-complete",
    ]
    assert fixture.options.enable_cpu_mem_arena is False
    assert fixture.options.enable_mem_pattern is False
    assert fixture.options.intra_op_num_threads == fixture.options.inter_op_num_threads == 1
    assert fixture.options.graph_optimization_level == ("disabled" if profile == "low-memory" else "runtime-default")
    assert fixture.configEntries == ([("session.disable_prepacking", "1")] if profile == "low-memory" else [])
    with pytest.raises(study.evaluation.EvaluationError, match="tokenizer"):
        ranker.scorePairs([("question", "passage")])
    assert ranker.scoreEncodedPairs([]) == []
    assert ranker.pairCount == 0
    assert ranker.timings == []
    ranker.close()





def _serialPairFixture():
    """提供包含 padding 及两段类型信息的原始 tokenizer 结果。"""
    return [
        SimpleNamespace(ids=[101, 7, 102, 88, 102, 0], attention_mask=[1, 1, 1, 1, 1, 0], type_ids=[0, 0, 0, 1, 1, 0]),
        SimpleNamespace(ids=[101, 9, 11, 102, 88, 102], attention_mask=[1, 1, 1, 1, 1, 1], type_ids=[0, 0, 0, 0, 1, 1]),
    ]


def _encodedReranker():
    """记录真实 numpy 输入张量，使用固定 logits 替代模型推理。"""
    numpy = pytest.importorskip("numpy")
    ranker = object.__new__(study.StudyReranker)
    ranker.manifest = {"maxTokens": 256}
    ranker.numpy = numpy
    ranker.tokenizer = None
    ranker.timings = []
    ranker.pairCount = 0
    calls = []

    def run(names, inputs):
        """保留 dtype 和 token 值，用于比较串行与旧接口的输入。"""
        calls.append({name: values.copy() for name, values in inputs.items()})
        count = len(inputs["input_ids"])
        return [numpy.asarray([[float(index * 2)] for index in range(count)])]

    ranker.session = SimpleNamespace(
        get_inputs=lambda: [SimpleNamespace(name=name, type=dataType) for name, dataType in (
            ("input_ids", "tensor(int64)"), ("attention_mask", "tensor(int32)"), ("token_type_ids", "tensor(int64)"),
        )],
        run=run,
    )
    return ranker, calls


def test_pairEncodingPreservesValuesAndCopiesTokenizerLists():
    """序列化往返不改变三个字段，调用者修改协议对象也不能污染 tokenizer。"""
    source = _serialPairFixture()
    pairs = [("query one", "passage one"), ("query two", "passage two")]
    seen = []

    def encode(passed):
        """检查仍使用原 encode_batch，并保存传入文本顺序。"""
        seen.append(passed)
        return source

    encoded = study.encodeRerankerPairs(SimpleNamespace(encode_batch=encode), pairs)
    assert seen == [pairs]
    assert json.loads(json.dumps(encoded)) == [
        {"input_ids": item.ids, "attention_mask": item.attention_mask, "token_type_ids": item.type_ids}
        for item in source
    ]
    for item in encoded:
        assert all(type(value) is int for values in item.values() for value in values)
    encoded[0]["input_ids"][0] = 999
    encoded[0]["attention_mask"][0] = 0
    encoded[0]["token_type_ids"][0] = 1
    assert source[0].ids[0] == 101
    assert source[0].attention_mask[0] == 1
    assert source[0].type_ids[0] == 0


def test_serialEncodedAndTextInterfacesProduceIdenticalInputsScoresAndCounts():
    """两种接口必须给 ORT 相同 token、mask、类型及 dtype，返回相同 sigmoid。"""
    ranker, calls = _encodedReranker()
    ranker.tokenizer = SimpleNamespace(encode_batch=lambda pairs: _serialPairFixture())
    pairs = [("query one", "passage one"), ("query two", "passage two")]
    textScores = ranker.scorePairs(pairs)
    encoded = json.loads(json.dumps(study.encodeRerankerPairs(ranker.tokenizer, pairs)))
    ranker.tokenizer = None
    serialScores = ranker.scoreEncodedPairs(encoded)
    assert serialScores == textScores == pytest.approx([0.5, 0.880797])
    assert ranker.pairCount == 4
    assert len(ranker.timings) == 2
    assert len(calls) == 2
    for name, expectedType in (("input_ids", ranker.numpy.int64), ("attention_mask", ranker.numpy.int32), ("token_type_ids", ranker.numpy.int64)):
        assert calls[0][name].tolist() == calls[1][name].tolist() == [item[name] for item in encoded]
        assert calls[0][name].dtype == calls[1][name].dtype == expectedType


def test_pairTextTimingIncludesEncodingButEncodedTimingDoesNot(monkeypatch):
    """默认调用继续统计编码成本，串行推理不会伪装成完整链路耗时。"""
    ranker, calls = _encodedReranker()
    clock = [0.0]
    originalRun = ranker.session.run

    def encode(pairs):
        """模拟两秒分词耗时，无实际等待。"""
        clock[0] += 2
        return _serialPairFixture()

    def run(names, inputs):
        """模拟三秒推理耗时，无实际等待。"""
        clock[0] += 3
        return originalRun(names, inputs)

    ranker.tokenizer = SimpleNamespace(encode_batch=encode)
    ranker.session.run = run
    monkeypatch.setattr(study.time, "perf_counter", lambda: clock[0])
    pairs = [("query one", "passage one"), ("query two", "passage two")]
    ranker.scorePairs(pairs)
    encoded = study.encodeRerankerPairs(ranker.tokenizer, pairs)
    ranker.scoreEncodedPairs(encoded)
    assert ranker.timings == [5.0, 3.0]


@pytest.mark.parametrize("damage", [
    "not-list", "not-object", "missing-field", "extra-field", "not-list-field", "empty-field",
    "unequal-fields", "oversized", "unequal-batch", "mask-two",
])
def test_encodedPairProtocolRejectsMalformedShapeBeforeNativeCalls(damage):
    """错误序列不得进入 numpy/session，也不得计入成功推理数量或耗时。"""
    ranker, calls = _encodedReranker()
    encoded = study.encodeRerankerPairs(SimpleNamespace(encode_batch=lambda pairs: _serialPairFixture()), [("a", "b"), ("c", "d")])
    if damage == "not-list":
        encoded = tuple(encoded)
    elif damage == "not-object":
        encoded[0] = None
    elif damage == "missing-field":
        del encoded[0]["token_type_ids"]
    elif damage == "extra-field":
        encoded[0]["private-data"] = [1]
    elif damage == "not-list-field":
        encoded[0]["input_ids"] = tuple(encoded[0]["input_ids"])
    elif damage == "empty-field":
        encoded[0]["input_ids"] = []
    elif damage == "unequal-fields":
        encoded[0]["input_ids"].pop()
    elif damage == "oversized":
        encoded = [{name: [1] * 257 for name in encoded[0]}]
    elif damage == "unequal-batch":
        encoded[1] = {name: values[:-1] for name, values in encoded[1].items()}
    else:
        encoded[0]["attention_mask"][0] = 2
    ranker.session.get_inputs = lambda: pytest.fail("native input inspection preceded protocol validation")
    with pytest.raises(study.evaluation.EvaluationError):
        ranker.scoreEncodedPairs(encoded)
    assert calls == []
    assert ranker.timings == []
    assert ranker.pairCount == 0


@pytest.mark.parametrize("field", ["input_ids", "attention_mask", "token_type_ids"])
@pytest.mark.parametrize("value", [-1, True, 1.0])
def test_encodedPairProtocolRejectsInvalidIntegerValues(field, value):
    """每个字段都拒绝负数、bool 和浮点数，防止 numpy 静默转换非法协议。"""
    ranker, calls = _encodedReranker()
    encoded = [{"input_ids": [1], "attention_mask": [1], "token_type_ids": [0]}]
    encoded[0][field][0] = value
    ranker.session.get_inputs = lambda: pytest.fail("native call for invalid integer")
    with pytest.raises(study.evaluation.EvaluationError):
        ranker.scoreEncodedPairs(encoded)
    assert calls == []
    assert ranker.timings == []
    assert ranker.pairCount == 0


def test_encodedPairProtocolAcceptsExactTokenLimitAndEmptyBatch():
    """等于清单 token 上限仍合法；空 batch 不启动分词或推理。"""
    ranker, calls = _encodedReranker()
    assert ranker.scoreEncodedPairs([]) == []
    encoded = [{"input_ids": [7] * 256, "attention_mask": [1] * 256, "token_type_ids": [0] * 256}]
    assert ranker.scoreEncodedPairs(encoded) == [0.5]
    assert ranker.pairCount == 1
    assert len(calls) == len(ranker.timings) == 1
    assert calls[0]["input_ids"].shape == (1, 256)
    assert study.encodeRerankerPairs(SimpleNamespace(encode_batch=lambda pairs: pytest.fail("tokenized empty batch")), []) == []


@pytest.mark.parametrize("logits", [[[float("nan")]], [[0.0, 1.0]], []])
def test_encodedPairsRejectInvalidModelLogits(logits):
    """串行接口保留原有限单标量 logit 契约，坏输出不计为成功推理。"""
    ranker, calls = _encodedReranker()
    ranker.session.run = lambda names, inputs: [ranker.numpy.asarray(logits)]
    encoded = [{"input_ids": [7], "attention_mask": [1], "token_type_ids": [0]}]
    with pytest.raises(study.evaluation.EvaluationError, match="logit"):
        ranker.scoreEncodedPairs(encoded)
    assert ranker.pairCount == 0
    assert ranker.timings == []


@pytest.mark.parametrize("encodedCount", [0, 1, 3])
def test_pairEncodingRejectsTokenizerCountMismatch(encodedCount):
    """分词丢项或多项不能让旧文本接口静默返回错误数量的相关性分数。"""
    tokenizer = SimpleNamespace(encode_batch=lambda pairs: [_serialPairFixture()[0]] * encodedCount)
    with pytest.raises(study.evaluation.EvaluationError, match="tokenizer"):
        study.encodeRerankerPairs(tokenizer, [("a", "b"), ("c", "d")])
