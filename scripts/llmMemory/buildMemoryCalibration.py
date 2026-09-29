"""组合人工可审查的主题素材，生成现有评测器可直接读取的大候选池 fixture。

只扩充 calibration；旧场景原样保留，holdout 不改题、不改答案。主题内的
难负例有逐题标注，跨域背景按显式域选择补齐，全部新数据仍待人工复核。
"""

import argparse
from collections import Counter
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import unicodedata

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from config import LLM_MEMORY_CALIBRATION_PATH, LLM_MEMORY_MODEL_MANIFEST_PATH
from scripts.llmMemory import evaluateMemory as evaluation


FIXTURE_ROOT = PROJECT_ROOT / "tests/utils/llm/memory/fixtures"
DEFAULT_BASE = FIXTURE_ROOT / "retrievalCases.json"
DEFAULT_SOURCES = FIXTURE_ROOT / "calibrationExpansion"
DEFAULT_OUTPUT = PROJECT_ROOT / ".cache/llmMemory/reports/retrievalExpandedCases.json"
DEFAULT_REPORT = PROJECT_ROOT / ".cache/llmMemory/reports/calibrationExpansion.json"
QUERY_NOW = "2026-01-01T12:00:00"
DEFAULT_CANDIDATE_COUNT = 80
PRIMARY_CATEGORIES = {
    "normalRecall", "noAnswer", "multiRequired", "misleadingHint",
    "reference", "oppositeFacts", "topicSwitch",
}


def _memoryTextKey(memory: dict) -> str:
    """规范化正文以发现用新 ID 重复同一事实的虚假扩容。"""
    return " ".join(unicodedata.normalize("NFKC", memory["content"]).split())


def _idSet(values, description: str) -> set[int]:
    """校验源素材的标签 ID，禁止布尔值、重复值和静默类型转换。"""
    if not isinstance(values, list) or any(type(value) is not int or value <= 0 for value in values):
        raise evaluation.EvaluationError(f"{description} 必须是正整数 ID 数组")
    if len(set(values)) != len(values):
        raise evaluation.EvaluationError(f"{description} 含重复 ID")
    return set(values)


def _collectGroups(sources: list[dict]) -> tuple[list[dict], dict[int, dict]]:
    """建立唯一事实库，验证源格式与背景域，不自动批准任何标注。"""
    groups = []
    memories = {}
    seenGroups = set()
    seenText = set()
    domains = set()
    for source in sources:
        if source.get("schemaVersion") != 1 or source.get("reviewStatus") != "draft":
            raise evaluation.EvaluationError("扩充源必须是 schemaVersion=1 的 draft")
        domain = source.get("domain")
        if not isinstance(domain, str) or not domain.strip() or domain in domains:
            raise evaluation.EvaluationError("每份素材必须声明唯一的非空 domain")
        domains.add(domain)
        rawGroups = source.get("groups")
        if not isinstance(rawGroups, list) or not rawGroups:
            raise evaluation.EvaluationError(f"{domain} 缺少 groups")
        for rawGroup in rawGroups:
            group = deepcopy(rawGroup)
            groupID = group.get("groupID")
            if not isinstance(groupID, str) or not groupID.strip() or groupID in seenGroups:
                raise evaluation.EvaluationError("groupID 缺失或重复")
            seenGroups.add(groupID)
            if group.get("primaryCategory") not in PRIMARY_CATEGORIES:
                raise evaluation.EvaluationError(f"{groupID} primaryCategory 无效")
            if not isinstance(group.get("cases"), list) or not group["cases"]:
                raise evaluation.EvaluationError(f"{groupID} 缺少 cases")
            rawMemories = group.get("memories")
            if not isinstance(rawMemories, list) or not rawMemories:
                raise evaluation.EvaluationError(f"{groupID} 缺少 memories")
            group["domain"] = domain
            for rawMemory in rawMemories:
                memory = evaluation._normalizeMemory(rawMemory, groupID)
                memoryID = memory["id"]
                if type(rawMemory.get("id")) is not int or memoryID in memories:
                    raise evaluation.EvaluationError(f"{groupID} memory ID 重复或类型无效")
                textKey = _memoryTextKey(memory)
                if textKey in seenText:
                    raise evaluation.EvaluationError(f"{groupID} 正文重复，不能靠更换 ID 扩容")
                if memory["scope_type"] != "global" or not memory["enabled"] or memory["mode"] != "contextual":
                    raise evaluation.EvaluationError(f"{groupID} 背景素材必须是可见 global contextual")
                seenText.add(textKey)
                memories[memoryID] = {"memory": deepcopy(rawMemory), "domain": domain, "groupID": groupID}
            groups.append(group)
    for group in groups:
        backgroundDomains = group.get("backgroundDomains")
        if not isinstance(backgroundDomains, list) or not backgroundDomains or not all(isinstance(value, str) for value in backgroundDomains):
            raise evaluation.EvaluationError(f"{group['groupID']} 缺少显式 backgroundDomains")
        # 域名只认源文件的顶层 domain；层级错误必须修素材，不能静默扩大
        # 负例池。跨域仅约束候选来源，不能证明语义无关。
        if len(set(backgroundDomains)) != len(backgroundDomains):
            raise evaluation.EvaluationError(f"{group['groupID']} 背景域重复")
        if group["domain"] in backgroundDomains:
            raise evaluation.EvaluationError(f"{group['groupID']} 背景域包含本域")
        unknownDomains = set(backgroundDomains) - domains
        if unknownDomains:
            raise evaluation.EvaluationError(f"{group['groupID']} 未知背景域: {sorted(unknownDomains)}")
    return sorted(groups, key=lambda group: group["groupID"]), memories


