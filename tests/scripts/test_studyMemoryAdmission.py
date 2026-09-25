"""只用合成 fixture 验证离线准入学习的隔离、预算与保守弃权。"""

import hashlib
import json
import math
import os
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

import pytest

# 独立执行该文件也禁止配置导入读取真实 .env。
with patch("dotenv.load_dotenv", return_value=False), patch.dict(os.environ, {"BOT_TOKEN": "offline-fixture-test"}):
    from scripts import studyMemoryAdmission as admission
    from tests.scripts.test_evaluateMemory import _case




def _cases(count=10):
    """每组包含两个不同标签候选，并单列多事实和无答案场景。"""
    rawCases = [_case(f"admission-{index}", "calibration", hint="HINT_MUST_NOT_LEAK") for index in range(count)]
    rawCases[0].update(requiredIDs=[1, 2], forbiddenIDs=[], subsets=["expandedCalibration", "multiRequired"])
    rawCases[1].update(requiredIDs=[], forbiddenIDs=[1, 2], allowAbstain=True, subsets=["expandedCalibration", "noAnswer"])
    return admission.evaluation.validateEvaluationCases(rawCases)


def _record(*, sameQuery=False):
    """基础分表达互补证据，增强分故意偏向另一条以捕获 hint 泄漏。"""
    return {
        "sameQuery": sameQuery,
        "scores": {
            "semanticCurrent": {1: 0.8, 2: 0.2},
            "semanticAssisted": {1: 0.3, 2: 0.7},
            "lexical": {1: 0.0, 2: 2.0},
        },
        "ranking": {"semanticCurrent": {1: 0.01, 2: 999.0}, "semanticAssisted": {1: 999.0, 2: 0.01}},
    }


def _memoryProcess(rss=100, peak=120):
    """可控进程观测不依赖开发机实际负载。"""
    return SimpleNamespace(memory_info=lambda: SimpleNamespace(rss=rss, peak_wset=peak))

def test_featuresIgnoreLabelsHintsMetadataAndIdentifierValues():
    """标签、增强排序、主题及 ID 改名不得成为模型的可见证据。"""
    case = _cases()[2]
    record = _record()
    originalCase, originalRecord = deepcopy(case), deepcopy(record)
    expected = admission.buildCandidateFeatures(case, record, {1, 2})
    changed = deepcopy(case)
    changed.update(caseID="different-case", groupID="different-group", subsets=["different-cohort"],
                   requiredIDs={2}, allowedIDs=set(), forbiddenIDs={1}, metadata={"reviewStatus": "approved"})
    for memory in changed["memories"]:
        memory["retrievalHint"] = "MUTATED_HINT_MARKER"
    changedRecord = deepcopy(record)
    changedRecord["ranking"] = {"semanticCurrent": {1: 99999.0}, "semanticAssisted": {2: -99999.0}}
    assert admission.buildCandidateFeatures(changed, changedRecord, {1, 2}) == expected
    renamed = {1: 22, 2: 11}
    for memory in changed["memories"]:
        memory["id"] = renamed[memory["id"]]
    for channel, values in changedRecord["scores"].items():
        changedRecord["scores"][channel] = {renamed[key]: value for key, value in values.items()}
    actual = admission.buildCandidateFeatures(changed, changedRecord, set(renamed.values()))
    assert {key: actual[value] for key, value in renamed.items()} == expected
    assert (case, record) == (originalCase, originalRecord)
    assert all(len(row) == len(admission.FEATURE_NAMES) and all(math.isfinite(value) for value in row) for row in expected.values())


def test_featureRanksShareTiesAndHaveNoArtificialSingleCandidateMargin():
    """同分同名次与单候选零领先防止 ID 或空分布变成虚假优势。"""
    case = _cases()[2]
    record = _record(sameQuery=True)
    record["scores"] = {channel: {2: 0.5, 1: 0.5} for channel in admission.evaluation.CHANNEL_NAMES}
    rows = admission.buildCandidateFeatures(case, record, {1, 2})
    for name in admission.RELATIVE_FEATURES:
        index = admission.FEATURE_NAMES.index(name)
        assert rows[1][index] == rows[2][index]
    assert rows[1][admission.FEATURE_NAMES.index("historyDelta")] == 0
    for values in record["scores"].values():
        values.pop(2)
    single = admission.buildCandidateFeatures(case, record, {1})[1]
    for name in ("currentBestOtherGap", "assistedBestOtherGap", "currentRankFraction"):
        assert single[admission.FEATURE_NAMES.index(name)] == 0


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_featureBoundaryRejectsNonFiniteBaseEvidence(value):
    """非有限基础证据不能归零后混入拟合。"""
    record = _record()
    record["scores"]["semanticCurrent"][2] = value
    with pytest.raises(admission.evaluation.EvaluationError, match="有限"):
        admission.buildCandidateFeatures(_cases()[2], record, {1})


