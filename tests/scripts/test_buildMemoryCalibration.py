"""校准素材组合器的来源隔离、标注与可复现契约。"""

from copy import deepcopy
import json

import pytest

from scripts import buildMemoryCalibration as builder
from tests.scripts.test_evaluateMemory import _case




def _inputs():
    """构造三个不同域，各含独立正文和一题，避免依赖真实草稿标签。"""
    base = {"schemaVersion": 1, "cases": [_case("base", "calibration"), _case("held", "holdout")]}
    sources = []
    for index, domain in enumerate(("work", "travel", "leisure")):
        firstID = 100 + index * 10
        sources.append({
            "schemaVersion": 1, "reviewStatus": "draft", "domain": domain,
            "groups": [{
                "groupID": domain, "primaryCategory": "normalRecall",
                "backgroundDomains": [value for value in ("work", "travel", "leisure") if value != domain],
                "memories": [
                    {"id": firstID, "content": f"{domain} first fact"},
                    {"id": firstID + 1, "content": f"{domain} second fact"},
                ],
                "cases": [{
                    "caseID": f"{domain}-case", "query": {"currentText": f"{domain} question"},
                    "requiredIDs": [firstID], "allowedIDs": [], "forbiddenIDs": [firstID + 1],
                    "allowAbstain": False,
                    "metadata": {"reviewStatus": "draft", "annotationRationale": "First fact answers the question."},
                }],
            }],
        })
    return base, sources


def _newCase(dataset, groupID="work"):
    """按组取得生成题，避免测试依赖输入或输出排列。"""
    return next(case for case in dataset["cases"] if case["groupID"] == groupID)


def test_declaredDomainsSelectOnlyTheirMemoriesAndReportNoFallback():
    """未声明域不得补池，报告区分允许来源与实际被选来源。"""
    base, sources = _inputs()
    sources[0]["groups"][0]["backgroundDomains"] = ["travel"]
    dataset, report = builder.buildCalibrationDataset(base, sources, candidateCount=4)
    case = _newCase(dataset)
    assert {memory["id"] for memory in case["memories"]} == {100, 101, 110, 111}
    assert case["metadata"]["resolvedBackgroundDomains"] == ["travel"]
    assert case["metadata"]["selectedBackgroundDomains"] == ["travel"]
    assert case["metadata"]["backgroundDomainFallback"] is False
    assert report["backgroundSelection"]["fallbackGroupCount"] == 0
    assert report["backgroundSelection"]["fallbackCaseCount"] == 0
    assert report["backgroundSelection"]["automaticForbiddenLabelCount"] == 6


@pytest.mark.parametrize("domains", [["unknown"], ["travel", "unknown"], ["work"], ["travel", "travel"], []])
def test_invalidDomainDeclarationsAreRejected(domains):
    """未知域、混合未知域、本域和重复声明均不能静默退化。"""
    base, sources = _inputs()
    sources[0]["groups"][0]["backgroundDomains"] = domains
    with pytest.raises(builder.evaluation.EvaluationError):
        builder.buildCalibrationDataset(base, sources, candidateCount=4)


@pytest.mark.parametrize("candidateCount", [0, -1, True, 1.5, 1, 7])
def test_invalidOrInsufficientCapacityIsRejected(candidateCount):
    """容量不能裁掉本题事实，也不能重复填充不足的背景。"""
    base, sources = _inputs()
    with pytest.raises(builder.evaluation.EvaluationError):
        builder.buildCalibrationDataset(base, sources, candidateCount=candidateCount)


@pytest.mark.parametrize("labels", [
    {"allowedIDs": [100]}, {"forbiddenIDs": [100, 101]},
    {"allowedIDs": [101]}, {"forbiddenIDs": []},
    {"requiredIDs": [999]}, {"requiredIDs": [100, 100]}, {"requiredIDs": [True]},
])
def test_labelsMustBeMutuallyExclusiveAndComplete(labels):
    """任意两类重叠、漏标、越界或重复 ID 都是数据错误。"""
    base, sources = _inputs()
    sources[0]["groups"][0]["cases"][0].update(labels)
    with pytest.raises(builder.evaluation.EvaluationError):
        builder.buildCalibrationDataset(base, sources, candidateCount=4)


@pytest.mark.parametrize("duplicate", ["work first fact", "  work\tfirst   fact  ", "ｗｏｒｋ first fact"])
def test_duplicateNormalizedBodyIsRejectedAcrossDomains(duplicate):
    """新 ID、空白及全角变化不能冒充新增事实。"""
    base, sources = _inputs()
    sources[1]["groups"][0]["memories"][0]["content"] = duplicate
    with pytest.raises(builder.evaluation.EvaluationError, match="正文重复"):
        builder.buildCalibrationDataset(base, sources, candidateCount=4)


