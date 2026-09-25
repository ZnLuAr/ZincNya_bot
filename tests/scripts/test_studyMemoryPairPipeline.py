"""验证串行研究的数据绑定、进程交接及失败隔离，不加载真实模型。"""

import json
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import studyMemoryPairPipeline as pipeline
from scripts import studyMemoryRetrieval as study
from tests.scripts.test_evaluateMemory import _case




def _writeJSON(path, value):
    """创建仅用于测试的 JSON 输入。"""
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _writeLines(path, rows):
    """写入按 pairID 关联的行式交接文件。"""
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _cacheFixture(tmpPath, *, mutateCases=None):
    """建立两个 calibration 场景及精确绑定的候选缓存。"""
    raw = [_case("multi", "calibration"), _case("empty", "calibration")]
    raw[0].update(requiredIDs=[1, 2], forbiddenIDs=[])
    raw[1].update(requiredIDs=[], forbiddenIDs=[1, 2], allowAbstain=True)
    cases = study.evaluation.validateEvaluationCases(raw)
    if mutateCases:
        mutateCases(cases)
    manifest = tmpPath / "dense.json"
    _writeJSON(manifest, {"revision": "fixed-revision", "encodingVersion": "encoding-v1"})
    report = {
        "reportType": "retrieval-improvement-study", "status": "experimental", "split": "calibration",
        "calibrationSha256": study.evaluation._casesDigest(cases),
        "modelManifestSha256": pipeline.fileDigest(manifest), "modelRevision": "fixed-revision",
        "baselineEncodingVersion": "encoding-v1",
        "candidateStudy": {"trials": [{"strategy": "union", "limitPerChannel": 32,
                                       "candidateIDsByCase": {case["caseID"]: [1, 2] for case in cases}}]},
    }
    path = tmpPath / "candidates.json"
    _writeJSON(path, report)
    return SimpleNamespace(cases=cases, raw=raw, manifest=manifest, report=report, path=path)


def test_cacheBindsDataModelAndUniqueUnionWithoutMutatingInputs(tmp_path):
    """缓存复用保留来源摘要，并且不把旧报告伪装成当前代码重新计算。"""
    fixture = _cacheFixture(tmp_path)
    before = deepcopy(fixture.cases)
    selected, provenance = pipeline.readCandidateSelection(fixture.cases, fixture.path, fixture.manifest)
    assert selected == {"multi": {1, 2}, "empty": {1, 2}}
    assert provenance["candidateReportSha256"] == pipeline.fileDigest(fixture.path)
    assert provenance["candidateGeneratorScriptSha256"] is None
    assert fixture.cases == before
    assert json.loads(fixture.path.read_text(encoding="utf-8")) == fixture.report


@pytest.mark.parametrize("field", ["reportType", "status", "split", "calibrationSha256", "modelManifestSha256", "modelRevision", "baselineEncodingVersion"])
def test_cacheRejectsChangedBinding(tmp_path, field):
    """任何身份不匹配均不能沿用旧候选。"""
    fixture = _cacheFixture(tmp_path)
    fixture.report[field] = "different"
    _writeJSON(fixture.path, fixture.report)
    with pytest.raises(ValueError):
        pipeline.readCandidateSelection(fixture.cases, fixture.path, fixture.manifest)


@pytest.mark.parametrize("shape", ["missing", "duplicate", "none", "nonlist", "nonobject", "wrong-limit", "wrong-strategy"])
def test_cacheRequiresOneWellFormedUnion32Trial(tmp_path, shape):
    """标准非候选报告的 null 以及重复对照都必须明确拒绝。"""
    fixture = _cacheFixture(tmp_path)
    trials = fixture.report["candidateStudy"]["trials"]
    if shape == "missing":
        trials.clear()
    elif shape == "duplicate":
        trials.append(deepcopy(trials[0]))
    elif shape == "none":
        fixture.report["candidateStudy"] = None
    elif shape == "nonlist":
        fixture.report["candidateStudy"]["trials"] = {}
    elif shape == "nonobject":
        trials.append(1)
    elif shape == "wrong-limit":
        trials[0]["limitPerChannel"] = 16
    else:
        trials[0]["strategy"] = "dense"
    _writeJSON(fixture.path, fixture.report)
    with pytest.raises(ValueError):
        pipeline.readCandidateSelection(fixture.cases, fixture.path, fixture.manifest)