def test_featuresRejectUnretrievableIDsAndSupportEmptyPool():
    """scope 外的候选必须拒绝，空候选无需制造训练样本。"""
    case = _cases()[2]
    assert admission.buildCandidateFeatures(case, _record(), set()) == {}
    with pytest.raises(admission.evaluation.EvaluationError, match="scope"):
        admission.buildCandidateFeatures(case, _record(), {999})




def _featureRows(cases):
    """人工特征只表达两个相关性档位，测试统计过程时无需编码器。"""
    return {case["caseID"]: {1: [1.0] * len(admission.FEATURE_NAMES), 2: [-1.0] * len(admission.FEATURE_NAMES)} for case in cases}


def test_fitBalancesGroupsAndIgnoresHeldOutFeatureExtremes():
    """同组重复题不放大权重，持出组异常大值不能改变缩放与拟合。"""
    cases = _cases()[2:5]
    features = _featureRows(cases)
    features[cases[1]["caseID"]] = {1: [3.0] * len(admission.FEATURE_NAMES), 2: [1.0] * len(admission.FEATURE_NAMES)}
    model = admission.fitAdmissionModel(cases, features, "scores")
    assert model["enabled"] is True
    assert model["mean"] == pytest.approx([2 / 3] * len(admission.SCORE_FEATURES))
    assert model["scale"] == pytest.approx([math.sqrt(17 / 9)] * len(admission.SCORE_FEATURES))
    heldOut = _cases()[5]
    features[heldOut["caseID"]] = {1: [1e9] * len(admission.FEATURE_NAMES), 2: [-1e9] * len(admission.FEATURE_NAMES)}
    assert admission.fitAdmissionModel(cases, features, "scores") == model
    duplicate = deepcopy(cases[1])
    duplicate["caseID"] = "duplicate-in-same-group"
    features[duplicate["caseID"]] = deepcopy(features[cases[1]["caseID"]])
    repeated = admission.fitAdmissionModel([*cases, duplicate], features, "scores")
    for name in ("mean", "scale", "coefficients", "intercept"):
        assert repeated[name] == pytest.approx(model[name])
    predictions = admission.predictAdmission(model, features)
    assert all(math.isfinite(score) and 0 <= score <= 1 for rows in predictions.values() for score in rows.values())


def test_fitTreatsAllowedAsPositiveAndExcludesUnlabeledCandidates():
    """allowed 是可接受正例；未标注候选不参与拟合但不能在校准时算正确。"""
    cases = _cases()[2:4]
    cases[0].update(requiredIDs=set(), allowedIDs={1})
    features = _featureRows(cases)
    features[cases[0]["caseID"]][3] = [9999.0] * len(admission.FEATURE_NAMES)
    model = admission.fitAdmissionModel(cases, features, "scores")
    assert model["positivePairCount"] == model["negativePairCount"] == 2
    assert model["excludedUnlabeledPairCount"] == 1
    assert model["mean"] == pytest.approx([0.0] * len(admission.SCORE_FEATURES))
    probabilities = {case["caseID"]: {1: 0.8, 2: 0.2} for case in cases}
    probabilities[cases[0]["caseID"]][3] = 0.9
    threshold, detail = admission.chooseAdmissionThreshold(cases, probabilities)
    assert threshold is None
    assert detail["enabled"] is False


@pytest.mark.parametrize("label", ["positive", "negative", "empty"])
def test_degenerateTrainingSafelyDisablesAdmission(label):
    """单类和空候选无法校准可信准入，必须关闭而非输出常数正概率。"""
    cases = _cases()[2:4]
    features = _featureRows(cases)
    for case in cases:
        case.update(requiredIDs={1, 2} if label == "positive" else set(),
                    allowedIDs=set(), forbiddenIDs={1, 2} if label == "negative" else set())
        if label == "empty":
            features[case["caseID"]] = {}
    model = admission.fitAdmissionModel(cases, features, "relative")
    assert model["enabled"] is False
    predictions = admission.predictAdmission(model, features)
    assert all(not values for values in predictions.values())
    assert admission.chooseAdmissionThreshold(cases, predictions)[0] is None


def test_thresholdKeepsEqualScoreBucketAndDisablesForbiddenTie():
    """不能拆开同分正负候选挑答案；安全的同分多事实应一起通过。"""
    multi, _, regular = _cases()[:3]
    assert admission.chooseAdmissionThreshold([multi, regular], {multi["caseID"]: {1: 0.8, 2: 0.8}, regular["caseID"]: {1: 0.8, 2: 0.1}})[0] == 0.8
    assert admission.chooseAdmissionThreshold([regular], {regular["caseID"]: {1: 0.8, 2: 0.8}})[0] is None