def test_inputsAreUnchangedAndHoldoutIsCopiedWithoutCheckingItsAnswers():
    """只校验 calibration，旧 holdout 的答案字段作为不透明数据原样保留。"""
    base, sources = _inputs()
    base["cases"][1]["requiredIDs"] = {"unread": "do not validate holdout labels"}
    original = deepcopy((base, sources))
    dataset, report = builder.buildCalibrationDataset(base, sources, candidateCount=4)
    assert (base, sources) == original
    assert dataset["cases"][:2] == base["cases"]
    assert report["preservedHoldoutSha256"] == builder.evaluation._canonicalDigest([base["cases"][1]])
    _newCase(dataset)["memories"][0]["content"] = "changed output only"
    assert (base, sources) == original


def test_newIDsCannotCopyOldCalibrationBodies():
    """旧 calibration 正文换 ID 也不算新增事实；检查无需借用 holdout。"""
    base, sources = _inputs()
    sources[0]["groups"][0]["memories"][0]["content"] = base["cases"][0]["memories"][0]["content"]
    with pytest.raises(builder.evaluation.EvaluationError, match="正文重复已有 calibration"):
        builder.buildCalibrationDataset(base, sources, candidateCount=4)


def test_distinctAllowedBackgroundsSharePoolButNotPermissions():
    """同组多个题目的允许背景取并集入池，仍分别标注且检查并集容量。"""
    base, sources = _inputs()
    group = sources[0]["groups"][0]
    group["cases"].append(deepcopy(group["cases"][0]))
    group["cases"][1]["caseID"] = "work-variant"
    group["cases"][0]["allowedBackgroundIDs"] = [110]
    group["cases"][1]["allowedBackgroundIDs"] = [120]
    with pytest.raises(builder.evaluation.EvaluationError, match="超过容量"):
        builder.buildCalibrationDataset(base, sources, candidateCount=3)
    dataset, _ = builder.buildCalibrationDataset(base, sources, candidateCount=4)
    cases = [case for case in dataset["cases"] if case["groupID"] == "work"]
    assert cases[0]["memories"] == cases[1]["memories"]
    assert cases[0]["allowedIDs"] == [110] and 120 in cases[0]["forbiddenIDs"]
    assert cases[1]["allowedIDs"] == [120] and 110 in cases[1]["forbiddenIDs"]


def test_reproducibleWithReorderedSourcesAndNestedCandidatePools():
    """相同输入可重放，源文件枚举顺序不改变结果；扩大池只追加背景。"""
    base, sources = _inputs()
    small, report = builder.buildCalibrationDataset(base, sources, candidateCount=3)
    assert (small, report) == builder.buildCalibrationDataset(base, deepcopy(sources), candidateCount=3)
    assert (small, report) == builder.buildCalibrationDataset(base, list(reversed(sources)), candidateCount=3)
    large, _ = builder.buildCalibrationDataset(base, sources, candidateCount=5)
    for source in sources:
        groupID = source["domain"]
        assert {memory["id"] for memory in _newCase(small, groupID)["memories"]} < {
            memory["id"] for memory in _newCase(large, groupID)["memories"]
        }


def test_exclusionsAndPerCaseBackgroundAllowanceAreExplicit():
    """例外背景必须入池；同组共享候选，但 allowed 权限仅属于声明题。"""
    base, sources = _inputs()
    group = sources[0]["groups"][0]
    group["excludedBackgroundIDs"] = [110, 111]
    group["cases"].append(deepcopy(group["cases"][0]))
    group["cases"][1]["caseID"] = "work-variant"
    group["cases"][0]["allowedBackgroundIDs"] = [120]
    dataset, report = builder.buildCalibrationDataset(base, sources, candidateCount=3)
    first = _newCase(dataset)
    second = next(case for case in dataset["cases"] if case["caseID"] == "work-variant")
    assert first["memories"] == second["memories"]
    assert first["allowedIDs"] == [120]
    assert 120 in second["forbiddenIDs"]
    assert first["metadata"]["resolvedBackgroundDomains"] == ["leisure"]
    assert first["metadata"]["selectedBackgroundDomains"] == ["leisure"]
    assert next(group for group in report["backgroundSelection"]["groups"] if group["groupID"] == "work")["excludedIDs"] == [110, 111]


@pytest.mark.parametrize("excluded,allowed,count", [([999], [], 4), ([110], [110], 4), ([], [100], 4), ([], [110, 111], 3)])
def test_invalidBackgroundExceptionsAreRejected(excluded, allowed, count):
    """本域、已排除、未知或超容量的 allowed 均不能绕过域边界。"""
    base, sources = _inputs()
    group = sources[0]["groups"][0]
    group["excludedBackgroundIDs"] = excluded
    group["cases"][0]["allowedBackgroundIDs"] = allowed
    with pytest.raises(builder.evaluation.EvaluationError):
        builder.buildCalibrationDataset(base, sources, candidateCount=count)


