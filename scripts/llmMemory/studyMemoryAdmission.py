"""只在 calibration 研究轻量联合准入；拟合、校准、验证按主题分离。

特征是相关性代理，不是人物/属性/时间语义解析器。实验不改标签、不训练
embedding，也不输出生产 calibration。所有预先声明的对照都要报告。
"""

import argparse
import hashlib
import json
import math
import re
import sys
import time
from collections import Counter
from pathlib import Path
from statistics import median

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from config import LLM_MEMORY_CALIBRATION_PATH, LLM_MEMORY_MODEL_DIR, LLM_MEMORY_MODEL_MANIFEST_PATH
from scripts.llmMemory import studyMemoryRetrieval as study
from utils.llm.memory.lexical import tokenizeMemoryText
from utils.llm.memory.retrieval import buildQueryTexts


evaluation = study.evaluation
FEATURE_VERSION = "memory-admission-proxies-v1"
CANDIDATE_LIMIT = 32
INNER_CALIBRATION_STRIDE = 4
REGULARIZATION = 0.1
MAX_ITERATIONS = 80
CONVERGENCE_TOLERANCE = 1e-8
MEMORY_LIMIT_BYTES = 512 * 1024 * 1024
SCORE_FEATURES = (
    "currentBase", "assistedBase", "lexicalLog", "currentPresent",
    "assistedPresent", "lexicalPresent", "historyDelta", "differentQuery",
)
RELATIVE_FEATURES = (
    "currentRankFraction", "assistedRankFraction", "lexicalRankFraction",
    "currentMedianGap", "assistedMedianGap", "currentBestOtherGap",
    "assistedBestOtherGap", "lexicalMaxFraction",
)
SURFACE_FEATURES = (
    "currentBodyCoverage", "assistedBodyCoverage", "currentTagCoverage",
    "currentAsciiCoverage", "currentNumericCoverage", "queryHasAscii",
    "queryHasNumbers", "bodyTokenLog",
)
FEATURE_NAMES = SCORE_FEATURES + RELATIVE_FEATURES + SURFACE_FEATURES
FEATURE_SETS = {
    "scores": SCORE_FEATURES,
    "relative": SCORE_FEATURES + RELATIVE_FEATURES,
    "surface": FEATURE_NAMES,
}




def _relativeFeatures(scores: dict, memoryID: int) -> tuple[float, float, float]:
    """按同分同名次计算相对位置；单候选没有可观测的领先优势。"""
    if memoryID not in scores:
        return 0.0, 0.0, 0.0
    value = scores[memoryID]
    values = list(scores.values())
    others = [score for itemID, score in scores.items() if itemID != memoryID]
    return (
        sum(score < value for score in values) / max(1, len(values) - 1),
        value - median(values),
        value - max(others) if others else 0.0,
    )


def _coverage(queryTokens: set, bodyTokens: set) -> float:
    """空查询词集表示没有该项证据，不能作为完整匹配。"""
    return len(queryTokens & bodyTokens) / len(queryTokens) if queryTokens else 0.0