def test_gatePreservesChannelEvidenceWithoutLexicalBypassOrMutation():
    """词面高分也必须通过联合准入，不创造缺失通道或污染原记录。"""
    record = _record()
    record["scores"]["semanticCurrent"] = {1: 0.8}
    record["scores"]["lexical"][2] = 999.0
    records = {"case": record}
    original = deepcopy(records)
    gated = admission.gateRecords(records, {"case": {1: 0.9, 2: 0.1}}, 0.5)
    assert gated["case"]["scores"]["semanticCurrent"] == {1: 1.0}
    assert gated["case"]["scores"]["lexical"] == {}
    assert gated["case"]["ranking"]["semanticCurrent"] == {1: 0.01}
    gated["case"]["scores"]["semanticCurrent"][1] = 0
    gated["case"]["ranking"]["semanticCurrent"][1] = 0
    assert records == original
    assert all(not scores for scores in admission.gateRecords(records, {"case": {1: 0.9}}, None)["case"]["scores"].values())


def test_replayKeepsMultipleRequiredAndUsesExistingRenderedBudget():
    """联合同分不等于只取第一条；pinned 去重及字符预算沿用正式回放。"""
    case = _cases()[0]
    records = {case["caseID"]: _record(sameQuery=True)}
    probabilities = {case["caseID"]: {1: 0.8, 2: 0.8}}
    multi = admission.replayAdmission([case], records, probabilities, 0.8)[0]
    assert set(multi["contextualIDs"]) == {1, 2}
    rawCase = _case("budget", "calibration")
    rawCase["memories"][1]["content"] = "长" * (admission.evaluation.LLM_MEMORY_CONTEXT_MAX_CHARS + 200)
    pinned = deepcopy(rawCase["memories"][0])
    pinned.update(id=3, mode="pinned")
    rawCase["memories"].append(pinned)
    rawCase["requiredPinnedIDs"] = [3]
    case = admission.evaluation.validateEvaluationCases([rawCase])[0]
    record = _record(sameQuery=True)
    actual = admission.replayAdmission([case], {"budget": record}, {"budget": {1: 0.8, 2: 0.8}}, 0.5)[0]
    expected = admission.evaluation._scoreCaseResult(case, admission.evaluation._evaluateSingleCase(
        case, "hybrid+hint", {"semanticCurrent": 0.5, "semanticAssisted": 0.5, "lexical": 0.0},
        channelScores={"semanticCurrent": {1: 1.0, 2: 1.0}, "semanticAssisted": {}, "lexical": {2: 2.0}},
        semanticRankingScores=record["ranking"],
    ))
    expected.pop("diagnostics")
    assert actual == expected
    assert actual["pinnedIDs"] == [3]
    assert 1 not in actual["contextualIDs"]
    assert 2 not in actual["contextualIDs"]


def test_studyIsolatesFitCalibrationAndTestGroupsAndHeldOutOutliers():
    """外折测试分数或标签变化不回流到本折标准化、系数或准入阈值。"""
    cases = _cases()
    records = {case["caseID"]: _record(sameQuery=True) for case in cases}
    originalCases, originalRecords = deepcopy(cases), deepcopy(records)
    report = admission.studyAdmission(cases, records)
    assert report["productionEligible"] is False
    assert {trial["variant"] for trial in report["trials"]} == set(admission.FEATURE_SETS)
    for trial in report["trials"]:
        assert sum(fold["testCaseCount"] for fold in trial["folds"]) == len(cases)
        for fold in trial["folds"]:
            fit, calibration, test = [set(fold[key]) for key in ("fitGroupIDs", "calibrationGroupIDs", "testGroupIDs")]
            assert fit and calibration and test
            assert not (fit & calibration or fit & test or calibration & test)
            assert fit | calibration | test == {case["groupID"] for case in cases}
            assert fold["model"]["fitGroupIDs"] == sorted(fit)
        assert set(trial["groupValidation"]["cohorts"]) == {"oldCalibration", "expandedCalibration"}
    assert (cases, records) == (originalCases, originalRecords)
    targetGroups = set(report["trials"][0]["folds"][0]["testGroupIDs"])
    for case in cases:
        if case["groupID"] in targetGroups:
            case.update(requiredIDs={2}, allowedIDs=set(), forbiddenIDs={1})
            records[case["caseID"]]["scores"]["semanticCurrent"] = {1: -1e6, 2: 1e6}
    changed = admission.studyAdmission(cases, records)
    for before, after in zip(report["trials"], changed["trials"]):
        assert before["folds"][0]["model"] == after["folds"][0]["model"]
        assert before["folds"][0]["threshold"] == after["folds"][0]["threshold"]
    reordered = admission.splitFitCalibration(list(reversed(originalCases)))
    originalSplit = admission.splitFitCalibration(originalCases)
    assert [{case["caseID"] for case in part} for part in reordered] == [{case["caseID"] for case in part} for part in originalSplit]