def _selectBackground(group: dict, inventory: dict, count: int) -> list[int]:
    """用固定散列顺序补背景，所有问法共用候选池，规模对照采用嵌套前缀。

    只从作者明确声明的其他域选取。跨域不保证无关，因此源素材可排除
    歧义背景，并逐题显式标记 allowedBackgroundIDs；其余背景按草稿禁入。
    """
    excluded = _idSet(group.get("excludedBackgroundIDs", []), "excludedBackgroundIDs")
    requestedDomains = set(group["backgroundDomains"])
    eligible = {
        memoryID for memoryID, entry in inventory.items()
        if entry["domain"] in requestedDomains and entry["domain"] != group["domain"]
    }
    if not excluded <= eligible:
        raise evaluation.EvaluationError(f"{group['groupID']} 排除的背景不在声明域内")
    eligible -= excluded
    allowed = set()
    for case in group["cases"]:
        allowed |= _idSet(case.get("allowedBackgroundIDs", []), "allowedBackgroundIDs")
    if not allowed <= eligible or len(allowed) > count:
        raise evaluation.EvaluationError(f"{group['groupID']} allowed 背景不在可用池内或超过容量")
    if count > len(eligible):
        raise evaluation.EvaluationError(f"{group['groupID']} 可用背景不足: {len(eligible)} < {count}")

    def order(memoryID: int) -> str:
        """固定组内顺序，与输入遍历顺序和模型分数无关。"""
        return hashlib.sha256(f"{group['groupID']}:{memoryID}".encode()).hexdigest()

    return sorted(allowed, key=order) + sorted(eligible - allowed, key=order)[:count - len(allowed)]