def buildCandidateFeatures(case: dict, record: dict, selectedIDs: set[int]) -> dict:
    """从 base、完整候选池分布和正文提取无标签特征，不读取 hint/元数据。

    ASCII/数字只是词项重合，不能解释为人名、日期或属性语义匹配。
    ID 仅定位记忆，任何同分相对特征都不通过 ID 排序制造差异。
    """
    current, assisted, _ = buildQueryTexts(case["query"], now=case.get("queryNow"))
    candidates = evaluation._contextualCandidates(evaluation._scopeCandidates(case))
    byID = {int(memory["id"]): memory for memory in candidates}
    # 非有限分数在特征边界拒绝，不能把坏分数归零后继续拟合。
    channels = record["scores"]
    if any(not math.isfinite(value) for scores in channels.values() for value in scores.values()):
        raise evaluation.EvaluationError("准入特征需要有限分数")
    if not selectedIDs <= byID.keys():
        raise evaluation.EvaluationError("候选特征不能包含 scope 外或 pinned 记忆")
    currentScores = channels["semanticCurrent"]
    assistedScores = channels["semanticAssisted"]
    lexicalScores = channels["lexical"]
    currentTokens = set(tokenizeMemoryText(current))
    assistedTokens = set(tokenizeMemoryText(assisted))
    asciiTokens = {token for token in currentTokens if re.fullmatch(r"[a-z0-9]+", token)}
    numericTokens = {token for token in asciiTokens if any(char.isdigit() for char in token)}
    lexicalMaximum = max(lexicalScores.values(), default=0.0)
    features = {}
    for memoryID in sorted(selectedIDs):
        memory = byID[memoryID]
        bodyTokens = set(tokenizeMemoryText(memory["content"]))
        tagTokens = set(tokenizeMemoryText(" ".join(memory.get("tags") or [])))
        currentScore = currentScores.get(memoryID, 0.0)
        assistedScore = assistedScores.get(memoryID, 0.0)
        lexicalScore = lexicalScores.get(memoryID, 0.0)
        currentRank, currentMedian, currentGap = _relativeFeatures(currentScores, memoryID)
        assistedRank, assistedMedian, assistedGap = _relativeFeatures(assistedScores, memoryID)
        lexicalRank, _, _ = _relativeFeatures(lexicalScores, memoryID)
        features[memoryID] = [
            currentScore, assistedScore, math.log1p(max(0.0, lexicalScore)),
            float(memoryID in currentScores), float(memoryID in assistedScores),
            float(memoryID in lexicalScores),
            assistedScore - currentScore if not record["sameQuery"] and memoryID in currentScores and memoryID in assistedScores else 0.0,
            float(not record["sameQuery"]),
            currentRank, assistedRank, lexicalRank, currentMedian, assistedMedian,
            currentGap, assistedGap, lexicalScore / lexicalMaximum if lexicalMaximum > 0 else 0.0,
            _coverage(currentTokens, bodyTokens), _coverage(assistedTokens, bodyTokens),
            _coverage(currentTokens, tagTokens), _coverage(asciiTokens, bodyTokens),
            _coverage(numericTokens, bodyTokens), float(bool(asciiTokens)),
            float(bool(numericTokens)), math.log1p(len(bodyTokens)),
        ]
    return features


def _featureIndices(variant: str) -> list[int]:
    """将预声明对照映射到固定特征列，拒绝临时添加未登记特征。"""
    if variant not in FEATURE_SETS:
        raise evaluation.EvaluationError("未知准入特征对照")
    return [FEATURE_NAMES.index(name) for name in FEATURE_SETS[variant]]


def _validateFeatureSchema(featureSchema: tuple[str, ...]) -> None:
    """显式 schema 固定列顺序；空名、重名和空 schema 都无法可靠复用模型。"""
    if (not isinstance(featureSchema, tuple) or not featureSchema
            or any(not isinstance(name, str) or not name.strip() for name in featureSchema)
            or len(set(featureSchema)) != len(featureSchema)):
        raise evaluation.EvaluationError("准入特征 schema 必须为非空且名称唯一的 tuple")


def _validateFeatureWidths(features: dict, caseIDs, width: int) -> None:
    """只检查指定阶段的输入宽度，不读取其他组的特征或标签。"""
    for caseID in caseIDs:
        rows = features.get(caseID)
        if not isinstance(rows, dict):
            raise evaluation.EvaluationError("准入特征缺少场景映射")
        if any(not isinstance(row, (list, tuple)) or len(row) != width for row in rows.values()):
            raise evaluation.EvaluationError("准入特征行宽度与 schema 不一致")