@pytest.mark.parametrize("invalid", ["missing-case", "extra-case", "duplicate-id", "bool-id", "string-id", "outside-id", "over-capacity", "nonlist", "nondict"])
def test_cacheRejectsIncompleteCasesAndInvalidCandidateIDs(tmp_path, invalid):
    """缓存集合必须完整，ID 必须唯一、在范围内且不超每通道并集上限。"""
    fixture = _cacheFixture(tmp_path)
    trial = fixture.report["candidateStudy"]["trials"][0]
    selected = trial["candidateIDsByCase"]
    if invalid == "missing-case":
        del selected["empty"]
    elif invalid == "extra-case":
        selected["holdout"] = [1]
    elif invalid == "nondict":
        trial["candidateIDsByCase"] = []
    else:
        selected["multi"] = {"duplicate-id": [1, 1], "bool-id": [True], "string-id": ["1"],
                             "outside-id": [99], "over-capacity": list(range(97)), "nonlist": "1"}[invalid]
    _writeJSON(fixture.path, fixture.report)
    with pytest.raises(ValueError):
        pipeline.readCandidateSelection(fixture.cases, fixture.path, fixture.manifest)


@pytest.mark.parametrize("restriction", ["disabled", "pinned", "other-scope"])
def test_cacheRejectsCandidatesUnavailableToContextualRetrieval(tmp_path, restriction):
    """仅存在于 fixture 并不足够，缩池缓存也必须遵守线上 scope/mode。"""
    def mutate(cases):
        """保留标签而改变可见性，直接检验缓存边界。"""
        memory = cases[0]["memories"][0]
        memory.update({"disabled": {"enabled": False}, "pinned": {"mode": "pinned"},
                       "other-scope": {"scope_type": "chat", "scope_id": "999"}}[restriction])

    fixture = _cacheFixture(tmp_path, mutateCases=mutate)
    with pytest.raises(ValueError):
        pipeline.readCandidateSelection(fixture.cases, fixture.path, fixture.manifest)




def test_preparePairsDeduplicatesOnlyIdenticalTextAndExcludesHintsLabels(tmp_path, monkeypatch):
    """相同内容跨场景复用推理，case/channel/id 映射及标签隔离保持完整。"""
    fixture = _cacheFixture(tmp_path)
    for case in fixture.cases:
        case["memories"][0]["retrieval_hint"] = "must-not-leak-hint"
    monkeypatch.setattr(study, "buildQueryTexts", lambda query, **kwargs: ("current", "assisted", "unused"))
    before = deepcopy(fixture.cases)
    selected = {case["caseID"]: {1, 2} for case in fixture.cases}
    pairs, mapping = pipeline.preparePairs(fixture.cases, selected)
    assert len(pairs) == 4
    assert all(set(pair) == {"pairID", "query", "memory"} for pair in pairs)
    assert "must-not-leak-hint" not in json.dumps(pairs)
    assert "requiredIDs" not in json.dumps(pairs)
    assert mapping["multi"] == mapping["empty"]
    assert mapping["multi"]["sameQuery"] is False
    assert fixture.cases == before
    # 标签和 hint 改变不应改变送入模型的任何文本。
    altered = deepcopy(fixture.cases)
    altered[0]["requiredIDs"] = set()
    altered[0]["forbiddenIDs"] = {1, 2}
    altered[0]["memories"][0]["retrieval_hint"] = "different-hint"
    assert pipeline.preparePairs(altered, selected) == (pairs, mapping)
    scores = {pair["pairID"]: index / 10 for index, pair in enumerate(pairs)}
    records = pipeline.restoreRecords(mapping, scores)
    assert records["multi"]["scores"]["lexical"] == {}
    for channel in ("semanticCurrent", "semanticAssisted"):
        expected = {memoryID: scores[pairID] for memoryID, pairID in mapping["multi"]["channels"][channel].items()}
        assert records["multi"]["scores"][channel] == expected
        assert records["multi"]["ranking"][channel] == expected
        assert records["multi"]["ranking"][channel] is not records["multi"]["scores"][channel]


