"""测试 memory 离线编码器基准入口。"""

import json
from types import SimpleNamespace

import pytest

from scripts.evaluateMemory import (
    EvaluationError,
    _chooseThreshold,
    _writeReport,
    calibrateRetrievalThresholds,
    evaluateRetrievalCases,
    loadApprovedCalibration,
    validateEvaluationCases,
    runEncoderBenchmark,
    runRetrievalBenchmark,
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