def fitAdmissionModel(cases: list[dict], features: dict, variant: str, *, featureSchema: tuple[str, ...] | None = None) -> dict:
    """只在传入拟合组上标准化并拟合固定 L2 logistic，单类时安全关闭。

    每组先等权、组内每题等权，再全局平衡正负类。required 与 allowed
    都是可接受正例；未显式标注的候选不用于拟合，阈值校准仍计作不正确。
    显式 schema 使用输入的全部列；省略时沿用旧对照在完整特征中的列号。
    """
    import numpy as np

    if featureSchema is None:
        indices = _featureIndices(variant)
        featureNames = FEATURE_SETS[variant]
    else:
        _validateFeatureSchema(featureSchema)
        _validateFeatureWidths(features, (case["caseID"] for case in cases), len(featureSchema))
        indices = list(range(len(featureSchema)))
        featureNames = featureSchema
    groups = Counter(case["groupID"] for case in cases)
    labeledByCase = {
        case["caseID"]: sorted((case["requiredIDs"] | case["allowedIDs"] | case["forbiddenIDs"]) & features[case["caseID"]].keys())
        for case in cases
    }
    eligibleGroups = Counter(case["groupID"] for case in cases if labeledByCase[case["caseID"]])
    rows, labels, weights = [], [], []
    excludedCount = 0
    for case in sorted(cases, key=lambda item: item["caseID"]):
        positives = case["requiredIDs"] | case["allowedIDs"]
        caseFeatures = features[case["caseID"]]
        labeledIDs = labeledByCase[case["caseID"]]
        excludedCount += len(caseFeatures) - len(labeledIDs)
        for memoryID in labeledIDs:
            rows.append([caseFeatures[memoryID][index] for index in indices])
            labels.append(float(memoryID in positives))
            weights.append(1.0 / (eligibleGroups[case["groupID"]] * len(labeledIDs)))
    model = {
        "variant": variant, "featureNames": list(featureNames), "featureIndices": indices,
        "regularization": REGULARIZATION, "fitGroupIDs": sorted(groups),
        "fitCaseIDs": sorted(case["caseID"] for case in cases),
        "fitPairCount": len(rows), "positivePairCount": int(sum(labels)),
        "negativePairCount": len(labels) - int(sum(labels)),
        "excludedUnlabeledPairCount": excludedCount,
        "enabled": False, "reason": "insufficient-labeled-classes",
    }
    if featureSchema is not None:
        model["inputFeatureCount"] = len(featureSchema)
    if not rows or len(set(labels)) < 2:
        return model
    matrix = np.asarray(rows, dtype=np.float64)
    target = np.asarray(labels, dtype=np.float64)
    weight = np.asarray(weights, dtype=np.float64)
    if not np.isfinite(matrix).all():
        raise evaluation.EvaluationError("拟合特征必须有限")
    # 统计量不能读取校准/测试折；类平衡只重加权已有拟合行，不复制样本。
    weight /= weight.sum()
    mean = np.sum(matrix * weight[:, None], axis=0)
    scale = np.sqrt(np.sum((matrix - mean) ** 2 * weight[:, None], axis=0))
    scale[scale < 1e-12] = 1.0
    for label in (0.0, 1.0):
        mask = target == label
        weight[mask] *= 0.5 / weight[mask].sum()
    design = np.column_stack((np.ones(len(matrix)), (matrix - mean) / scale))
    coefficients = np.zeros(design.shape[1])
    penalty = np.full(design.shape[1], REGULARIZATION)
    penalty[0] = 0.0

    def objective(values):
        """logaddexp 避免极端 logit 溢出；截距不接受 L2 惩罚。"""
        logits = design @ values
        return float(np.sum(weight * (np.logaddexp(0.0, logits) - target * logits)) + 0.5 * np.sum(penalty * values ** 2))

    converged = False
    for iteration in range(MAX_ITERATIONS):
        probabilities = 1.0 / (1.0 + np.exp(-np.clip(design @ coefficients, -60.0, 60.0)))
        gradient = design.T @ (weight * (probabilities - target)) + penalty * coefficients
        if np.max(np.abs(gradient)) < CONVERGENCE_TOLERANCE:
            converged = True
            break
        curvature = weight * probabilities * (1.0 - probabilities)
        hessian = design.T @ (curvature[:, None] * design) + np.diag(penalty)
        step = np.linalg.solve(hessian, gradient)
        oldObjective = objective(coefficients)
        multiplier = 1.0
        for _ in range(30):
            proposal = coefficients - multiplier * step
            if objective(proposal) <= oldObjective:
                coefficients = proposal
                break
            multiplier *= 0.5
        else:
            raise evaluation.EvaluationError("准入拟合线搜索未收敛")
    if not converged:
        raise evaluation.EvaluationError("准入拟合达到迭代上限")
    model.update({
        "enabled": True, "reason": None, "mean": mean.tolist(), "scale": scale.tolist(),
        "intercept": float(coefficients[0]), "coefficients": coefficients[1:].tolist(),
        "iterations": iteration, "objective": objective(coefficients),
        "weighting": "equal-group/equal-case among cases with labeled pairs, then global balanced classes",
    })
    return model