@pytest.mark.parametrize("query", ["same", ""])
def test_preparePairsKeepsSameQueryFlagAndEmptyQueryAbstention(tmp_path, monkeypatch, query):
    """相同文本只编码一次，空查询不虚构 pair；回放仍知道通道可接管。"""
    fixture = _cacheFixture(tmp_path)
    monkeypatch.setattr(study, "buildQueryTexts", lambda value, **kwargs: (query, query, "unused"))
    pairs, mapping = pipeline.preparePairs(fixture.cases, {case["caseID"]: {1} for case in fixture.cases})
    assert len(pairs) == bool(query)
    assert all(item["sameQuery"] for item in mapping.values())
    scores = {pair["pairID"]: 0.7 for pair in pairs}
    records = pipeline.restoreRecords(mapping, scores)
    expected = {1: 0.7} if query else {}
    assert records["multi"]["scores"] == {"semanticCurrent": expected, "semanticAssisted": expected, "lexical": {}}


@pytest.mark.parametrize("rows", [
    [], [{"pairID": "a", "score": 0.1}, {"pairID": "a", "score": 0.2}],
    [{"pairID": "other", "score": 0.1}], [{"pairID": "a", "score": True}],
    [{"pairID": "a", "score": "0.5"}], [{"pairID": "a", "score": float("nan")}],
    [{"pairID": "a", "score": float("inf")}], [{"pairID": "a", "score": -0.1}],
    [{"pairID": "a", "score": 1.1}],
])
def test_readScoresRejectsMissingExtraDuplicateOrInvalidScores(tmp_path, rows):
    """未完成或损坏交接不能进入阈值选择。"""
    path = tmp_path / "scores.jsonl"
    _writeLines(path, rows)
    with pytest.raises(ValueError):
        pipeline.readScores(path, {"a"})


def test_readScoresUsesPairIdentityInsteadOfRowOrder(tmp_path):
    """合法边界分数可以任意行序恢复，避免两个进程错配。"""
    path = tmp_path / "scores.jsonl"
    _writeLines(path, [{"pairID": "b", "score": 1}, {"pairID": "a", "score": 0}])
    assert pipeline.readScores(path, {"a", "b"}) == {"a": 0, "b": 1}