def test_studyRejectsNonCalibrationCases():
    """持出题不能经程序调用绕过 CLI 的 calibration 筛选。"""
    cases = _cases()
    cases[0]["split"] = "holdout"
    with pytest.raises(admission.evaluation.EvaluationError, match="calibration"):
        admission.studyAdmission(cases, {})




def _cliHarness(tmpPath, monkeypatch):
    """仅替换编码器和进程观测，保留真实分组学习、筛选与报告写入。"""
    rawCases = [_case(f"cli-{index}", "calibration", hint="PRIVATE_HINT_MARKER") for index in range(5)]
    rawCases[0].update(requiredIDs=[1, 2], forbiddenIDs=[], subsets=["expandedCalibration", "multiRequired"])
    rawCases[1].update(requiredIDs=[], forbiddenIDs=[1, 2], allowAbstain=True, subsets=["expandedCalibration", "noAnswer"])
    casesPath = tmpPath / "cases.json"
    casesPath.write_text(json.dumps({"cases": [*rawCases, {"split": "holdout", "malformed": "IGNORE_HOLDOUT"}]}), encoding="utf-8")
    manifestPath = tmpPath / "manifest.json"
    manifestPath.write_text(json.dumps({"revision": "fixture-revision"}), encoding="utf-8")
    output = tmpPath / "report.json"
    events = []
    encoder = SimpleNamespace(close=lambda: events.append("encoder.close"))

    def makeEncoder(**kwargs):
        """记录模型生命周期，不导入任何推理依赖。"""
        events.append("encoder.load")
        return encoder

    def score(texts, candidates):
        """分数只依赖合成 ID，不接触答案标签。"""
        events.append("dense.score")
        return [{"base": {memory["id"]: 0.9 / memory["id"] for memory in candidates},
                 "enhanced": {memory["id"]: 0.95 for memory in candidates}} for text in texts]

    monkeypatch.setattr(admission.study, "StudyEncoder", makeEncoder)
    monkeypatch.setattr(admission.evaluation, "EncoderSemanticScorer", lambda encoder: SimpleNamespace(score=score, clear=lambda: events.append("scorer.clear")))
    monkeypatch.setattr(admission.evaluation.psutil, "Process", _memoryProcess)
    monkeypatch.setattr("sys.argv", ["studyMemoryAdmission.py", "--cases", str(casesPath), "--manifest", str(manifestPath),
                                    "--model-dir", str(tmpPath / "model"), "--output", str(output)])
    return SimpleNamespace(casesPath=casesPath, manifestPath=manifestPath, output=output, events=events)


def test_cliUsesOnlyCalibrationAndClosesEncoderBeforeTraining(tmp_path, monkeypatch):
    """坏 holdout 不妨碍完整研究；三种对照共享一次编码并先释放模型。"""
    fixture = _cliHarness(tmp_path, monkeypatch)
    originalFit = admission.fitAdmissionModel

    def fit(*args, **kwargs):
        """每次拟合都要求编码器已经释放。"""
        assert "encoder.close" in fixture.events
        fixture.events.append("fit")
        return originalFit(*args, **kwargs)

    monkeypatch.setattr(admission, "fitAdmissionModel", fit)
    assert admission.main() == 0
    serialized = fixture.output.read_text(encoding="utf-8")
    report = json.loads(serialized)
    assert report["split"] == "calibration"
    assert report["caseCount"] == 5
    assert report["productionEligible"] is False
    assert len(report["trials"]) == 3
    assert report["modelManifestSha256"] == hashlib.sha256(fixture.manifestPath.read_bytes()).hexdigest()
    assert report["calibrationSha256"] == admission.evaluation._casesDigest(admission.study.loadCalibrationCases(fixture.casesPath))
    assert "IGNORE_HOLDOUT" not in serialized
    assert "PRIVATE_HINT_MARKER" not in serialized
    assert fixture.events.count("dense.score") == 5
    assert fixture.events.index("scorer.clear") < fixture.events.index("encoder.close") < fixture.events.index("fit")
    assert not fixture.output.with_suffix(".json.tmp").exists()