def predictAdmission(model: dict, features: dict) -> dict:
    """输出联合准入分数；类平衡后的 logistic 值不承诺是真实概率。"""
    import numpy as np

    # 已拟合模型自己携带列映射；旧报告缺少该字段时仍可按既有变体回放。
    indices = model["featureIndices"] if "featureIndices" in model else _featureIndices(model["variant"])
    if "inputFeatureCount" in model:
        _validateFeatureWidths(features, features.keys(), model["inputFeatureCount"])
    predictions = {}
    for caseID, rows in features.items():
        predictions[caseID] = {}
        if not model["enabled"] or not rows:
            continue
        memoryIDs = sorted(rows)
        matrix = np.asarray([[rows[item][index] for index in indices] for item in memoryIDs])
        if not np.isfinite(matrix).all():
            raise evaluation.EvaluationError("预测特征必须有限")
        logits = ((matrix - model["mean"]) / model["scale"]) @ np.asarray(model["coefficients"]) + model["intercept"]
        probabilities = 1.0 / (1.0 + np.exp(-np.clip(logits, -60.0, 60.0)))
        predictions[caseID] = dict(zip(memoryIDs, map(float, probabilities)))
    return predictions


def chooseAdmissionThreshold(cases: list[dict], probabilities: dict) -> tuple:
    """仅用校准组选零 forbidden、precision≥0.95 的最低候选级阈值。"""
    observations = []
    for case in cases:
        positives = case["requiredIDs"] | case["allowedIDs"]
        observations.extend({
            "score": score, "positive": memoryID in positives,
            "forbidden": memoryID in case["forbiddenIDs"],
        } for memoryID, score in probabilities[case["caseID"]].items())
    # 未见可接受正例或任何禁入负例时，无法校准两侧边界，不能把缺少
    # 禁入观察误解成已证明零 forbidden；这比旧阈值选择器更保守。
    if not any(item["positive"] for item in observations) or not any(item["forbidden"] for item in observations):
        return None, {"enabled": False, "reason": "insufficient-calibration-classes", "observedCount": len(observations)}
    return evaluation._chooseThreshold(observations)


def gateRecords(records: dict, probabilities: dict, threshold: float | None) -> dict:
    """联合模型统一准入，保留原通道排序，词面不能绕过联合判断。

    语义通道用二值准入适配已有回放接口，排序仍使用原 enhanced；词面
    保留 BM25 排序。不能将一份联合分数凭空变成两份语义 RRF 证据。
    """
    gated = {}
    for caseID, predictions in probabilities.items():
        record = records[caseID]
        admitted = {memoryID for memoryID, score in predictions.items() if threshold is not None and score >= threshold}
        scores = {}
        ranking = {}
        for channel in evaluation.CHANNEL_NAMES:
            scores[channel] = {
                memoryID: value if channel == "lexical" else 1.0
                for memoryID, value in record["scores"][channel].items()
                if memoryID in admitted and math.isfinite(value) and (channel != "lexical" or value > 0)
            }
            if channel != "lexical":
                ranking[channel] = {
                    memoryID: record["ranking"][channel].get(memoryID, record["scores"][channel][memoryID])
                    for memoryID in scores[channel]
                }
        gated[caseID] = {"sameQuery": record["sameQuery"], "scores": scores, "ranking": ranking}
    return gated