def _cliFixture(tmpPath, monkeypatch, *, failedStage=None, probeOnly=False):
    """创建真实交接文件，由 fake supervisor 模拟串行子进程契约。"""
    fixture = _cacheFixture(tmpPath)
    casesPath = tmpPath / "cases.json"
    _writeJSON(casesPath, {"cases": fixture.raw + [{"split": "holdout", "broken": "never-normalize-or-score"}]})
    pairManifest = tmpPath / "pair.json"
    _writeJSON(pairManifest, {"revision": "pair-revision"})
    scripts = tmpPath / "scripts"
    scripts.mkdir()
    for name in ("studyMemoryRetrieval.py", "probeMemoryReranker.py"):
        (scripts / name).write_text("# isolated test source\n", encoding="utf-8")
    monkeypatch.setattr(pipeline, "PROJECT_ROOT", tmpPath)
    monkeypatch.setattr(pipeline.probe, "PROJECT_ROOT", tmpPath)
    calls = []
    output = tmpPath / ".cache/llmMemory/reports/result.json"

    def supervise(command, **kwargs):
        """完整前阶段输出后才返回，后阶段必须使用同一已摘要绑定的文件。"""
        name = command[command.index("--worker") + 1]
        source = Path(command[command.index("--worker-input") + 1])
        target = Path(command[command.index("--worker-output") + 1])
        assert command[command.index("--input-sha") + 1] == pipeline.fileDigest(source)
        assert command[command.index("--manifest-sha") + 1] == pipeline.fileDigest(pairManifest)
        assert "--cases" not in command and "--candidate-report" not in command
        if name == "infer":
            assert calls == ["tokenize"]
        calls.append(name)
        if name == failedStage:
            return {"status": "aborted", "reason": "memory-budget-exceeded", "trials": [],
                    "rssBytes": {"conservativePeakSumMaximum": 600, "limit": 512}}
        rows = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()]
        if name == "tokenize":
            assert all(set(row) == {"pairID", "query", "memory"} for row in rows)
            results = [{"pairID": row["pairID"], "fields": {"input_ids": [1] * (256 if row["pairID"] == "max-length" else 2),
                        "attention_mask": [1], "token_type_ids": [0]}} for row in rows]
        else:
            assert all(set(row) == {"pairID", "fields"} for row in rows)
            results = [{"pairID": row["pairID"], "score": 0.8} for row in reversed(rows)]
        _writeLines(target, results)
        return {"status": "resource-probe-complete", "reason": None, "trials": [],
                "rssBytes": {"conservativePeakSumMaximum": 100, "limit": 512}}

    def studyScores(cases, records, floor):
        """只允许两个 calibration 场景在全部推理成功后进入验证。"""
        assert calls == ["tokenize", "infer"]
        assert {case["caseID"] for case in cases} == {"multi", "empty"}
        assert all(case["split"] == "calibration" for case in cases)
        assert set(records) == {"multi", "empty"}
        assert floor is None
        calls.append("studyScores")
        return {"groupValidation": {"syntheticTestOnly": True}}

    monkeypatch.setattr(pipeline.probe, "supervise", supervise)
    monkeypatch.setattr(study, "studyScores", studyScores)
    argv = ["studyMemoryPairPipeline.py", "--model-dir", str(tmpPath / "pair-model"),
            "--manifest", str(pairManifest), "--output", str(output), "--dense-manifest", str(fixture.manifest)]
    if probeOnly:
        argv.append("--probe-only")
    else:
        argv.extend(["--cases", str(casesPath), "--candidate-report", str(fixture.path)])
    monkeypatch.setattr(sys, "argv", argv)
    return SimpleNamespace(calls=calls, output=output, casesPath=casesPath, manifest=pairManifest, cache=fixture)


def test_mainRunsTwoSerialStagesBeforeCalibrationAndBindsHandoffs(tmp_path, monkeypatch):
    """完整交接恢复才评估，损坏 holdout 不参与准备或评分。"""
    fixture = _cliFixture(tmp_path, monkeypatch)
    assert pipeline.main() == 0
    assert fixture.calls == ["tokenize", "infer", "studyScores"]
    report = json.loads(fixture.output.read_text(encoding="utf-8"))
    assert report["status"] == "experimental"
    assert report["productionEligible"] is False
    assert report["scoredPairCount"] == report["uniquePairCount"]
    assert report["logicalChannelPairCount"] >= report["uniquePairCount"]
    assert report["candidateSource"]["candidateReportSha256"] == pipeline.fileDigest(fixture.cache.path)
    taskDir = Path(report["handoffs"]["directory"])
    for key, fileName in (("textsSha256", "texts.jsonl"), ("tokenizeOutputSha256", "encoded.jsonl"), ("inferOutputSha256", "scores.jsonl")):
        assert report["handoffs"][key] == pipeline.fileDigest(taskDir / fileName)
    assert len(report["trials"]) == 1
    assert not fixture.output.with_suffix(".json.tmp").exists()


@pytest.mark.parametrize("stage,expectedCalls", [("tokenize", ["tokenize"]), ("infer", ["tokenize", "infer"])])
def test_mainAbortsWithoutLaterStagesOrQualityResults(tmp_path, monkeypatch, stage, expectedCalls):
    """任一阶段失败都保留资源证据且不会拟合阈值。"""
    fixture = _cliFixture(tmp_path, monkeypatch, failedStage=stage)
    assert pipeline.main() == 1
    assert fixture.calls == expectedCalls
    report = json.loads(fixture.output.read_text(encoding="utf-8"))
    assert report["status"] == "aborted"
    assert report["reason"] == "memory-budget-exceeded"
    assert report["trials"] == []
    assert "pairRecords" not in report