@pytest.mark.parametrize("temporary", [False, True])
@pytest.mark.parametrize("target", ["cases", "manifest", "calibration", "formalManifest", "model"])
def test_cliProtectsFinalAndTemporaryOutputsBeforeInputs(tmp_path, monkeypatch, temporary, target):
    """原子写入的 final 与 tmp 都要保护，不能在拒绝前读取数据或加载模型。"""
    output = tmp_path / "report.json"
    protected = output.with_suffix(".json.tmp") if temporary else output
    protected.write_bytes(b"keep-original-input")
    paths = {name: tmp_path / f"{name}.json" for name in ("cases", "manifest", "calibration", "formalManifest")}
    modelDir = tmp_path if target == "model" else tmp_path / "model"
    if target != "model":
        paths[target] = protected
    monkeypatch.setattr(admission, "LLM_MEMORY_CALIBRATION_PATH", paths["calibration"])
    monkeypatch.setattr(admission, "LLM_MEMORY_MODEL_MANIFEST_PATH", paths["formalManifest"])
    monkeypatch.setattr(admission.study, "loadCalibrationCases", lambda path: pytest.fail("read input before protection"))
    monkeypatch.setattr(admission.study, "StudyEncoder", lambda **kwargs: pytest.fail("loaded model before protection"))
    monkeypatch.setattr("sys.argv", ["studyMemoryAdmission.py", "--cases", str(paths["cases"]), "--manifest", str(paths["manifest"]),
                                    "--model-dir", str(modelDir), "--output", str(output)])
    assert admission.main() == 1
    assert protected.read_bytes() == b"keep-original-input"
    if temporary:
        assert not output.exists()


@pytest.mark.parametrize("trigger", ["encoder.load", "dense.score", "study.complete"])
def test_cliOverBudgetWritesNoIncompleteQualityReport(tmp_path, monkeypatch, trigger):
    """加载、编码和研究完成后超限都仅写中止证据，不能发布部分质量成绩。"""
    fixture = _cliHarness(tmp_path, monkeypatch)
    originalStudy = admission.studyAdmission

    def study(*args, **kwargs):
        """保留完整计算，并模拟最后边界才发现历史峰值超限。"""
        result = originalStudy(*args, **kwargs)
        fixture.events.append("study.complete")
        return result

    def memoryInfo():
        """当前 RSS 回落后历史峰值仍能触发限制。"""
        return SimpleNamespace(rss=100, peak_wset=admission.MEMORY_LIMIT_BYTES + 1 if trigger in fixture.events else 120)

    monkeypatch.setattr(admission, "studyAdmission", study)
    monkeypatch.setattr(admission.evaluation.psutil, "Process", lambda: SimpleNamespace(memory_info=memoryInfo))
    assert admission.main() == 1
    report = json.loads(fixture.output.read_text(encoding="utf-8"))
    assert report["status"] == "aborted"
    assert report["productionEligible"] is False
    assert report["trials"] == []
    assert not {"baseline", "qualityGate", "threshold", "groupValidation"} & report.keys()
    assert report["rssBytes"]["atAbort"] == 100
    assert report["rssBytes"]["processPeakWorkingSet"] == admission.MEMORY_LIMIT_BYTES + 1
    assert fixture.events[-1] == "encoder.close"
    if trigger == "encoder.load":
        assert "dense.score" not in fixture.events


@pytest.mark.parametrize("safeReads,expectedCalls", [(0, 0), (1, 1)])
def test_budgetScorerStopsAtFirstExceededBoundary(safeReads, expectedCalls):
    """真实编码前后的采样均可中止，不允许继续后续推理。"""
    observations = iter([100] * safeReads + [admission.MEMORY_LIMIT_BYTES + 1])
    process = SimpleNamespace(memory_info=lambda: SimpleNamespace(rss=next(observations)))
    calls = []
    scorer = admission.BudgetScorer(SimpleNamespace(score=lambda texts, candidates: calls.append(texts)),
                                   lambda: admission.checkMemoryBudget(process, []))
    with pytest.raises(admission.AdmissionMemoryError):
        scorer.score(["问题"], [])
    assert len(calls) == expectedCalls


def test_emptyCaseDoesNotDiluteItsGroupsTrainingWeight():
    """没有标注候选的同组题不应削弱这个组已有训练证据的权重。"""
    cases = _cases()[2:5]
    features = _featureRows(cases)
    features[cases[1]["caseID"]] = {1: [3.0] * len(admission.FEATURE_NAMES), 2: [1.0] * len(admission.FEATURE_NAMES)}
    expected = admission.fitAdmissionModel(cases, features, "scores")
    emptyCase = deepcopy(cases[1])
    emptyCase["caseID"] = "empty-case-in-second-group"
    features[emptyCase["caseID"]] = {}
    actual = admission.fitAdmissionModel([*cases, emptyCase], features, "scores")
    assert actual["fitPairCount"] == expected["fitPairCount"]
    for name in ("mean", "scale", "coefficients", "intercept"):
        assert actual[name] == pytest.approx(expected[name])


@pytest.mark.parametrize("cohort", ["positive", "negative", "empty"])
def test_calibrationRequiresObservedPositiveAndForbiddenExamples(cohort):
    """校准缺一类时不能仅因观察到零错误就打开准入。"""
    cases = _cases()
    case = cases[0] if cohort == "positive" else cases[1]
    probabilities = {} if cohort == "empty" else {1: 0.9, 2: 0.8}
    threshold, detail = admission.chooseAdmissionThreshold([case], {case["caseID"]: probabilities})
    assert threshold is None
    assert detail["enabled"] is False
    assert detail["reason"] == "insufficient-calibration-classes"