def replayAdmission(cases: list[dict], records: dict, probabilities: dict, threshold: float | None) -> list[dict]:
    """保留相同查询去重、pinned、scope、正文去重与渲染预算后再计分。"""
    thresholds = {"semanticCurrent": 0.5, "semanticAssisted": 0.5, "lexical": 0.0}
    return study.replayCases(cases, gateRecords(records, probabilities, threshold), thresholds, None)


def splitFitCalibration(cases: list[dict]) -> tuple[list[dict], list[dict]]:
    """按独立盐值散列分出约四分之一训练主题校准，其余拟合且不 refit。"""
    groups = sorted({case["groupID"] for case in cases}, key=lambda value: hashlib.sha256(("admission-calibration-v1:" + value).encode()).hexdigest())
    if len(groups) < 2:
        raise evaluation.EvaluationError("准入拟合和阈值校准至少需要两个主题组")
    calibrationGroups = set(groups[::INNER_CALIBRATION_STRIDE])
    return (
        [case for case in cases if case["groupID"] not in calibrationGroups],
        [case for case in cases if case["groupID"] in calibrationGroups],
    )


def evaluateAdmissionFeatures(cases: list[dict], records: dict, featureVariants: dict, *, memoryCheck=None) -> dict:
    """独立特征矩阵共享同一候选池及嵌套分组，复用原拟合、校准和回放。

    featureVariants 每项含 names tuple 与 features 场景/候选映射。调用者
    先限定共同候选池；本函数不读取文本或生成特征，也不更改全局特征集合。
    """
    if any(case.get("split") != evaluation.CALIBRATION_SPLIT for case in cases):
        raise evaluation.EvaluationError("准入研究只接受 calibration")
    if not isinstance(featureVariants, dict) or not featureVariants:
        raise evaluation.EvaluationError("准入研究至少需要一组预声明特征")
    caseIDs = {case["caseID"] for case in cases}
    if not caseIDs <= records.keys():
        raise evaluation.EvaluationError("准入研究缺少场景通道记录")
    selected = {
        caseID: set().union(*(values.keys() for values in records[caseID]["scores"].values()))
        for caseID in caseIDs
    }
    for variant, specification in featureVariants.items():
        if (not isinstance(variant, str) or not variant.strip() or not isinstance(specification, dict)
                or "names" not in specification or "features" not in specification):
            raise evaluation.EvaluationError("准入特征对照需要名称、schema 和矩阵")
        _validateFeatureSchema(specification["names"])
        features = specification["features"]
        if not isinstance(features, dict) or set(features) != caseIDs:
            raise evaluation.EvaluationError("准入特征对照必须覆盖相同场景")
        _validateFeatureWidths(features, caseIDs, len(specification["names"]))
        if any(set(features[caseID]) != selected[caseID] for caseID in caseIDs):
            raise evaluation.EvaluationError("准入特征对照必须使用相同候选池")
    if memoryCheck:
        memoryCheck()
    baseline = study.studyScores(cases, records, None)
    foldCases = []
    matchedResults = []
    matchedFolds = []
    for fold in baseline["folds"]:
        if memoryCheck:
            memoryCheck()
        testGroups = set(fold["testGroupIDs"])
        train = [case for case in cases if case["groupID"] not in testGroups]
        test = [case for case in cases if case["groupID"] in testGroups]
        fit, calibrate = splitFitCalibration(train)
        foldCases.append((fit, calibrate, test))
        thresholds = study.chooseThresholds(calibrate, records, None)
        matchedResults.extend(study.replayCases(test, records, thresholds, None))
        matchedFolds.append({"fold": fold["fold"], "calibrationGroupIDs": sorted({case["groupID"] for case in calibrate}), "thresholds": thresholds})
    trials = []
    for variant, specification in featureVariants.items():
        features = specification["features"]
        featureSchema = specification["names"]
        if memoryCheck:
            memoryCheck()
        started = time.perf_counter()
        model = fitAdmissionModel(cases, features, variant, featureSchema=featureSchema)
        probabilities = predictAdmission(model, features)
        threshold, calibration = chooseAdmissionThreshold(cases, probabilities)
        inSample = replayAdmission(cases, records, probabilities, threshold)
        validationResults, folds = [], []
        for foldNumber, (fit, calibrate, test) in enumerate(foldCases):
            if memoryCheck:
                memoryCheck()
            foldModel = fitAdmissionModel(fit, features, variant, featureSchema=featureSchema)
            foldPredictions = predictAdmission(foldModel, {case["caseID"]: features[case["caseID"]] for case in [*calibrate, *test]})
            foldThreshold, foldCalibration = chooseAdmissionThreshold(calibrate, foldPredictions)
            validationResults.extend(replayAdmission(test, records, foldPredictions, foldThreshold))
            folds.append({
                "fold": foldNumber, "fitGroupIDs": sorted({case["groupID"] for case in fit}),
                "calibrationGroupIDs": sorted({case["groupID"] for case in calibrate}),
                "testGroupIDs": sorted({case["groupID"] for case in test}),
                "fitCaseCount": len(fit), "calibrationCaseCount": len(calibrate), "testCaseCount": len(test),
                "calibrationNoAnswerCaseCount": sum(not case["requiredIDs"] and not case["allowedIDs"] for case in calibrate),
                "model": foldModel, "threshold": foldThreshold, "calibration": foldCalibration,
            })
        trials.append({
            "variant": variant, "model": model, "threshold": threshold, "calibration": calibration,
            "inSample": study.summarizeResults(inSample), "groupValidation": study.summarizeResults(validationResults),
            "folds": folds, "cases": inSample, "validationCases": validationResults,
            "seconds": time.perf_counter() - started,
        })
        print(f"完成准入对照 {variant}", flush=True)
    return {
        "baseline": baseline,
        "calibrationMatchedBaseline": {
            "groupValidation": study.summarizeResults(matchedResults),
            "folds": matchedFolds, "validationCases": matchedResults,
        },
        "trials": trials,
    }