def test_mainSyntheticProbeDoesNotReadCasesOrReportQuality(tmp_path, monkeypatch):
    """资源探测仅有两个合成输入及 token 边界，不触发数据集加载。"""
    fixture = _cliFixture(tmp_path, monkeypatch, probeOnly=True)
    monkeypatch.setattr(study, "loadCalibrationCases", lambda *args: pytest.fail("loaded dataset in probe"))
    assert pipeline.main() == 0
    assert fixture.calls == ["tokenize", "infer"]
    report = json.loads(fixture.output.read_text(encoding="utf-8"))
    assert report["status"] == "resource-probe-complete"
    assert report["trials"] == []
    assert report["probeInputs"] == [{"pairID": "short", "tokenCount": 2}, {"pairID": "max-length", "tokenCount": 256}]
    assert "calibrationSha256" not in report


@pytest.mark.parametrize("field", ["manifest", "dense_manifest", "cases", "candidate_report"])
@pytest.mark.parametrize("temporary", [False, True])
def test_validatePathsProtectsEveryInputFromFinalAndTemporaryOutput(tmp_path, monkeypatch, field, temporary):
    """原子输出临时路径同样不能覆盖 fixture、候选缓存或清单。"""
    monkeypatch.setattr(pipeline.probe, "PROJECT_ROOT", tmp_path)
    reports = tmp_path / ".cache/llmMemory/reports"
    reports.mkdir(parents=True)
    output = reports / "result.json"
    args = SimpleNamespace(output=str(output), manifest=str(tmp_path / "pair.json"), model_dir=str(tmp_path / "model"),
                           dense_manifest=str(tmp_path / "dense.json"), cases=str(tmp_path / "cases.json"),
                           candidate_report=str(tmp_path / "candidate.json"), probe_only=False)
    protected = output.with_suffix(".json.tmp") if temporary else output
    protected.write_bytes(b"preserve input")
    setattr(args, field, str(protected))
    with pytest.raises(ValueError):
        pipeline.validatePaths(args)
    assert protected.read_bytes() == b"preserve input"




def _workerFixture(tmpPath, monkeypatch, *, mode, rows, probeOnly=False):
    """使用轻量模型替身运行真实 worker 文件交接和阶段检查。"""
    source, target, manifestPath = (tmpPath / name for name in ("input.jsonl", "output.jsonl", "pair.json"))
    _writeLines(source, rows)
    manifest = {"maxTokens": 256, "revision": "fake-revision"}
    _writeJSON(manifestPath, manifest)
    calls = []
    model = SimpleNamespace(manifest=manifest, tokenizer=None, timings=[0.1])

    def tokenize(tokenizer, pairs):
        """记录真实 batch 边界，生成可精确比较的整数输入。"""
        assert tokenizer == "fake-tokenizer"
        calls.append(("tokenize", pairs))
        length = 256 if pairs[0][0] == "long" else 3
        return [{"input_ids": list(range(length)), "attention_mask": [1] * length, "token_type_ids": [0] * length}]

    def makeModel(modelDir, path, **kwargs):
        """推理进程必须显式禁用 tokenizer，并固定低内存 runtime。"""
        assert kwargs["loadTokenizer"] is False
        assert kwargs["runtimeProfile"] == "low-memory"
        calls.append("model")
        return model

    def scoreEncodedPairs(fields):
        """保留收到的完整张量字段，证明交接不重新编码。"""
        calls.append(("infer", deepcopy(fields)))
        assert len(fields) == 1
        return [0.75]

    model.scoreEncodedPairs = scoreEncodedPairs
    model.close = lambda: calls.append("close")
    monkeypatch.setattr(study, "readRerankerManifest", lambda *args: manifest)
    monkeypatch.setattr(study, "loadRerankerTokenizer", lambda *args: "fake-tokenizer")
    monkeypatch.setattr(study, "encodeRerankerPairs", tokenize)
    monkeypatch.setattr(study, "StudyReranker", makeModel)
    args = SimpleNamespace(worker=mode, worker_input=str(source), worker_output=str(target),
                           manifest=str(manifestPath), model_dir="unused-model-dir", probe_only=probeOnly,
                           manifest_sha=pipeline.fileDigest(manifestPath), input_sha=pipeline.fileDigest(source))
    return SimpleNamespace(args=args, target=target, source=source, manifestPath=manifestPath, calls=calls)