@pytest.mark.parametrize("rss,peak", [(0, None), (512 * 1024 * 1024, None), (100, 512 * 1024 * 1024)])
def test_memoryBudgetBoundaryKeepsCurrentAndHistoricalValuesSeparate(rss, peak):
    """预算边界可以继续，RSS 采样序列不能混入历史峰值。"""
    samples = []
    admission.checkMemoryBudget(_memoryProcess(rss, peak), samples)
    assert samples == [rss]


@pytest.mark.parametrize("rss,peak", [(None, None), (-1, None), (float("nan"), None), (100, -1), (100, float("inf"))])
def test_memoryBudgetRejectsUnknownOrInvalidObservations(rss, peak):
    """未知资源状态不能被当作预算内零值，也不能污染采样序列。"""
    samples = []
    with pytest.raises(admission.evaluation.EvaluationError):
        admission.checkMemoryBudget(_memoryProcess(rss, peak), samples)
    assert samples == []




def _featureVariantFixture(cases):
    """为同一候选池生成 24/10/34 列独立矩阵，无编码器或真实数据依赖。"""
    variants = {}
    for variant, width in (("dense", 24), ("pair", 10), ("fusion", 34)):
        variants[variant] = {
            "names": tuple(f"{variant}{index}" for index in range(width)),
            "features": {
                case["caseID"]: {
                    memoryID: [float((caseIndex % 3) - memoryID) * (index + 1) / width for index in range(width)]
                    for memoryID in (1, 2)
                } for caseIndex, case in enumerate(cases)
            },
        }
    return variants


def _withoutTrialTiming(value):
    """仅忽略运行耗时，保留模型参数、预测、阈值及所有分组元数据。"""
    if isinstance(value, dict):
        return {key: _withoutTrialTiming(item) for key, item in value.items() if key != "seconds"}
    if isinstance(value, list):
        return [_withoutTrialTiming(item) for item in value]
    return value


@pytest.mark.parametrize("variant", list(admission.FEATURE_SETS))
def test_explicitFeatureSchemaPreservesLegacyNumericalResultsAndStoredMapping(variant):
    """原特征子集的独立矩阵必须拟合出相同参数，旧模型缺列号时仍能预测。"""
    cases = _cases()[2:5]
    features = _featureRows(cases)
    features[cases[1]["caseID"]][1] = [3.0] * len(admission.FEATURE_NAMES)
    legacy = admission.fitAdmissionModel(cases, features, variant)
    indices = admission._featureIndices(variant)
    projected = {caseID: {memoryID: [row[index] for index in indices] for memoryID, row in rows.items()}
                 for caseID, rows in features.items()}
    custom = admission.fitAdmissionModel(cases, projected, "external-schema", featureSchema=admission.FEATURE_SETS[variant])
    assert custom["featureNames"] == legacy["featureNames"]
    assert legacy["featureIndices"] == indices
    assert custom["featureIndices"] == list(range(len(indices)))
    assert custom["inputFeatureCount"] == len(indices)
    for name in ("mean", "scale", "coefficients", "intercept", "objective"):
        assert custom[name] == pytest.approx(legacy[name])
    predictions = admission.predictAdmission(legacy, features)
    assert admission.predictAdmission(custom, projected) == predictions
    legacy.pop("featureIndices")
    assert admission.predictAdmission(legacy, features) == predictions


def test_customSchemaUsesAllColumnsInDeclaredOrderWithoutGlobalMutation(monkeypatch):
    """新名称和非旧顺序均使用模型自带列号，全局特征字典不参与预测。"""
    cases = _cases()[2:5]
    schema = ("pairProbability", "currentDenseScore")
    features = {case["caseID"]: {1: [1.0, 8.0], 2: [-1.0, -2.0]} for case in cases}
    originalFeatures = deepcopy(features)
    originalSets = deepcopy(admission.FEATURE_SETS)
    model = admission.fitAdmissionModel(cases, features, "pair-plus-dense", featureSchema=schema)
    assert model["featureNames"] == list(schema)
    assert model["mean"] == pytest.approx([0.0, 3.0])
    reordered = {caseID: {memoryID: row[::-1] for memoryID, row in rows.items()} for caseID, rows in features.items()}
    reorderedModel = admission.fitAdmissionModel(cases, reordered, "reordered", featureSchema=schema[::-1])
    assert reorderedModel["mean"] == pytest.approx(model["mean"][::-1])
    monkeypatch.setattr(admission, "_featureIndices", lambda variant: pytest.fail("custom prediction used global feature schema"))
    assert admission.predictAdmission(model, features) == admission.predictAdmission(reorderedModel, reordered)
    assert features == originalFeatures
    assert admission.FEATURE_SETS == originalSets