def studyAdmission(cases: list[dict], records: dict, *, memoryCheck=None) -> dict:
    """固定三组特征与 union-32，在相同外折同时记录两种基线和联合准入。"""
    if any(case.get("split") != evaluation.CALIBRATION_SPLIT for case in cases):
        raise evaluation.EvaluationError("准入研究只接受 calibration")
    selected = {case["caseID"]: study.selectCandidateIDs(records[case["caseID"]], "union", CANDIDATE_LIMIT) for case in cases}
    features = {case["caseID"]: buildCandidateFeatures(case, records[case["caseID"]], selected[case["caseID"]]) for case in cases}
    # 原始通道基线使用同一候选池；不复用 dense-gate helper，因为它会关闭
    # lexical，而此处要对照原本三个独立阈值与一个联合准入的区别。
    poolRecords = {
        caseID: {
            "sameQuery": record["sameQuery"],
            "scores": {name: {item: value for item, value in values.items() if item in selected[caseID]} for name, values in record["scores"].items()},
            "ranking": {name: {item: value for item, value in values.items() if item in selected[caseID]} for name, values in record["ranking"].items()},
        } for caseID, record in records.items() if caseID in selected
    }
    # 老对照仍按原列集合取值；每项显式携带矩阵，避免全局修改列定义。
    featureVariants = {
        variant: {
            "names": names,
            "features": {
                caseID: {memoryID: [row[index] for index in _featureIndices(variant)] for memoryID, row in rows.items()}
                for caseID, rows in features.items()
            },
        } for variant, names in FEATURE_SETS.items()
    }
    evaluated = evaluateAdmissionFeatures(cases, poolRecords, featureVariants, memoryCheck=memoryCheck)
    # 旧报告公开完整 24 列 candidateFeatures；模型列映射仍指向该矩阵，
    # 不能让内部投影后的宽度限制破坏既有报告的 predictAdmission 回放。
    for trial in evaluated["trials"]:
        for model in [trial["model"], *(fold["model"] for fold in trial["folds"])]:
            model["featureIndices"] = _featureIndices(trial["variant"])
            model.pop("inputFeatureCount", None)
    baseline = evaluated["baseline"]
    coverage = study.candidateCoverageMetrics(cases, selected)
    coverage["cohorts"] = {
        name: study.candidateCoverageMetrics([case for case in cases if ("expandedCalibration" in case.get("subsets", [])) == expanded], selected)
        for name, expanded in (("oldCalibration", False), ("expandedCalibration", True))
    }
    return {
        "schemaVersion": 1, "reportType": "memory-admission-study", "status": "experimental",
        "productionEligible": False, "split": "calibration", "featureVersion": FEATURE_VERSION,
        "calibrationSha256": evaluation._casesDigest(cases), "caseCount": len(cases),
        "reviewStatusCounts": dict(Counter(case.get("metadata", {}).get("reviewStatus", "unspecified") for case in cases)),
        "candidateStrategy": "union-per-channel", "candidateLimitPerChannel": CANDIDATE_LIMIT,
        "candidateCoverage": coverage, "baseline": baseline,
        "baselineNote": "同一 union 池内三个原通道独立准入，lexical 可独立通过；区别于此前候选网格的 dense-only gate。matched 基线仅用与联合模型相同的校准组选择阈值。",
        "featureNames": list(FEATURE_NAMES), "candidateFeatures": features,
        "calibrationMatchedBaseline": evaluated["calibrationMatchedBaseline"],
        "trials": evaluated["trials"],
        "validationNote": baseline["validationNote"] + " 外折内部再按主题分开拟合和阈值校准，校准后不 refit；样本内模型使用全体拟合与校准，仅为乐观诊断。三个对照都报告，择优不是独立证据。",
        "featureNote": "无 ID、主题、标签、hint 特征；相对位置只使用本题候选分布。词项与数字重合不是人物、时间、属性或矛盾语义识别；allowed 作为正例。联合分数不能解释为真实概率。",
        "rankingNote": "联合阈值决定所有通道资格，原 enhanced/BM25 排序与同查询去重保持；不把联合分数复制成新增通道。最终指标经过 scope、正文去重及渲染预算。",
    }