def test_tokenizeWorkerPreservesBatchOneAndPublishesOnlyCompleteHandoff(tmp_path, monkeypatch, capsys):
    """两对文本分别编码，完整结束后才原子发布带原 pairID 的整数。"""
    rows = [{"pairID": "short", "query": "short", "memory": "first"},
            {"pairID": "max-length", "query": "long", "memory": "second"}]
    fixture = _workerFixture(tmp_path, monkeypatch, mode="tokenize", rows=rows, probeOnly=True)
    assert pipeline.worker(fixture.args) == 0
    assert fixture.calls == [("tokenize", [("short", "first")]), ("tokenize", [("long", "second")])]
    output = [json.loads(line) for line in fixture.target.read_text(encoding="utf-8").splitlines()]
    assert [row["pairID"] for row in output] == ["short", "max-length"]
    assert [len(row["fields"]["input_ids"]) for row in output] == [3, 256]
    assert all(set(row) == {"pairID", "fields"} for row in output)
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[-1]["phase"] == "complete"
    assert events[-1]["pairCount"] == 2
    assert events[-1]["maximumTokens"] == 256
    assert not fixture.target.with_suffix(".jsonl.tmp").exists()


def test_inferWorkerReusesExactFieldsWithoutTokenizerAndClosesModel(tmp_path, monkeypatch, capsys):
    """推理收到与交接完全一致的三个字段，不需要文本或分词器。"""
    fields = {"input_ids": [4, 3, 2], "attention_mask": [1, 1, 0], "token_type_ids": [0, 1, 1]}
    fixture = _workerFixture(tmp_path, monkeypatch, mode="infer", rows=[{"pairID": "pair", "fields": fields}])
    monkeypatch.setattr(study, "loadRerankerTokenizer", lambda *args: pytest.fail("inference loaded tokenizer"))
    monkeypatch.setattr(study, "encodeRerankerPairs", lambda *args: pytest.fail("inference encoded text"))
    assert pipeline.worker(fixture.args) == 0
    assert fixture.calls == ["model", ("infer", [fields]), "close"]
    assert pipeline.readScores(fixture.target, {"pair"}) == {"pair": 0.75}
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert next(event for event in events if event["phase"] == "inference-ready")["tokenizerLoaded"] is False


@pytest.mark.parametrize("changed", ["manifest", "input"])
def test_workerRejectsChangedHandoffBeforeLoadingModel(tmp_path, monkeypatch, changed):
    """任一摘要变化均在昂贵资源分配和发布结果之前拒绝。"""
    fixture = _workerFixture(tmp_path, monkeypatch, mode="infer", rows=[])
    if changed == "manifest":
        fixture.manifestPath.write_text("{}", encoding="utf-8")
    else:
        fixture.source.write_text("changed", encoding="utf-8")
    assert pipeline.worker(fixture.args) == 1
    assert fixture.calls == []
    assert not fixture.target.exists()


@pytest.mark.parametrize("mode", ["tokenize", "infer"])
def test_workerRejectsDuplicatePairIDsWithoutPublishingPartialOutput(tmp_path, monkeypatch, mode):
    """已经评分一部分也不能用重复 ID 的临时输出冒充完整交接。"""
    row = {"pairID": "duplicate", "query": "short", "memory": "text",
           "fields": {"input_ids": [1], "attention_mask": [1], "token_type_ids": [0]}}
    fixture = _workerFixture(tmp_path, monkeypatch, mode=mode, rows=[row, row])
    assert pipeline.worker(fixture.args) == 1
    assert not fixture.target.exists()
    if mode == "infer":
        assert fixture.calls[-1] == "close"


def test_mainEvaluationErrorWritesFailureWithoutPartialQuality(tmp_path, monkeypatch):
    """回放异常使用结构化失败报告，绝不保留半份指标。"""
    fixture = _cliFixture(tmp_path, monkeypatch)

    def failEvaluation(*args):
        """模拟回放领域异常，区别于标准 ValueError。"""
        raise study.evaluation.EvaluationError("synthetic replay failure")

    monkeypatch.setattr(study, "studyScores", failEvaluation)
    assert pipeline.main() == 1
    assert fixture.calls == ["tokenize", "infer"]
    report = json.loads(fixture.output.read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["trials"] == []
    assert "pairRecords" not in report