@pytest.mark.parametrize("queryShape", ["flat", "turns"])
@pytest.mark.parametrize("answerKind", ["required", "allowed", "background", "none"])
def test_topicSwitchAllowsCurrentAnswersAndAbstention(queryShape, answerKind):
    """新话题可有必要答案或辅助证据，也可无答案；分类不能强制弃权。"""
    base, sources = _inputs()
    group = sources[0]["groups"][0]
    group["primaryCategory"] = "topicSwitch"
    case = group["cases"][0]
    case["query"]["history"] = [{
        "content": "previous travel second fact topic", "direction": "incoming",
        "timestamp": "2026-01-01T11:55:00",
    }]
    if queryShape == "turns":
        case["query"]["turns"] = [{"currentText": case["query"].pop("currentText")}]
    if answerKind == "allowed":
        case.update(requiredIDs=[], allowedIDs=[100], allowAbstain=True)
    elif answerKind == "background":
        case["allowedBackgroundIDs"] = [110]
    elif answerKind == "none":
        case.update(requiredIDs=[], forbiddenIDs=[100, 101], allowAbstain=True)
    original = deepcopy((base, sources))
    dataset, report = builder.buildCalibrationDataset(base, sources, candidateCount=4)
    expanded = _newCase(dataset)
    expectedRequired = [100] if answerKind in {"required", "background"} else []
    expectedAllowed = {"required": [], "allowed": [100], "background": [110], "none": []}[answerKind]
    assert expanded["requiredIDs"] == expectedRequired
    assert expanded["allowedIDs"] == expectedAllowed
    assert expanded["allowAbstain"] is (answerKind in {"allowed", "none"})
    assert expanded["metadata"]["primaryCategory"] == "topicSwitch"
    assert report["noAnswerCaseCount"] == (1 if answerKind == "none" else 0)
    assert (base, sources) == original


@pytest.mark.parametrize("historyKind", [
    "absent", "empty", "blank", "expired", "missingTimestamp",
    "invalidTimestamp", "future", "reaction", "duplicateCurrent",
])
def test_topicSwitchNeedsHistoryThatActuallyReachesAssistedQuery(historyKind):
    """引用文本或不可用历史不能冒充切换证据，不能只比较 current 与 assisted。"""
    base, sources = _inputs()
    group = sources[0]["groups"][0]
    group["primaryCategory"] = "topicSwitch"
    case = group["cases"][0]
    case["query"]["replyText"] = "quoted context already changes assisted query"
    historyMessage = {
        "content": "previous travel first fact topic", "direction": "incoming",
        "timestamp": "2026-01-01T11:55:00",
    }
    overrides = {
        "blank": {"content": " \t\n "},
        "expired": {"timestamp": "2025-11-01T11:55:00"},
        "missingTimestamp": {"timestamp": None},
        "invalidTimestamp": {"timestamp": "not-a-time"},
        "future": {"timestamp": "2026-01-01T12:01:00"},
        "reaction": {"direction": "reaction"},
        "duplicateCurrent": {"content": case["query"]["currentText"]},
    }
    if historyKind != "absent":
        historyMessage.update(overrides.get(historyKind, {}))
        case["query"]["history"] = [] if historyKind == "empty" else [historyMessage]
    with pytest.raises(builder.evaluation.EvaluationError, match="有效历史"):
        builder.buildCalibrationDataset(base, sources, candidateCount=4)


@pytest.mark.parametrize("invalidField", ["requiredIDs", "allowedIDs", "allowedBackgroundIDs", "allowAbstain"])
def test_noAnswerStillRejectsPositiveEvidenceAndMandatoryAnswers(invalidField):
    """放宽话题切换不能放宽纯无答案的本地、背景正标签或弃权要求。"""
    base, sources = _inputs()
    group = sources[0]["groups"][0]
    group["primaryCategory"] = "noAnswer"
    case = group["cases"][0]
    case.update(requiredIDs=[], forbiddenIDs=[100, 101], allowAbstain=True)
    if invalidField == "allowAbstain":
        case[invalidField] = False
    elif invalidField == "allowedBackgroundIDs":
        case[invalidField] = [110]
    else:
        case[invalidField] = [100]
        case["forbiddenIDs"] = [101]
    with pytest.raises(builder.evaluation.EvaluationError, match="noAnswer"):
        builder.buildCalibrationDataset(base, sources, candidateCount=4)