class AdmissionMemoryError(evaluation.EvaluationError):
    """在资源中止报告中保留发现超限时的证据。"""

    def __init__(self, rss: int, peak: int | None):
        """当前工作集与历史峰值是不同统计量，分别记录。"""
        self.rss = rss
        self.peak = peak
        super().__init__("准入实验进程内存超过 512 MiB，停止实验")


def checkMemoryBudget(process, samples: list) -> None:
    """检查当前 RSS/可用历史峰值；这是采样检查，不是操作系统硬限额。"""
    info = process.memory_info()
    rss, peak = info.rss, getattr(info, "peak_wset", None)
    if not isinstance(rss, (int, float)) or not math.isfinite(rss) or rss < 0:
        raise evaluation.EvaluationError("无法获得有效的实验 RSS")
    if peak is not None and (not isinstance(peak, (int, float)) or not math.isfinite(peak) or peak < 0):
        raise evaluation.EvaluationError("无法获得有效的实验峰值")
    samples.append(rss)
    if max(rss, peak if peak is not None else rss) > MEMORY_LIMIT_BYTES:
        raise AdmissionMemoryError(rss, peak)


class BudgetScorer:
    """在每次实际编码前后采样，复用现有 scorer 缓存与关闭流程。"""

    def __init__(self, scorer, check):
        """包装器不持有模型所有权，外层 finally 负责释放。"""
        self.scorer = scorer
        self.check = check

    def score(self, texts: list[str], candidates: list[dict]) -> list[dict]:
        """编码超限后立即停止，不进入后续拟合。"""
        self.check()
        result = self.scorer.score(texts, candidates)
        self.check()
        return result