def _expandGroup(group: dict, inventory: dict, candidateCount: int) -> list[dict]:
    """保留本题难负例和完整标注，只在外围补齐候选规模。"""
    localIDs = {memory["id"] for memory in group["memories"]}
    if len(localIDs) > candidateCount:
        raise evaluation.EvaluationError(f"{group['groupID']} 本题事实已超过候选容量")
    background = _selectBackground(group, inventory, candidateCount - len(localIDs))
    excluded = set(group.get("excludedBackgroundIDs", []))
    resolvedDomains = sorted({
        entry["domain"] for memoryID, entry in inventory.items()
        if entry["domain"] in group["backgroundDomains"] and memoryID not in excluded
    })
    selectedDomains = sorted({inventory[memoryID]["domain"] for memoryID in background})
    cases = []
    for rawCase in group["cases"]:
        case = deepcopy(rawCase)
        required = _idSet(case.get("requiredIDs"), "requiredIDs")
        allowed = _idSet(case.get("allowedIDs"), "allowedIDs")
        forbidden = _idSet(case.get("forbiddenIDs"), "forbiddenIDs")
        if required & allowed or required & forbidden or allowed & forbidden or required | allowed | forbidden != localIDs:
            raise evaluation.EvaluationError(f"{case.get('caseID')} 本题标签必须互斥且完整覆盖所有事实")
        metadata = case.get("metadata", {})
        if metadata.get("reviewStatus") != "draft" or not str(metadata.get("annotationRationale", "")).strip():
            raise evaluation.EvaluationError(f"{case.get('caseID')} 缺少 draft 或标注理由")
        category = group["primaryCategory"]
        if category == "multiRequired" and len(required) < 2:
            raise evaluation.EvaluationError("multiRequired 每题至少两条必要事实")
        # 话题切换仍可能在新话题下有答案；是否可回答由逐题标签决定，
        # 只有 noAnswer 强制零正例。有效历史在规范化查询后单独验证。
        if category == "noAnswer" and (required or allowed or case.get("allowAbstain") is not True):
            raise evaluation.EvaluationError(f"{category} 必须无答案且允许弃权")
        backgroundAllowed = _idSet(case.pop("allowedBackgroundIDs", []), "allowedBackgroundIDs")
        if category == "noAnswer" and backgroundAllowed:
            raise evaluation.EvaluationError(f"{category} 不能包含 allowed 背景")
        case.update({
            "groupID": group["groupID"], "split": "calibration",
            "queryNow": QUERY_NOW, "scope": {},
            "allowedIDs": sorted(allowed | backgroundAllowed),
            "forbiddenIDs": sorted(forbidden | (set(background) - backgroundAllowed)),
            "memories": deepcopy(group["memories"]) + [deepcopy(inventory[memoryID]["memory"]) for memoryID in background],
        })
        # 不让“本题记忆总在前八条”成为隐含线索。相同组问法仍共用顺序，
        # 与背景选择一样不读取标签，也不根据模型成绩排序。
        case["memories"].sort(key=lambda memory: hashlib.sha256(f"{group['groupID']}:order:{memory['id']}".encode()).hexdigest())
        case["subsets"] = sorted(set(case.get("subsets", [])) | {category, "expandedCalibration", group["domain"]})
        case["metadata"] = {
            **metadata, "primaryCategory": category, "domain": group["domain"],
            "localCandidateCount": len(localIDs), "backgroundCount": len(background),
            "backgroundDomains": sorted(group["backgroundDomains"]),
            "resolvedBackgroundDomains": resolvedDomains,
            "selectedBackgroundDomains": selectedDomains,
            "backgroundDomainFallback": False,
            "excludedBackgroundIDs": sorted(excluded),
            "allowedBackgroundIDs": sorted(backgroundAllowed),
            "backgroundReviewStatus": "draft",
            "backgroundPolicy": "declared-cross-domain-forbidden-except-explicit-allowed-draft",
        }
        cases.append(case)
    normalizedCases = evaluation.validateEvaluationCases(cases)
    if group["primaryCategory"] == "topicSwitch":
        for case in normalizedCases:
            # 历史必须实际改变辅助检索文本；空或过期历史不能检验干扰。
            # 是否构成话题切换仍由语义审查决定，不能用结构校验代替人审。
            query = case["query"]
            withHistory = evaluation.buildQueryTexts(query, now=case["queryNow"])[1]
            withoutHistory = evaluation.buildQueryTexts(replace(query, history=()), now=case["queryNow"])[1]
            if not query.history or withHistory == withoutHistory:
                raise evaluation.EvaluationError(f"{case['caseID']} topicSwitch 缺少有效历史干扰")
    return cases