@pytest.mark.parametrize("schema", [(), ("duplicate", "duplicate"), ("",), (" ",), (1,), ["list-instead-of-tuple"]])
def test_explicitFeatureSchemaRejectsEmptyDuplicateOrInvalidNames(schema):
    """显式协议拒绝空列、重名和非字符串列名，不得默默退回旧特征集。"""
    with pytest.raises(admission.evaluation.EvaluationError, match="schema"):
        admission.fitAdmissionModel(_cases()[2:4], {}, "custom", featureSchema=schema)


@pytest.mark.parametrize("damage", ["missing-case", "too-short", "too-long", "not-row"])
def test_explicitSchemaRejectsMalformedTrainingWidth(damage):
    """即使错列候选未标注，训练入口也不得接受与声明不一致的矩阵。"""
    cases = _cases()[2:4]
    features = {case["caseID"]: {1: [1.0, 2.0], 2: [-1.0, -2.0]} for case in cases}
    if damage == "missing-case":
        del features[cases[0]["caseID"]]
    else:
        features[cases[0]["caseID"]][999] = {
            "too-short": [1.0], "too-long": [1.0, 2.0, 3.0], "not-row": None,
        }[damage]
    with pytest.raises(admission.evaluation.EvaluationError):
        admission.fitAdmissionModel(cases, features, "custom", featureSchema=("a", "b"))


def test_explicitSchemaFitDoesNotReadHeldOutRows():
    """另一组的异常宽度和数值不能回流到当前拟合入口。"""
    cases = _cases()[2:5]
    features = {case["caseID"]: {1: [1.0], 2: [-1.0]} for case in cases}
    expected = admission.fitAdmissionModel(cases, features, "one-column", featureSchema=("score",))
    features["unseen-test-case"] = {1: [float("nan"), 99.0]}
    assert admission.fitAdmissionModel(cases, features, "one-column", featureSchema=("score",)) == expected


@pytest.mark.parametrize("enabled", [True, False])
def test_customPredictionChecksSchemaWidthEvenForDisabledModel(enabled):
    """自有 schema 的预测输入必须同宽，模型关闭不能掩盖缓存列错位。"""
    cases = _cases()[2:4]
    features = {case["caseID"]: {1: [1.0], 2: [-1.0]} for case in cases}
    model = admission.fitAdmissionModel(cases, features, "custom", featureSchema=("pairScore",))
    model["enabled"] = enabled
    features[cases[0]["caseID"]][1].append(0.0)
    with pytest.raises(admission.evaluation.EvaluationError, match="schema"):
        admission.predictAdmission(model, features)


def test_sharedEvaluationUsesIndependentSchemasWithIdenticalNestedGroups():
    """三种不同宽度的模型共用外折及校准主题；输入和全局列定义均不可变。"""
    cases = _cases()
    records = {case["caseID"]: _record(sameQuery=True) for case in cases}
    variants = _featureVariantFixture(cases)
    original = deepcopy((cases, records, variants, admission.FEATURE_SETS))
    report = admission.evaluateAdmissionFeatures(cases, records, variants)
    assert set(report) == {"baseline", "calibrationMatchedBaseline", "trials"}
    assert [trial["variant"] for trial in report["trials"]] == ["dense", "pair", "fusion"]
    for trial in report["trials"]:
        schema = variants[trial["variant"]]["names"]
        assert trial["model"]["featureNames"] == list(schema)
        assert trial["model"]["inputFeatureCount"] == len(schema)
        assert sum(fold["testCaseCount"] for fold in trial["folds"]) == len(cases)
        for fold, baselineFold, matchedFold in zip(trial["folds"], report["baseline"]["folds"], report["calibrationMatchedBaseline"]["folds"]):
            assert fold["testGroupIDs"] == baselineFold["testGroupIDs"]
            assert fold["calibrationGroupIDs"] == matchedFold["calibrationGroupIDs"]
            fit, calibrate, test = [set(fold[key]) for key in ("fitGroupIDs", "calibrationGroupIDs", "testGroupIDs")]
            assert fit and calibrate and test
            assert not (fit & calibrate or fit & test or calibrate & test)
            assert fit | calibrate | test == {case["groupID"] for case in cases}
            assert fold["model"]["featureNames"] == list(schema)
            assert fold["model"]["fitGroupIDs"] == sorted(fit)
    assert (cases, records, variants, admission.FEATURE_SETS) == original
    # 更改某组特征只能改变该组模型，不得被最后一个 schema 覆盖其他对照。
    variants["pair"]["features"][cases[0]["caseID"]][1][0] += 100
    changed = admission.evaluateAdmissionFeatures(cases, records, variants)
    for index in (0, 2):
        assert _withoutTrialTiming(changed["trials"][index]) == _withoutTrialTiming(report["trials"][index])
    assert changed["baseline"] == report["baseline"]
    assert changed["calibrationMatchedBaseline"] == report["calibrationMatchedBaseline"]