def validateOutputPath(output: str, cases: str, manifest: str, modelDir: str) -> None:
    """在读取输入前保护最终及原始文件名派生的临时路径。"""
    rawPath = Path(output)
    writes = {rawPath.resolve(), rawPath.with_suffix(rawPath.suffix + ".tmp").resolve()}
    protected = {Path(item).resolve() for item in (cases, manifest, LLM_MEMORY_CALIBRATION_PATH, LLM_MEMORY_MODEL_MANIFEST_PATH)}
    modelRoot = Path(modelDir).resolve()
    if len(writes) < 2 or writes & protected or any(path == modelRoot or modelRoot in path.parents for path in writes):
        raise evaluation.EvaluationError("实验报告及临时文件不能覆盖输入、正式配置或模型产物")
    if rawPath.suffix != ".json":
        raise evaluation.EvaluationError("实验报告必须使用 .json 路径")


def main() -> int:
    """使用本地 BGE-small 做一次编码，三个固定轻量对照共享分数。"""
    parser = argparse.ArgumentParser(description="Memory calibration-only 轻量联合准入研究")
    parser.add_argument("--cases", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--model-dir", default=LLM_MEMORY_MODEL_DIR)
    parser.add_argument("--manifest", default=LLM_MEMORY_MODEL_MANIFEST_PATH)
    args = parser.parse_args()
    encoder = scorer = None
    samples = []
    phase = "inputs"
    try:
        validateOutputPath(args.output, args.cases, args.manifest, args.model_dir)
        cases = study.loadCalibrationCases(args.cases)
        inputs = {
            "calibrationSha256": evaluation._casesDigest(cases),
            "modelManifestSha256": hashlib.sha256(Path(args.manifest).read_bytes()).hexdigest(),
            "studyScriptSha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        }
        process = evaluation.psutil.Process()

        def check():
            """在编码及每次拟合边界检查同一个进程预算。"""
            checkMemoryBudget(process, samples)

        phase = "dense-load"
        check()
        encoder = study.StudyEncoder(modelDir=args.model_dir, manifestPath=args.manifest)
        check()
        scorer = evaluation.EncoderSemanticScorer(encoder)
        phase = "dense-scoring"
        records = study.collectScores(cases, BudgetScorer(scorer, check))
        scorer.clear()
        encoder.close()
        phase = "feature-study"
        report = studyAdmission(cases, records, memoryCheck=check)
        check()
        report.update(inputs)
        report["rssBytes"] = {"observedMaximum": max(samples), "processPeakWorkingSet": getattr(process.memory_info(), "peak_wset", None), "limit": MEMORY_LIMIT_BYTES}
        evaluation._writeReport(report, args.output)
        return 0
    except AdmissionMemoryError as exc:
        failure = {
            "schemaVersion": 1, "reportType": "memory-admission-study", "status": "aborted",
            "reason": "memory-budget-exceeded", "productionEligible": False, "split": "calibration",
            **inputs, "phase": phase, "trials": [],
            "rssBytes": {"atAbort": exc.rss, "observedMaximum": max(samples), "processPeakWorkingSet": exc.peak, "limit": MEMORY_LIMIT_BYTES},
            "note": "phase 是发现超限的阶段，历史峰值可能来自更早阶段；没有完整质量结果。",
        }
        try:
            evaluation._writeReport(failure, args.output)
        except OSError as reportError:
            print(f"中止报告写入失败: {reportError}", file=sys.stderr)
        print(f"准入实验失败: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, evaluation.psutil.Error) as exc:
        print(f"准入实验失败: {exc}", file=sys.stderr)
        return 1
    finally:
        if scorer is not None:
            scorer.clear()
        if encoder is not None:
            encoder.close()


if __name__ == "__main__":
    raise SystemExit(main())