def buildCalibrationDataset(baseData: dict, sources: list[dict], *, candidateCount: int = DEFAULT_CANDIDATE_COUNT) -> tuple[dict, dict]:
    """返回完整 fixture 与覆盖报告；原始场景、标签和 holdout 保持原样。

    原 66 条小池保留为历史对照，不伪装为已经扩成大池；新增场景的每个
    候选都参与实际检索，而不是仅在报告中把候选数量写大。
    """
    if type(candidateCount) is not int or candidateCount < 1:
        raise evaluation.EvaluationError("candidateCount 必须是正整数")
    if not isinstance(baseData, dict) or baseData.get("schemaVersion") != 1 or not isinstance(baseData.get("cases"), list):
        raise evaluation.EvaluationError("基础 fixture 必须是 schemaVersion=1 的 cases 对象")
    dataset = deepcopy(baseData)
    baseCases = dataset["cases"]
    baseCalibration = evaluation.validateEvaluationCases([case for case in baseCases if case.get("split") == "calibration"])
    groups, inventory = _collectGroups(sources)
    # 防止用新 ID 复制旧 calibration 来扩大独立事实计数；holdout 正文
    # 不参与素材取舍，只保留其原始对象与完整性摘要。
    baseText = {_memoryTextKey(memory) for case in baseCalibration for memory in case["memories"]}
    if any(_memoryTextKey(entry["memory"]) in baseText for entry in inventory.values()):
        raise evaluation.EvaluationError("扩充正文重复已有 calibration，不能靠更换 ID 扩容")
    baseGroups = {case["groupID"] for case in baseCases}
    if baseGroups & {group["groupID"] for group in groups}:
        raise evaluation.EvaluationError("扩充 groupID 与已有场景重复")
    existingIDs = {memory["id"] for case in baseCases for memory in case["memories"]}
    if existingIDs & inventory.keys():
        raise evaluation.EvaluationError("扩充 memory ID 与已有场景冲突")
    newCases = [case for group in groups for case in _expandGroup(group, inventory, candidateCount)]
    if len({case["caseID"] for case in [*baseCases, *newCases]}) != len(baseCases) + len(newCases):
        raise evaluation.EvaluationError("扩充 caseID 与已有场景重复")
    dataset["cases"] = [*baseCases, *newCases]
    dataset["calibrationCaseCount"] = sum(case["split"] == "calibration" for case in dataset["cases"])
    dataset["holdoutCaseCount"] = sum(case["split"] == "holdout" for case in dataset["cases"])
    dataset["description"] = "校准扩充草稿；原始场景及旧 holdout 原样保留。新增主题的跨域背景与全部标注仍待人工复核。"
    calibrationCases = evaluation.validateEvaluationCases([case for case in dataset["cases"] if case["split"] == "calibration"])
    backgroundGroups = []
    for group in groups:
        groupCases = [case for case in newCases if case["groupID"] == group["groupID"]]
        metadata = groupCases[0]["metadata"]
        backgroundGroups.append({
            "groupID": group["groupID"], "domain": group["domain"],
            "declaredDomains": metadata["backgroundDomains"],
            "resolvedDomains": metadata["resolvedBackgroundDomains"],
            "selectedDomains": metadata["selectedBackgroundDomains"],
            "excludedIDs": metadata["excludedBackgroundIDs"],
            "selectedCount": metadata["backgroundCount"],
            "explicitAllowedIDs": sorted({
                memoryID for case in groupCases for memoryID in case["metadata"]["allowedBackgroundIDs"]
            }),
        })
    report = {
        "schemaVersion": 1, "status": "draft", "productionEligible": False,
        "newCaseCount": len(newCases), "newGroupCount": len(groups),
        "uniqueNewMemoryCount": len(inventory), "candidateCountPerNewCase": candidateCount,
        "calibrationCaseCount": len(calibrationCases), "holdoutCaseCount": dataset["holdoutCaseCount"],
        "newPrimaryCategoryCounts": dict(sorted(Counter(case["metadata"]["primaryCategory"] for case in newCases).items())),
        "candidateCountDistribution": dict(sorted(Counter(len(case["memories"]) for case in calibrationCases).items())),
        "multiRequiredCaseCount": sum(len(case["requiredIDs"]) > 1 for case in calibrationCases),
        "noAnswerCaseCount": sum(not case["requiredIDs"] and not case["allowedIDs"] for case in calibrationCases),
        "calibrationSha256": evaluation._casesDigest(calibrationCases),
        "sourceSha256": evaluation._canonicalDigest(sorted(sources, key=lambda source: source["domain"])),
        "preservedHoldoutSha256": evaluation._canonicalDigest([case for case in baseCases if case["split"] == "holdout"]),
        "backgroundSelection": {
            "unknownDomainPolicy": "reject", "fallbackGroupCount": 0, "fallbackCaseCount": 0,
            "reviewStatus": "draft", "groups": backgroundGroups,
            "automaticForbiddenLabelCount": sum(
                case["metadata"]["backgroundCount"] - len(case["metadata"]["allowedBackgroundIDs"])
                for case in newCases
            ),
            "note": "域不同不等于无关；自动 forbidden 是逐题待复核的草稿假设，不是人工批准。",
        },
        "note": "仅合成草稿。组内问法不是独立主题，背景事实会跨组复用；人工确认前不得批准 calibration。",
    }
    dataset["expansion"] = {"sourceSha256": report["sourceSha256"], "reviewStatus": "draft", "candidateCount": candidateCount}
    return dataset, report