def test_sharedEvaluationKeepsHeldOutFeaturesAndLabelsOutOfFoldTraining():
    """外折测试组的各矩阵和标签改变，不得改变本折拟合参数及校准阈值。"""
    cases = _cases()
    records = {case["caseID"]: _record(sameQuery=True) for case in cases}
    variants = _featureVariantFixture(cases)
    before = admission.evaluateAdmissionFeatures(cases, records, variants)
    testGroups = set(before["trials"][0]["folds"][0]["testGroupIDs"])
    for case in cases:
        if case["groupID"] in testGroups:
            case.update(requiredIDs={2}, allowedIDs=set(), forbiddenIDs={1})
            for specification in variants.values():
                specification["features"][case["caseID"]] = {
                    1: [1e6] * len(specification["names"]), 2: [-1e6] * len(specification["names"]),
                }
    after = admission.evaluateAdmissionFeatures(cases, records, variants)
    for old, new in zip(before["trials"], after["trials"]):
        assert new["folds"][0]["model"] == old["folds"][0]["model"]
        assert new["folds"][0]["threshold"] == old["folds"][0]["threshold"]
        assert new["folds"][0]["calibration"] == old["folds"][0]["calibration"]


@pytest.mark.parametrize("damage", ["empty-variants", "missing-case", "extra-case", "missing-candidate", "extra-candidate", "bad-width", "missing-names"])
def test_sharedEvaluationRejectsMismatchedInputsBeforeBaseline(monkeypatch, damage):
    """候选池、场景或 schema 不一致时先中止，不能产出不可比较的质量结果。"""
    cases = _cases()
    records = {case["caseID"]: _record(sameQuery=True) for case in cases}
    variants = _featureVariantFixture(cases)
    pair = variants["pair"]
    if damage == "empty-variants":
        variants = {}
    elif damage == "missing-case":
        del pair["features"][cases[0]["caseID"]]
    elif damage == "extra-case":
        pair["features"]["other"] = {}
    elif damage == "missing-candidate":
        del pair["features"][cases[0]["caseID"]][2]
    elif damage == "extra-candidate":
        pair["features"][cases[0]["caseID"]][99] = [0.0] * 10
    elif damage == "bad-width":
        pair["features"][cases[0]["caseID"]][1].pop()
    else:
        del pair["names"]
    monkeypatch.setattr(admission.study, "studyScores", lambda *args: pytest.fail("baseline ran for mismatched variants"))
    with pytest.raises(admission.evaluation.EvaluationError):
        admission.evaluateAdmissionFeatures(cases, records, variants)


def test_sharedEvaluationRejectsHoldoutBeforeReadingFeatures():
    """共用函数不能成为绕过 calibration-only 边界的新入口。"""
    cases = _cases()
    cases[0]["split"] = "holdout"
    with pytest.raises(admission.evaluation.EvaluationError, match="calibration"):
        admission.evaluateAdmissionFeatures(cases, {}, {})


@pytest.mark.parametrize("stopAfterFit", [False, True])
def test_sharedEvaluationPropagatesBudgetFailureWithoutContinuing(monkeypatch, stopAfterFit):
    """初始或拟合后的预算错误必须传播，不能继续嵌套折并返回部分报告。"""
    cases = _cases()
    records = {case["caseID"]: _record(sameQuery=True) for case in cases}
    variants = _featureVariantFixture(cases)
    fitted = []
    realFit = admission.fitAdmissionModel

    def fit(*args, **kwargs):
        """记录实际完成的拟合边界，保持真实轻量 logistic。"""
        result = realFit(*args, **kwargs)
        fitted.append(result)
        return result

    def check():
        """模拟初始或第一个样本内拟合之后的资源超限。"""
        if not stopAfterFit or fitted:
            raise admission.AdmissionMemoryError(admission.MEMORY_LIMIT_BYTES + 1, None)

    monkeypatch.setattr(admission, "fitAdmissionModel", fit)
    with pytest.raises(admission.AdmissionMemoryError):
        admission.evaluateAdmissionFeatures(cases, records, variants, memoryCheck=check)
    assert len(fitted) == int(stopAfterFit)


def test_legacyStudyReportModelsStillPredictFromFullCandidateFeatures():
    """旧报告的完整矩阵仍可直接回放各子集模型，不要求调用者重建内部投影。"""
    cases = _cases()
    records = {case["caseID"]: _record(sameQuery=True) for case in cases}
    report = admission.studyAdmission(cases, records)
    features = report["candidateFeatures"]
    for trial in report["trials"]:
        variant = trial["variant"]
        direct = admission.fitAdmissionModel(cases, features, variant)
        assert trial["model"] == direct
        predictions = admission.predictAdmission(trial["model"], features)
        assert admission.replayAdmission(cases, records, predictions, trial["threshold"]) == trial["cases"]