def test_noAnswerWithoutHistoryRemainsValid():
    """纯无答案无需伪造话题历史，与切换题的结构契约分开。"""
    base, sources = _inputs()
    group = sources[0]["groups"][0]
    group["primaryCategory"] = "noAnswer"
    group["cases"][0].update(requiredIDs=[], forbiddenIDs=[100, 101], allowAbstain=True)
    dataset, report = builder.buildCalibrationDataset(base, sources, candidateCount=4)
    case = _newCase(dataset)
    assert case["requiredIDs"] == case["allowedIDs"] == []
    assert report["noAnswerCaseCount"] == 1


@pytest.mark.parametrize("target", ["base", "source", "calibration", "manifest", "same", "sourceDirectory", "python"])
def test_cliProtectsSourcesAndConfiguration(tmp_path, monkeypatch, target):
    """拒绝危险输出时不能先覆盖输入；CLI 的路径契约独立于模型。"""
    sourceRoot = tmp_path / "sources"
    sourceRoot.mkdir()
    source = sourceRoot / "one.json"
    source.write_text("{}", encoding="utf-8")
    base = tmp_path / "base.json"
    base.write_text("{}", encoding="utf-8")
    calibration = tmp_path / "calibration.json"
    manifest = tmp_path / "manifest.json"
    monkeypatch.setattr(builder, "LLM_MEMORY_CALIBRATION_PATH", calibration)
    monkeypatch.setattr(builder, "LLM_MEMORY_MODEL_MANIFEST_PATH", manifest)
    report = tmp_path / "report.json"
    targets = {"base": base, "source": source, "calibration": calibration, "manifest": manifest,
               "same": report, "sourceDirectory": sourceRoot / "new.json", "python": tmp_path / "code.py"}
    monkeypatch.setattr("sys.argv", ["buildMemoryCalibration.py", "--base", str(base), "--sources", str(sourceRoot),
                                   "--output", str(targets[target]), "--report", str(report)])
    assert builder.main() == 1
    assert base.read_text(encoding="utf-8") == "{}"
    assert source.read_text(encoding="utf-8") == "{}"
    assert not report.exists()


@pytest.mark.parametrize("destination", ["output", "report"])
@pytest.mark.parametrize("target", ["base", "calibration", "manifest"])
def test_cliProtectsTemporaryPathsBeforeBuilding(tmp_path, monkeypatch, destination, target):
    """任一报告的临时文件撞到输入或正式配置时，构建前拒绝且不改原字节。"""
    sourceRoot = tmp_path / "sources"
    sourceRoot.mkdir()
    source = sourceRoot / "one.json"
    source.write_bytes(b"{}\n")
    outputs = {name: tmp_path / f"{name}.json" for name in ("output", "report")}
    protected = {name: tmp_path / f"{name}.json" for name in ("base", "calibration", "manifest")}
    protected[target] = outputs[destination].with_suffix(".json.tmp")
    for name, path in protected.items():
        path.write_bytes(json.dumps({"sentinel": name}).encode("utf-8"))
    original = {path: path.read_bytes() for path in [source, *protected.values()]}
    monkeypatch.setattr(builder, "LLM_MEMORY_CALIBRATION_PATH", protected["calibration"])
    monkeypatch.setattr(builder, "LLM_MEMORY_MODEL_MANIFEST_PATH", protected["manifest"])
    monkeypatch.setattr(builder, "buildCalibrationDataset", lambda *args, **kwargs: pytest.fail("started build"))
    monkeypatch.setattr("sys.argv", [
        "buildMemoryCalibration.py", "--base", str(protected["base"]), "--sources", str(sourceRoot),
        "--output", str(outputs["output"]), "--report", str(outputs["report"]),
    ])
    assert builder.main() == 1
    assert all(path.read_bytes() == content for path, content in original.items())
    assert all(not path.exists() for path in outputs.values())


def test_checkedInSourcesBuildAsDraftAndPreserveExistingCases():
    """真实素材须通过同一组合链，但结构通过不意味着标注已获人审批准。"""
    base = json.loads(builder.DEFAULT_BASE.read_text(encoding="utf-8"))
    sources = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(builder.DEFAULT_SOURCES.glob("*.json"))]
    dataset, report = builder.buildCalibrationDataset(base, sources)
    assert dataset["cases"][:len(base["cases"])] == base["cases"]
    newCases = dataset["cases"][len(base["cases"]):]
    assert report["status"] == "draft" and report["productionEligible"] is False
    assert report["newCaseCount"] == len(newCases)
    for case in newCases:
        assert case["split"] == "calibration"
        assert case["metadata"]["reviewStatus"] == "draft"
        assert len(case["memories"]) == builder.DEFAULT_CANDIDATE_COUNT
        labels = case["requiredIDs"] + case["allowedIDs"] + case["forbiddenIDs"]
        assert len(labels) == len(set(labels)) == len(case["memories"])