def main() -> int:
    """从版本管理内的素材生成缓存产物，拒绝覆盖输入、源码及正式配置。"""
    parser = argparse.ArgumentParser(description="构造大候选池 memory calibration 草稿")
    parser.add_argument("--base", default=str(DEFAULT_BASE))
    parser.add_argument("--sources", default=str(DEFAULT_SOURCES))
    parser.add_argument("--candidates", type=int, default=DEFAULT_CANDIDATE_COUNT)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    args = parser.parse_args()
    try:
        sourceRoot = Path(args.sources).resolve()
        sourcePaths = sorted(sourceRoot.glob("*.json"))
        if not sourcePaths:
            raise evaluation.EvaluationError("没有扩充素材 JSON")
        protected = {Path(path).resolve() for path in [args.base, LLM_MEMORY_CALIBRATION_PATH, LLM_MEMORY_MODEL_MANIFEST_PATH, *sourcePaths]}
        rawOutputs = [Path(args.output), Path(args.report)]
        outputs = [path.resolve() for path in rawOutputs]
        # 报告写入器先覆盖原始输出名加 .tmp 的路径；必须在 resolve 前
        # 构造同一个临时名，防止别名解析后漏掉输入冲突或两份产物互相覆盖。
        temporaryOutputs = [path.with_suffix(path.suffix + ".tmp").resolve() for path in rawOutputs]
        writePaths = [*outputs, *temporaryOutputs]
        if (
            len(set(writePaths)) != len(writePaths)
            or any(path.suffix != ".json" for path in outputs)
            or any(path in protected or sourceRoot == path or sourceRoot in path.parents for path in writePaths)
        ):
            raise evaluation.EvaluationError("输出必须是不同的 JSON，不能覆盖输入、源素材目录或正式配置")
        baseData = json.loads(Path(args.base).read_text(encoding="utf-8"))
        sources = [json.loads(path.read_text(encoding="utf-8")) for path in sourcePaths]
        dataset, report = buildCalibrationDataset(baseData, sources, candidateCount=args.candidates)
        evaluation._writeReport(dataset, args.output)
        evaluation._writeReport(report, args.report)
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"校准数据扩充失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
