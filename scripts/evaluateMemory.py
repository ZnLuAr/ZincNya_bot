#!/usr/bin/env python3
"""
scripts/evaluateMemory.py

回答一个问题：hybrid 检索的分数阈值该定多少、定完效果好不好。

工作对象是人工标注的脱敏测试数据（JSON fixture，每条场景写明
「这些记忆里哪些必须召回 / 哪些无所谓 / 哪些禁止召回」），全程
不碰生产数据库、不调用生成模型。四个子命令分工：

- calibrate：拿标注数据里的一半（calibration split）试出各通道阈值，
  产出「候选」文件——必须有人审查批准后才能换成正式的；
- evaluate：拿另一半从没参与调阈值的数据（holdout split）验收，
  只认已批准的正式阈值文件；
- encoder / benchmark：测模型加载耗时、内存占用、查询延迟。

评测直接 import 线上检索用的那几个函数来打分，不在脚本里另写一套
评分逻辑——测出来的数字就是将来线上跑出来的行为。
"""

import argparse
from dataclasses import fields, is_dataclass
import hashlib
import inspect
import json
import math
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import psutil

from config import (
    LLM_MEMORY_CALIBRATION_PATH,
    LLM_MEMORY_CONTEXT_MAX_CHARS,
    LLM_MEMORY_MODEL_DIR,
    LLM_MEMORY_MODEL_MANIFEST_PATH,
    LLM_MEMORY_PINNED_MAX_CHARS,
    LLM_MEMORY_REPORT_DIR,
    LLM_MEMORY_RETRIEVE_PER_SCOPE,
    LLM_MEMORY_RETRIEVE_TOTAL,
)
from utils.llm.memory.database import (
    MEMORY_MODE_CONTEXTUAL,
    MEMORY_MODE_PINNED,
    selectLegacyMemoryCandidates,
)
from utils.llm.memory.encoder import MemoryEncoder, loadModelManifest
from utils.llm.memory.lexical import scoreLexicalCandidates
from utils.llm.memory.retrieval import (
    LEXICAL_VERSION,
    buildQueryTexts,
    renderMemoryContext,
    selectContextualCandidates,
    sortPinnedMemories,
)
from utils.llm.memory.types import MemoryQuery, MemoryTurn


# 三通道名与 retrieval.selectContextualCandidates 的通道一一对应
CHANNEL_NAMES = (
    "semanticCurrent",
    "semanticAssisted",
    "lexical",
)
# legacy = 线上旧行为基线；lexical = 仅词面通道；
# hybrid / hybrid+hint = 语义通道不带 / 带 retrievalHint 编码（对照实验）
EVALUATION_MODES = ("legacy", "lexical", "hybrid", "hybrid+hint")
# fixture 按 groupID 固定划入两 split：calibration 调阈值，holdout 验收；
# 同一事实的近似场景只进一个 split，防止校准集向保留集泄漏
CALIBRATION_SPLIT = "calibration"
HOLDOUT_SPLIT = "holdout"
CALIBRATION_PRECISION_TARGET = 0.95
REQUIRED_CASE_FIELDS = (
    "caseID",
    "groupID",
    "split",
    "memories",
    "query",
    "requiredIDs",
    "allowedIDs",
    "forbiddenIDs",
    "allowAbstain",
)


class EvaluationError(ValueError):
    """评测 fixture 或校准文件不满足契约。"""


def _writeReport(report: dict, outputPath: str | Path | None) -> None:
    """输出 JSON 报告；写文件时先落临时文件再原子替换。"""
    serialized = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    if outputPath is None:
        print(serialized)
        return

    path = Path(outputPath)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporaryPath = path.with_suffix(path.suffix + ".tmp")
    temporaryPath.write_text(serialized + "\n", encoding="utf-8")
    temporaryPath.replace(path)
    print(f"评测报告已写入: {path}")


def _canonicalDigest(value) -> str:
    """对可 JSON 化结构计算键顺序稳定的 SHA-256。"""
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _jsonSafe(value):
    """递归转换 dataclass、时间与集合，使规范化 fixture 可稳定序列化。"""
    if is_dataclass(value):
        return {
            field.name: _jsonSafe(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, set):
        return sorted(_jsonSafe(item) for item in value)
    if isinstance(value, tuple):
        return [_jsonSafe(item) for item in value]
    if isinstance(value, list):
        return [_jsonSafe(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _jsonSafe(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    return value


def _casesDigest(cases: list[dict]) -> str:
    """计算规范化评测集散列，供 calibration 绑定数据版本。"""
    return _canonicalDigest(_jsonSafe(cases))


def _parseTimestamp(value):
    """解析 fixture 时间；缺失或非法值使用稳定的最小时间。"""
    if isinstance(value, datetime):
        return value
    if not value:
        return datetime.min
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return datetime.min


def _normalizeIDSet(value, fieldName: str, caseID: str) -> set[int]:
    """把 fixture 的 ID 数组规范化为正整数集合。"""
    if value is None:
        return set()
    if not isinstance(value, list):
        raise EvaluationError(f"{caseID}.{fieldName} 必须是数组")
    result = set()
    for rawID in value:
        if isinstance(rawID, bool):
            raise EvaluationError(f"{caseID}.{fieldName} 含无效 ID")
        try:
            memoryID = int(rawID)
        except (TypeError, ValueError) as exc:
            raise EvaluationError(f"{caseID}.{fieldName} 含无效 ID") from exc
        if memoryID <= 0:
            raise EvaluationError(f"{caseID}.{fieldName} 含无效 ID")
        result.add(memoryID)
    return result


def _normalizeMemory(rawMemory, caseID: str) -> dict:
    """校验并规范化单条脱敏 memory fixture。"""
    if not isinstance(rawMemory, dict):
        raise EvaluationError(f"{caseID}.memories 必须只包含对象")
    memory = dict(rawMemory)
    try:
        memoryID = int(memory.get("id"))
    except (TypeError, ValueError) as exc:
        raise EvaluationError(f"{caseID} memory id 无效") from exc
    if memoryID <= 0:
        raise EvaluationError(f"{caseID} memory id 无效")

    scopeType = memory.get("scope_type", memory.get("scopeType", "global"))
    scopeID = memory.get("scope_id", memory.get("scopeID", "global"))
    scopeType = str(scopeType).strip().lower()
    if scopeType == "global":
        scopeID = "global"
    elif scopeType not in {"chat", "user", "session"}:
        raise EvaluationError(f"{caseID} memory {memoryID} scope 无效")
    elif scopeID is None or str(scopeID).strip() == "":
        raise EvaluationError(f"{caseID} memory {memoryID} scope_id 为空")

    content = str(memory.get("content", "")).strip()
    if not content:
        raise EvaluationError(f"{caseID} memory {memoryID} content 为空")

    rawPriority = memory.get("priority", 0)
    if isinstance(rawPriority, bool):
        raise EvaluationError(f"{caseID} memory {memoryID} priority 无效")
    try:
        priority = int(rawPriority)
    except (TypeError, ValueError) as exc:
        raise EvaluationError(f"{caseID} memory {memoryID} priority 无效") from exc
    if priority < 0:
        raise EvaluationError(f"{caseID} memory {memoryID} priority 无效")

    mode = str(memory.get("mode", MEMORY_MODE_CONTEXTUAL)).strip().lower()
    if mode not in {MEMORY_MODE_CONTEXTUAL, MEMORY_MODE_PINNED}:
        raise EvaluationError(f"{caseID} memory {memoryID} mode 无效")

    tags = memory.get("tags", [])
    if tags is None:
        tags = []
    if not isinstance(tags, list):
        raise EvaluationError(f"{caseID} memory {memoryID} tags 无效")

    enabled = memory.get("enabled", True)
    if not isinstance(enabled, bool):
        raise EvaluationError(f"{caseID} memory {memoryID} enabled 无效")

    return {
        "id": memoryID,
        "scope_type": scopeType,
        "scope_id": str(scopeID),
        "content": content,
        "tags": [str(tag).strip() for tag in tags if str(tag).strip()],
        "enabled": enabled,
        "priority": priority,
        "source": str(memory.get("source", "inferred")),
        "mode": mode,
        "retrievalHint": memory.get(
            "retrievalHint",
            memory.get("retrieval_hint"),
        ),
        "created_at": _parseTimestamp(
            memory.get("created_at", memory.get("createdAt")),
        ),
        "updated_at": _parseTimestamp(
            memory.get("updated_at", memory.get("updatedAt")),
        ),
    }


def _normalizeScope(rawScope, caseID: str) -> dict:
    """兼容 fixture 的 camelCase/snake_case scope 字段。"""
    if rawScope is None:
        rawScope = {}
    if not isinstance(rawScope, dict):
        raise EvaluationError(f"{caseID}.scope 必须是对象")

    def _value(*names):
        for name in names:
            if name in rawScope:
                return rawScope[name]
        return None

    return {
        "chatID": _value("chatID", "chat_id", "chat"),
        "userID": _value("userID", "user_id", "user"),
        "sessionID": _value("sessionID", "session_id", "session"),
    }


def _normalizeQuery(rawQuery, caseID: str) -> MemoryQuery:
    """将 fixture 查询转换为线上检索使用的不可变 ``MemoryQuery``。"""
    if isinstance(rawQuery, MemoryQuery):
        return rawQuery
    if not isinstance(rawQuery, dict):
        raise EvaluationError(f"{caseID}.query 必须是对象")

    rawTurns = rawQuery.get("turns")
    if rawTurns is None:
        rawTurns = [{
            "currentText": rawQuery.get(
                "currentText",
                rawQuery.get("current", ""),
            ),
            "replyText": rawQuery.get(
                "replyText",
                rawQuery.get("reply", ""),
            ),
            "currentSender": rawQuery.get("currentSender", ""),
            "replySender": rawQuery.get("replySender", ""),
        }]
    if not isinstance(rawTurns, list):
        raise EvaluationError(f"{caseID}.query.turns 必须是数组")

    turns = []
    for rawTurn in rawTurns:
        if not isinstance(rawTurn, dict):
            raise EvaluationError(f"{caseID}.query.turns 含无效对象")
        turns.append(MemoryTurn(
            currentText=str(rawTurn.get("currentText", rawTurn.get("current", ""))),
            replyText=str(rawTurn.get("replyText", rawTurn.get("reply", ""))),
            currentSender=str(rawTurn.get("currentSender", "")),
            replySender=str(rawTurn.get("replySender", "")),
        ))

    rawHistory = rawQuery.get("history", [])
    if rawHistory is None:
        rawHistory = []
    if not isinstance(rawHistory, list) or any(
        not isinstance(message, dict) for message in rawHistory
    ):
        raise EvaluationError(f"{caseID}.query.history 必须是对象数组")

    return MemoryQuery(
        turns=tuple(turns),
        history=tuple(dict(message) for message in rawHistory),
        feedbackText=str(rawQuery.get("feedbackText", "")),
    )


def validateEvaluationCases(rawData) -> list[dict]:
    """校验并规范化脱敏评测 fixture。

    同一 ``groupID`` 只能出现在一个 split 中，避免同一事实的近似场景
    同时进入校准集和保留集。这里不强制 50/30 数量，以便先用小型 CI
    fixture 验证评测逻辑；正式验收由报告中的数量门槛决定。
    """
    if isinstance(rawData, list):
        rawCases = rawData
    elif isinstance(rawData, dict):
        if rawData.get("schemaVersion", 1) != 1:
            raise EvaluationError("fixture schemaVersion 不受支持")
        rawCases = rawData.get("cases")
    else:
        rawCases = None
    if not isinstance(rawCases, list):
        raise EvaluationError("fixture 根对象必须包含 cases 数组")

    cases = []
    seenCaseIDs = set()
    groupSplits = {}
    for rawCase in rawCases:
        if not isinstance(rawCase, dict):
            raise EvaluationError("fixture case 必须是对象")
        missingFields = [
            fieldName for fieldName in REQUIRED_CASE_FIELDS
            if fieldName not in rawCase
        ]
        if missingFields:
            raise EvaluationError(
                f"fixture case 缺少字段: {', '.join(missingFields)}"
            )

        caseID = str(rawCase["caseID"]).strip()
        groupID = str(rawCase["groupID"]).strip()
        split = str(rawCase["split"]).strip().lower()
        if not caseID or not groupID:
            raise EvaluationError("caseID/groupID 不能为空")
        if caseID in seenCaseIDs:
            raise EvaluationError(f"caseID 重复: {caseID}")
        if split not in {CALIBRATION_SPLIT, HOLDOUT_SPLIT}:
            raise EvaluationError(f"{caseID}.split 必须是 calibration 或 holdout")
        if groupID in groupSplits and groupSplits[groupID] != split:
            raise EvaluationError(f"groupID 跨 split: {groupID}")
        seenCaseIDs.add(caseID)
        groupSplits[groupID] = split

        rawMemories = rawCase["memories"]
        if not isinstance(rawMemories, list):
            raise EvaluationError(f"{caseID}.memories 必须是数组")
        memories = [_normalizeMemory(memory, caseID) for memory in rawMemories]
        memoryIDs = [memory["id"] for memory in memories]
        if len(memoryIDs) != len(set(memoryIDs)):
            raise EvaluationError(f"{caseID}.memories 含重复 ID")

        requiredIDs = _normalizeIDSet(rawCase["requiredIDs"], "requiredIDs", caseID)
        allowedIDs = _normalizeIDSet(rawCase["allowedIDs"], "allowedIDs", caseID)
        forbiddenIDs = _normalizeIDSet(
            rawCase["forbiddenIDs"],
            "forbiddenIDs",
            caseID,
        )
        knownIDs = set(memoryIDs)
        for fieldName, ids in (
            ("requiredIDs", requiredIDs),
            ("allowedIDs", allowedIDs),
            ("forbiddenIDs", forbiddenIDs),
        ):
            unknownIDs = ids.difference(knownIDs)
            if unknownIDs:
                raise EvaluationError(
                    f"{caseID}.{fieldName} 引用了不存在的 memory: "
                    f"{sorted(unknownIDs)}"
                )
        if requiredIDs.intersection(forbiddenIDs):
            raise EvaluationError(f"{caseID} requiredIDs 与 forbiddenIDs 冲突")
        if allowedIDs.intersection(forbiddenIDs):
            raise EvaluationError(f"{caseID} allowedIDs 与 forbiddenIDs 冲突")

        rawSubsets = rawCase.get("subsets", [])
        if rawSubsets is None:
            rawSubsets = []
        if not isinstance(rawSubsets, list):
            raise EvaluationError(f"{caseID}.subsets 必须是数组")
        subsets = [
            str(value).strip() for value in rawSubsets
            if str(value).strip()
        ]
        for flag, subsetName in (
            ("zeroLexicalOverlap", "zeroLexicalOverlap"),
            ("noHint", "noHint"),
            ("hasHint", "hasHint"),
        ):
            if rawCase.get(flag) is True and subsetName not in subsets:
                subsets.append(subsetName)
        allowAbstain = rawCase["allowAbstain"]
        if not isinstance(allowAbstain, bool):
            raise EvaluationError(f"{caseID}.allowAbstain 必须是布尔值")

        cases.append({
            "caseID": caseID,
            "groupID": groupID,
            "split": split,
            "scope": _normalizeScope(rawCase.get("scope"), caseID),
            "memories": memories,
            "query": _normalizeQuery(rawCase["query"], caseID),
            "requiredIDs": requiredIDs,
            "allowedIDs": allowedIDs,
            "forbiddenIDs": forbiddenIDs,
            "allowAbstain": allowAbstain,
            "subsets": subsets,
            "metadata": dict(rawCase.get("metadata", {}))
            if isinstance(rawCase.get("metadata", {}), dict) else {},
        })
    return cases


def loadEvaluationCases(
    casesPath: str | Path,
) -> tuple[list[dict], str]:
    """读取 fixture，并返回规范化场景和绑定用数据集散列。"""
    path = Path(casesPath)
    try:
        rawData = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvaluationError(f"无法读取 fixture: {path}") from exc
    cases = validateEvaluationCases(rawData)
    return cases, _casesDigest(cases)


loadCases = loadEvaluationCases


def splitEvaluationCases(cases: list[dict], split: str) -> list[dict]:
    """只返回指定 calibration/holdout split 的规范化场景。"""
    split = str(split).strip().lower()
    if split not in {CALIBRATION_SPLIT, HOLDOUT_SPLIT}:
        raise EvaluationError("split 必须是 calibration 或 holdout")
    return [case for case in cases if case["split"] == split]


def _scopeCandidates(case: dict) -> list[dict]:
    """复现线上 global + 当前 chat/user/session 的 enabled scope 过滤。"""
    scope = case["scope"]
    scopeValues = {
        "chat": None if scope["chatID"] is None else str(scope["chatID"]),
        "user": None if scope["userID"] is None else str(scope["userID"]),
        "session": None if scope["sessionID"] is None else str(scope["sessionID"]),
    }
    result = []
    for memory in case["memories"]:
        if not memory.get("enabled", True):
            continue
        scopeType = memory.get("scope_type")
        if scopeType == "global":
            result.append(memory)
            continue
        if scopeValues.get(scopeType) == str(memory.get("scope_id")):
            result.append(memory)
    return result


def _contextualCandidates(candidates: list[dict]) -> list[dict]:
    """从可见候选中排除独立评估的 pinned memory。"""
    return [
        memory for memory in candidates
        if memory.get("mode", MEMORY_MODE_CONTEXTUAL) == MEMORY_MODE_CONTEXTUAL
    ]


def _memoryWithoutHint(memory: dict) -> dict:
    """复制 memory 并移除 hint，用于 hybrid 与 hybrid+hint 对照。"""
    result = dict(memory)
    result.pop("retrievalHint", None)
    result.pop("retrieval_hint", None)
    return result


def _normalizeScoreMap(rawScores) -> dict[int, float]:
    """把 scorer 输出收敛为正 ID 到有限浮点分数的映射。"""
    if not isinstance(rawScores, dict):
        return {}
    result = {}
    for rawID, rawScore in rawScores.items():
        try:
            memoryID = int(rawID)
            score = float(rawScore)
        except (TypeError, ValueError):
            continue
        if memoryID <= 0 or not math.isfinite(score):
            continue
        result[memoryID] = score
    return result


def _invokeSemanticScorer(
    semanticScorer,
    queryTexts: list[str],
    candidates: list[dict],
    *,
    includeHint: bool,
    channelNames: tuple[str, ...] | None = None,
) -> list[dict[int, float]]:
    """适配评测 scorer 的兼容签名与返回形态，并统一校验 score map。

    接受多种替身（测试 fake / EncoderSemanticScorer / 裸函数），
    让单测能注入 mock 而不依赖真实模型；返回值收敛为
    正 ID -> 有限分数 的列表，坏条目静默丢弃。
    """
    if semanticScorer is None or not queryTexts:
        return [{} for _ in queryTexts]

    scorer = getattr(semanticScorer, "score", semanticScorer)
    try:
        parameters = inspect.signature(scorer).parameters
    except (TypeError, ValueError):
        parameters = {}
    if "includeHint" in parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    ):
        rawResult = scorer(
            queryTexts,
            candidates,
            includeHint=includeHint,
        )
    else:
        rawResult = scorer(queryTexts, candidates)

    if isinstance(rawResult, dict) and any(
        name in rawResult for name in CHANNEL_NAMES
    ):
        names = channelNames or CHANNEL_NAMES
        return [
            _normalizeScoreMap(rawResult.get(name, {}))
            for name in names[:len(queryTexts)]
        ]
    if isinstance(rawResult, (list, tuple)):
        if len(rawResult) == len(queryTexts):
            return [_normalizeScoreMap(value) for value in rawResult]
        if len(rawResult) == 1 and len(queryTexts) > 1:
            normalized = _normalizeScoreMap(rawResult[0])
            return [dict(normalized) for _ in queryTexts]
    if isinstance(rawResult, dict):
        normalized = _normalizeScoreMap(rawResult)
        return [dict(normalized) for _ in queryTexts]
    raise EvaluationError("semantic scorer 必须返回 score map 或 score map 数组")


def scoreCaseChannels(
    case: dict,
    *,
    semanticScorer=None,
    includeHint: bool = True,
) -> dict[str, dict[int, float]]:
    """调用线上使用的 query、BM25 和候选评分原语，返回三通道分数。"""
    candidates = _scopeCandidates(case)
    contextual = _contextualCandidates(candidates)
    currentText, assistedText, lexicalText = buildQueryTexts(case["query"])
    scores = {name: {} for name in CHANNEL_NAMES}

    if lexicalText:
        scores["lexical"] = scoreLexicalCandidates(lexicalText, contextual)

    semanticTexts = []
    semanticNames = []
    duplicateNames = {}
    for name, textValue in (
        ("semanticCurrent", currentText),
        ("semanticAssisted", assistedText),
    ):
        if not textValue:
            continue
        if textValue in semanticTexts:
            duplicateNames[name] = semanticNames[semanticTexts.index(textValue)]
            continue
        semanticTexts.append(textValue)
        semanticNames.append(name)

    if semanticTexts and semanticScorer is not None:
        semanticCandidates = (
            contextual if includeHint else [
                _memoryWithoutHint(memory) for memory in contextual
            ]
        )
        semanticResults = _invokeSemanticScorer(
            semanticScorer,
            semanticTexts,
            semanticCandidates,
            includeHint=includeHint,
            channelNames=tuple(semanticNames),
        )
        for name, result in zip(semanticNames, semanticResults):
            scores[name] = result
        for name, sourceName in duplicateNames.items():
            scores[name] = dict(scores[sourceName])
    return scores


class EncoderSemanticScorer:
    """将 ``MemoryEncoder`` 适配为离线评测用的语义评分器。"""

    def __init__(self, encoder):
        """持有单个 encoder，并按 memory 内容与 hint 模式缓存 chunk 矩阵。"""
        self.encoder = encoder
        self._memoryCache = {}

    def _cacheKey(self, memory: dict, includeHint: bool):
        """构造不会把有 hint/无 hint 编码结果混用的缓存键。"""
        return (
            int(memory["id"]),
            _canonicalDigest({
                "content": memory.get("content", ""),
                "tags": memory.get("tags", []),
                "retrievalHint": memory.get("retrievalHint"),
            }),
            bool(includeHint),
        )

    def score(
        self,
        queryTexts: list[str],
        candidates: list[dict],
        *,
        includeHint: bool = True,
    ) -> list[dict[int, float]]:
        """编码查询，并按 memory 的最大 chunk 相似度返回逐通道分数。"""
        queryVectors = self.encoder.encodeQueries(queryTexts)
        results = [dict() for _ in queryTexts]
        for memory in candidates:
            key = self._cacheKey(memory, includeHint)
            matrix = self._memoryCache.get(key)
            if matrix is None:
                matrix = self.encoder.encodeMemory(memory)
                self._memoryCache[key] = matrix
            for queryIndex in range(len(queryTexts)):
                similarities = matrix @ queryVectors[queryIndex]
                score = float(similarities.max())
                if math.isfinite(score):
                    results[queryIndex][int(memory["id"])] = score
        return results

    def clear(self) -> None:
        """释放离线评测持有的 memory 向量引用。"""
        self._memoryCache.clear()


def _legacyPool(
    candidates: list[dict],
    *,
    perScopeLimit: int,
    totalLimit: int,
) -> list[dict]:
    """复现 legacy 的逐 scope 截断和汇池后总量截断。"""
    byScope = defaultdict(list)
    for memory in _contextualCandidates(candidates):
        byScope[(memory.get("scope_type"), memory.get("scope_id"))].append(memory)
    pool = []
    for scopeMemories in byScope.values():
        pool.extend(selectLegacyMemoryCandidates(
            scopeMemories,
            totalLimit=max(int(perScopeLimit), 0),
        ))
    return selectLegacyMemoryCandidates(pool, totalLimit=totalLimit)


def _evaluateSingleCase(
    case: dict,
    mode: str,
    thresholds: dict[str, float | None],
    *,
    semanticScorer=None,
    channelScores: dict[str, dict[int, float]] | None = None,
    perScopeLimit: int = LLM_MEMORY_RETRIEVE_PER_SCOPE,
    totalLimit: int = LLM_MEMORY_RETRIEVE_TOTAL,
    maxChars: int = LLM_MEMORY_CONTEXT_MAX_CHARS,
    pinnedMaxChars: int = LLM_MEMORY_PINNED_MAX_CHARS,
) -> dict:
    """用指定检索模式执行单个场景，并返回最终渲染后的选择结果。"""
    if mode not in EVALUATION_MODES:
        raise EvaluationError(f"不支持的评测模式: {mode}")
    candidates = _scopeCandidates(case)
    pinned = sortPinnedMemories([
        memory for memory in candidates
        if memory.get("mode") == MEMORY_MODE_PINNED
    ])
    contextual = []
    selectionDiagnostics = {}

    if mode == "legacy":
        contextual = _legacyPool(
            candidates,
            perScopeLimit=perScopeLimit,
            totalLimit=totalLimit,
        )
    else:
        if channelScores is None:
            channelScores = scoreCaseChannels(
                case,
                semanticScorer=semanticScorer,
                includeHint=mode == "hybrid+hint",
            )
        activeThresholds = {
            name: thresholds.get(name)
            if mode != "lexical" or name == "lexical"
            else None
            for name in CHANNEL_NAMES
        }
        contextual, selectionDiagnostics = selectContextualCandidates(
            _contextualCandidates(candidates),
            channelScores,
            activeThresholds,
        )

    selected, contextBlock, budgetDiagnostics = renderMemoryContext(
        pinned,
        contextual,
        maxChars=maxChars,
        pinnedMaxChars=pinnedMaxChars,
    )
    selectedPinnedIDs = [
        int(memory["id"]) for memory in selected
        if memory.get("mode") == MEMORY_MODE_PINNED
    ]
    selectedContextualIDs = [
        int(memory["id"]) for memory in selected
        if memory.get("mode", MEMORY_MODE_CONTEXTUAL) == MEMORY_MODE_CONTEXTUAL
    ]
    return {
        "caseID": case["caseID"],
        "mode": mode,
        "selectedIDs": [int(memory["id"]) for memory in selected],
        "pinnedIDs": selectedPinnedIDs,
        "contextualIDs": selectedContextualIDs,
        "contextChars": len(contextBlock),
        "diagnostics": {
            **selectionDiagnostics,
            **budgetDiagnostics,
            "candidateCount": len(candidates),
            "contextualCandidateCount": len(_contextualCandidates(candidates)),
        },
    }


def _scoreCaseResult(case: dict, result: dict) -> dict:
    """将 contextual 预测与 required/allowed/forbidden 标注逐项比较。

    三档标注语义：required = 必须召回；allowed = 召回不算错，但不能
    顶替 required 计入召回完成；forbidden = 召回即失败。
    """
    predictedIDs = set(result["contextualIDs"])
    requiredIDs = set(case["requiredIDs"])
    allowedIDs = set(case["allowedIDs"])
    forbiddenIDs = set(case["forbiddenIDs"])
    acceptableIDs = requiredIDs.union(allowedIDs)
    hitIDs = predictedIDs.intersection(requiredIDs)
    acceptedIDs = predictedIDs.intersection(acceptableIDs)
    falsePositiveIDs = predictedIDs.difference(acceptableIDs)
    forbiddenHitIDs = predictedIDs.intersection(forbiddenIDs)
    precision = (
        len(acceptedIDs) / len(predictedIDs)
        if predictedIDs else None
    )
    recall = (
        len(hitIDs) / len(requiredIDs)
        if requiredIDs else None
    )
    return {
        **result,
        "requiredIDs": sorted(requiredIDs),
        "allowedIDs": sorted(allowedIDs),
        "forbiddenIDs": sorted(forbiddenIDs),
        "hitRequiredIDs": sorted(hitIDs),
        "missedRequiredIDs": sorted(requiredIDs.difference(predictedIDs)),
        "falsePositiveIDs": sorted(falsePositiveIDs),
        "forbiddenHitIDs": sorted(forbiddenHitIDs),
        "precision": precision,
        "recall": recall,
        "abstained": not predictedIDs,
        "abstainAllowed": case["allowAbstain"],
        "subsets": list(case.get("subsets", [])),
    }


def _aggregateMetrics(caseResults: list[dict]) -> dict:
    """把逐场景结果汇总成总体指标，并给出「能不能上线」的判定。

    统计口径：precision = 全部场景选中的记忆里标注认可的比例（只统计
    contextual，pinned 不参与）；recall = 必须召回的命中比例。某场景
    一条未选计为「弃权」——全部弃权时 precision 记 None 而非 100%
    （零预测不能计为精确）。qualityGate 是启用 hybrid 的硬条件：
    至少 30 个场景、精确率 ≥95%、召回率 ≥80%、禁入条目零命中，
    四项全部满足才通过。
    """
    predictedCount = sum(len(result["contextualIDs"]) for result in caseResults)
    acceptedCount = sum(
        len(set(result["contextualIDs"]).intersection(
            set(result["requiredIDs"]).union(result["allowedIDs"])
        ))
        for result in caseResults
    )
    requiredCount = sum(len(result["requiredIDs"]) for result in caseResults)
    hitCount = sum(len(result["hitRequiredIDs"]) for result in caseResults)
    forbiddenHitCount = sum(len(result["forbiddenHitIDs"]) for result in caseResults)
    abstainedCount = sum(1 for result in caseResults if result["abstained"])
    unexpectedAbstentionCount = sum(
        1 for result in caseResults
        if result["abstained"] and not result["abstainAllowed"]
    )
    precision = acceptedCount / predictedCount if predictedCount else None
    recall = hitCount / requiredCount if requiredCount else None

    return {
        "caseCount": len(caseResults),
        "predictedContextualCount": predictedCount,
        "acceptedPredictionCount": acceptedCount,
        "requiredCount": requiredCount,
        "requiredHitCount": hitCount,
        "forbiddenHitCount": forbiddenHitCount,
        "precision": precision,
        "recall": recall,
        "coverage": (
            (len(caseResults) - abstainedCount) / len(caseResults)
            if caseResults else 0.0
        ),
        "abstentionRate": abstainedCount / len(caseResults) if caseResults else 0.0,
        "unexpectedAbstentionRate": (
            unexpectedAbstentionCount / len(caseResults)
            if caseResults else 0.0
        ),
        "minimumCaseCountMet": len(caseResults) >= 30,
        "qualityGate": {
            "precisionTarget": 0.95,
            "recallTarget": 0.80,
            "minimumCaseCount": 30,
            "minimumCaseCountPassed": len(caseResults) >= 30,
            "precisionPassed": precision is not None and precision >= 0.95,
            "recallPassed": recall is not None and recall >= 0.80,
            "forbiddenPassed": forbiddenHitCount == 0,
            "passed": (
                len(caseResults) >= 30
                and
                precision is not None
                and precision >= 0.95
                and recall is not None
                and recall >= 0.80
                and forbiddenHitCount == 0
            ),
        },
    }


def _subsetMetrics(caseResults: list[dict]) -> dict:
    """按 fixture subsets 分组复用同一套聚合指标。"""
    subsetNames = sorted({
        subset
        for result in caseResults
        for subset in result.get("subsets", [])
    })
    return {
        subsetName: _aggregateMetrics([
            result for result in caseResults
            if subsetName in result.get("subsets", [])
        ])
        for subsetName in subsetNames
    }


def evaluateRetrievalCases(
    cases: list[dict],
    thresholds: dict[str, float | None],
    *,
    semanticScorer=None,
    split: str = HOLDOUT_SPLIT,
    modes: tuple[str, ...] = EVALUATION_MODES,
    perScopeLimit: int = LLM_MEMORY_RETRIEVE_PER_SCOPE,
    totalLimit: int = LLM_MEMORY_RETRIEVE_TOTAL,
    maxChars: int = LLM_MEMORY_CONTEXT_MAX_CHARS,
    pinnedMaxChars: int = LLM_MEMORY_PINNED_MAX_CHARS,
) -> dict:
    """在指定 split 上比较 legacy、词面和两种 hybrid 结果。"""
    selectedCases = splitEvaluationCases(cases, split)
    if not selectedCases:
        raise EvaluationError(f"没有 split={split} 的 fixture 场景")
    for mode in modes:
        if mode not in EVALUATION_MODES:
            raise EvaluationError(f"不支持的评测模式: {mode}")

    channelCache = {}
    reportModes = {}
    for mode in modes:
        caseResults = []
        for case in selectedCases:
            channelScores = None
            if mode in {"lexical", "hybrid", "hybrid+hint"}:
                includeHint = mode == "hybrid+hint"
                cacheKey = (case["caseID"], includeHint)
                if cacheKey not in channelCache:
                    channelCache[cacheKey] = scoreCaseChannels(
                        case,
                        semanticScorer=semanticScorer,
                        includeHint=includeHint,
                    )
                channelScores = channelCache[cacheKey]
            caseResults.append(_scoreCaseResult(
                case,
                _evaluateSingleCase(
                    case,
                    mode,
                    thresholds,
                    semanticScorer=semanticScorer,
                    channelScores=channelScores,
                    perScopeLimit=perScopeLimit,
                    totalLimit=totalLimit,
                    maxChars=maxChars,
                    pinnedMaxChars=pinnedMaxChars,
                ),
            ))
        aggregate = _aggregateMetrics(caseResults)
        aggregate["subsets"] = _subsetMetrics(caseResults)
        reportModes[mode] = {
            "metrics": aggregate,
            "cases": caseResults,
        }

    return {
        "schemaVersion": 1,
        "split": split,
        "caseCount": len(selectedCases),
        "modes": reportModes,
        "note": (
            "precision/recall 只统计 contextual memory；pinned 仍按相同字符预算"
            "渲染，但不作为情境误召回。"
        ),
    }


evaluateCases = evaluateRetrievalCases


def _calibrationObservations(
    cases: list[dict],
    *,
    semanticScorer=None,
    includeHint: bool = True,
) -> dict[str, list[dict]]:
    """把 calibration 场景展开为各通道的候选分数与标注观察值。"""
    observations = {name: [] for name in CHANNEL_NAMES}
    for case in cases:
        scores = scoreCaseChannels(
            case,
            semanticScorer=semanticScorer,
            includeHint=includeHint,
        )
        positiveIDs = set(case["requiredIDs"]).union(case["allowedIDs"])
        forbiddenIDs = set(case["forbiddenIDs"])
        for channelName in CHANNEL_NAMES:
            for memoryID, score in scores[channelName].items():
                observations[channelName].append({
                    "caseID": case["caseID"],
                    "memoryID": memoryID,
                    "score": score,
                    "positive": memoryID in positiveIDs,
                    "forbidden": memoryID in forbiddenIDs,
                })
    return observations


def _chooseThreshold(
    observations: list[dict],
    *,
    precisionTarget: float = CALIBRATION_PRECISION_TARGET,
    maxForbiddenHits: int = 0,
) -> tuple[float | None, dict]:
    """从实测分数里挑一个阈值：「分数 ≥ 阈值就算命中」刚好满足精确率要求。

    做法：把标注数据中出现过的每个分数作为候选阈值，从低到高逐一
    验证，取第一个满足「放行的记忆中 ≥95% 为正确、且禁入条目零放行」
    的——取尽量低的阈值是为提高召回。全部不达标则返回 None（禁用
    该通道），而不是强行提高阈值迁就指标。
    """
    finiteScores = sorted({
        float(observation["score"])
        for observation in observations
        if (
            math.isfinite(float(observation["score"]))
            and float(observation["score"]) >= 0
        )
    })
    best = None
    for threshold in finiteScores:
        qualified = [
            observation for observation in observations
            if observation["score"] >= threshold
        ]
        if not qualified:
            continue
        acceptedCount = sum(1 for observation in qualified if observation["positive"])
        forbiddenHits = sum(1 for observation in qualified if observation["forbidden"])
        precision = acceptedCount / len(qualified)
        if precision >= precisionTarget and forbiddenHits <= maxForbiddenHits:
            best = {
                "threshold": float(threshold),
                "qualifiedCount": len(qualified),
                "acceptedCount": acceptedCount,
                "precision": precision,
                "forbiddenHits": forbiddenHits,
            }
            break

    if best is None:
        return None, {
            "enabled": False,
            "observedCount": len(observations),
            "positiveObservedCount": sum(
                1 for observation in observations if observation["positive"]
            ),
            "candidateThresholdCount": len(finiteScores),
            "reason": (
                "no threshold satisfies precision and forbidden-hit constraints"
            ),
        }
    return best["threshold"], {
        "enabled": True,
        "observedCount": len(observations),
        "positiveObservedCount": sum(
            1 for observation in observations if observation["positive"]
        ),
        "candidateThresholdCount": len(finiteScores),
        **best,
    }


def calibrateRetrievalThresholds(
    cases: list[dict],
    *,
    semanticScorer=None,
    manifestPath: str | Path = LLM_MEMORY_MODEL_MANIFEST_PATH,
    datasetSha256: str | None = None,
    precisionTarget: float = CALIBRATION_PRECISION_TARGET,
    maxForbiddenHits: int = 0,
    includeHint: bool = True,
) -> dict:
    """只用 calibration split 从实际分数边界生成候选 calibration。"""
    calibrationCases = splitEvaluationCases(cases, CALIBRATION_SPLIT)
    if not calibrationCases:
        raise EvaluationError("没有 calibration split 的 fixture 场景")
    if precisionTarget <= 0 or precisionTarget > 1:
        raise EvaluationError("precisionTarget 必须在 (0, 1] 内")
    if maxForbiddenHits < 0:
        raise EvaluationError("maxForbiddenHits 不能为负数")

    try:
        manifest = loadModelManifest(manifestPath)
    except (OSError, ValueError, RuntimeError) as exc:
        raise EvaluationError("无法读取模型 manifest") from exc

    observations = _calibrationObservations(
        calibrationCases,
        semanticScorer=semanticScorer,
        includeHint=includeHint,
    )
    thresholds = {}
    channelReports = {}
    for channelName in CHANNEL_NAMES:
        threshold, channelReport = _chooseThreshold(
            observations[channelName],
            precisionTarget=precisionTarget,
            maxForbiddenHits=maxForbiddenHits,
        )
        thresholds[channelName] = threshold
        channelReports[channelName] = channelReport

    if datasetSha256 is None:
        datasetSha256 = _casesDigest(cases)
    if (
        not isinstance(datasetSha256, str)
        or not datasetSha256
        or not all(character in "0123456789abcdef" for character in datasetSha256)
        or len(datasetSha256) != 64
    ):
        raise EvaluationError("datasetSha256 必须是 64 位小写 SHA-256")
    return {
        "schemaVersion": 1,
        "status": "candidate",
        "modelRevision": manifest["revision"],
        "encodingVersion": manifest["encodingVersion"],
        "lexicalVersion": LEXICAL_VERSION,
        "datasetSha256": datasetSha256,
        "calibrationSplit": CALIBRATION_SPLIT,
        "thresholds": thresholds,
        "channels": channelReports,
        "calibrationCaseCount": len(calibrationCases),
        "minimumCalibrationCaseCountMet": len(calibrationCases) >= 50,
        "precisionTarget": precisionTarget,
        "maxForbiddenHits": maxForbiddenHits,
        "includeHint": bool(includeHint),
        "reviewRequired": True,
        "note": "这是待人工审查的候选配置，不会自动替换正式 calibration 文件。",
    }


calibrateThresholds = calibrateRetrievalThresholds


def loadApprovedCalibration(
    calibrationPath: str | Path,
    *,
    manifestPath: str | Path = LLM_MEMORY_MODEL_MANIFEST_PATH,
    datasetSha256: str | None = None,
) -> tuple[dict, dict[str, float | None]]:
    """evaluate 加载阈值文件前的最终校验。

    拒收三种情况：仍为 candidate 状态未经批准、split 标注错误、
    数据集散列不匹配。散列绑定防止这类操纵——在 holdout 上跑出
    不理想的结果后回头微调阈值重跑，反复几轮得到的「holdout 成绩」
    实际是对该数据集过拟合的；绑定散列后，每次调整阈值都必须
    更换一份数据集。
    """
    path = Path(calibrationPath)
    try:
        calibration = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvaluationError(f"无法读取 calibration: {path}") from exc
    if not isinstance(calibration, dict):
        raise EvaluationError("calibration 根对象无效")
    if calibration.get("status") == "candidate":
        raise EvaluationError("evaluate 不接受未审查的 candidate calibration")
    if calibration.get("calibrationSplit") not in (None, CALIBRATION_SPLIT):
        raise EvaluationError("calibration split 无效")

    from utils.llm.memory.retrieval import loadCalibratedThresholds

    thresholds, reason = loadCalibratedThresholds(
        path,
        manifestPath=manifestPath,
    )
    if reason:
        raise EvaluationError(f"calibration 无效: {reason}")
    if datasetSha256 is not None and calibration.get("datasetSha256") != datasetSha256:
        raise EvaluationError("calibration 与 fixture 数据集散列不匹配")
    return calibration, thresholds


def _safeRSS(process) -> int | None:
    """读取进程 RSS；测试替身或平台不支持时返回 ``None``。"""
    try:
        return int(process.memory_info().rss)
    except (AttributeError, StopIteration, TypeError, ValueError):
        return None


def _percentile(values: list[float], percentile: float) -> float | None:
    """使用线性插值计算延迟百分位。"""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * percentile / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    weight = position - lower
    return float(ordered[lower] + (ordered[upper] - ordered[lower]) * weight)


def _syntheticMemories(memoryCount: int) -> list[dict]:
    """生成 benchmark 用的假记忆（「用户正在处理第 N 类任务」），不含任何真实用户数据。"""
    return [{
        "id": memoryIndex + 1,
        "scope_type": "global",
        "scope_id": "global",
        "content": (
            f"合成记忆 {memoryIndex + 1}："
            f"用户正在处理第 {memoryIndex % 17} 类任务。"
        ),
        "tags": ["合成评测", f"类别{memoryIndex % 17}"],
        "retrievalHint": f"第 {memoryIndex % 17} 类任务进展",
        "enabled": True,
        "priority": 0,
        "source": "inferred",
        "mode": MEMORY_MODE_CONTEXTUAL,
    } for memoryIndex in range(memoryCount)]


def runRetrievalBenchmark(
    *,
    memoryCount: int,
    queryCount: int,
    concurrency: tuple[int, ...] = (1, 2, 4),
    modelDir: str | Path = LLM_MEMORY_MODEL_DIR,
    manifestPath: str | Path = LLM_MEMORY_MODEL_MANIFEST_PATH,
    encoderFactory=MemoryEncoder,
    processFactory=psutil.Process,
    queryTimeoutSeconds: float = 2.0,
) -> dict:
    """测量编码器自身的加载、索引、查询延迟与内存占用。

    使用合成记忆，全程不访问生产数据库；查询评分以互斥锁串行执行，
    模拟线上单工作线程的调度节奏。未覆盖的项目在报告中如实标注为
    not-run——线上队列调度、事件循环延迟需在目标机部署时另行测试。
    """
    if memoryCount < 1 or queryCount < 1:
        raise ValueError("memoryCount 和 queryCount 必须大于 0")
    concurrency = tuple(sorted({int(value) for value in concurrency}))
    if not concurrency or any(value < 1 for value in concurrency):
        raise ValueError("concurrency 必须只包含正整数")
    if queryTimeoutSeconds <= 0:
        raise ValueError("queryTimeoutSeconds 必须大于 0")

    process = processFactory()
    baselineRSS = _safeRSS(process)
    loadStarted = time.perf_counter()
    encoder = encoderFactory(modelDir=modelDir, manifestPath=manifestPath)
    loadSeconds = time.perf_counter() - loadStarted
    loadedRSS = _safeRSS(process)
    scorer = EncoderSemanticScorer(encoder)
    memories = _syntheticMemories(memoryCount)
    indexSeconds = None
    indexedRSS = None
    settledRSS = None
    matrixBytes = None
    concurrencyReports = {}
    try:
        indexStarted = time.perf_counter()
        for memory in memories:
            scorer.score(["索引预热"], [memory], includeHint=True)
        indexSeconds = time.perf_counter() - indexStarted
        indexedRSS = _safeRSS(process)

        queryTexts = [
            f"现在第 {queryIndex % 17} 类任务进展怎么样"
            for queryIndex in range(queryCount)
        ]
        lock = threading.Lock()

        def _runQuery(queryText: str) -> float:
            """在线程池中计时；共享 encoder 仍由锁约束为单实例串行评分。"""
            started = time.perf_counter()
            with lock:
                scorer.score([queryText], memories, includeHint=True)
            return time.perf_counter() - started

        for concurrencyLevel in concurrency:
            latencies = []
            timeoutCount = 0
            started = time.perf_counter()
            with ThreadPoolExecutor(max_workers=concurrencyLevel) as executor:
                futures = [executor.submit(_runQuery, text) for text in queryTexts]
                for future in futures:
                    latency = future.result()
                    latencies.append(latency)
                    if latency > queryTimeoutSeconds:
                        timeoutCount += 1
            concurrencyReports[str(concurrencyLevel)] = {
                "queryCount": queryCount,
                "wallSeconds": time.perf_counter() - started,
                "p50Seconds": _percentile(latencies, 50),
                "p95Seconds": _percentile(latencies, 95),
                "p99Seconds": _percentile(latencies, 99),
                "timeoutRatio": timeoutCount / queryCount,
                "timeoutCount": timeoutCount,
                "maxQueue": max(0, queryCount - concurrencyLevel),
            }
        settledRSS = _safeRSS(process)
        matrixBytes = sum(
            int(getattr(matrix, "nbytes", 0))
            for matrix in scorer._memoryCache.values()
        )
    finally:
        closeStarted = time.perf_counter()
        scorer.clear()
        encoder.close()
        closeSeconds = time.perf_counter() - closeStarted

    manifest = loadModelManifest(manifestPath)
    rssSamples = [
        value for value in (baselineRSS, loadedRSS, indexedRSS, settledRSS)
        if value is not None
    ]
    maxObservedDelta = (
        max(rssSamples) - baselineRSS
        if rssSamples and baselineRSS is not None else None
    )
    return {
        "schemaVersion": 1,
        "benchmark": "retrieval-encoder",
        "modelRevision": manifest["revision"],
        "encodingVersion": manifest["encodingVersion"],
        "memoryCount": memoryCount,
        "queryCount": queryCount,
        "concurrency": list(concurrency),
        "loadSeconds": loadSeconds,
        "indexSeconds": indexSeconds,
        "closeSeconds": closeSeconds,
        "matrixBytes": matrixBytes,
        "matrixBudgetBytes": 32 * 1024 * 1024,
        "rssBytes": {
            "baseline": baselineRSS,
            "loaded": loadedRSS,
            "indexed": indexedRSS,
            "settled": settledRSS,
            "maxObservedDelta": maxObservedDelta,
        },
        "concurrencyReports": concurrencyReports,
        "eventLoopHeartbeatP95Ms": None,
        "incrementalIndexReadySeconds": indexSeconds,
        "lifecycleScenarios": {
            "sameIDUpdate": "not-run",
            "differentIDUpdate": "not-run",
            "deleteThenQuery": "not-run",
            "restartCache": "not-run",
        },
        "isolated": False,
        "isolation": "caller-process",
        "note": (
            "该基准只测本地编码器和单实例串行评分，不连接生产数据库；"
            "runtime 的在线队列、事件循环心跳和生命周期场景需在目标机另行验收。"
        ),
    }


runBenchmark = runRetrievalBenchmark


def runEncoderBenchmark(
    *,
    memoryCount: int,
    queryCount: int,
    modelDir: str | Path = LLM_MEMORY_MODEL_DIR,
    manifestPath: str | Path = LLM_MEMORY_MODEL_MANIFEST_PATH,
    encoderFactory=MemoryEncoder,
    processFactory=psutil.Process,
) -> dict:
    """运行编码器资源基准（保留 Phase 0 的兼容接口）。"""
    if memoryCount < 1 or queryCount < 1:
        raise ValueError("memoryCount 和 queryCount 必须大于 0")

    process = processFactory()
    baselineRSS = int(process.memory_info().rss)
    loadStarted = time.perf_counter()
    encoder = None
    memories = _syntheticMemories(memoryCount)
    try:
        encoder = encoderFactory(modelDir=modelDir, manifestPath=manifestPath)
        loadSeconds = time.perf_counter() - loadStarted
        loadedRSS = int(process.memory_info().rss)

        memoryStarted = time.perf_counter()
        encodedChunks = 0
        for memory in memories:
            vectors = encoder.encodeMemory(memory)
            encodedChunks += int(vectors.shape[0])
        memorySeconds = time.perf_counter() - memoryStarted
        indexedRSS = int(process.memory_info().rss)

        queryTexts = [
            f"现在第 {queryIndex % 17} 类任务进展怎么样"
            for queryIndex in range(queryCount)
        ]
        queryStarted = time.perf_counter()
        queryVectors = encoder.encodeQueries(queryTexts)
        querySeconds = time.perf_counter() - queryStarted
        finishedRSS = int(process.memory_info().rss)
    finally:
        if encoder is not None:
            encoder.close()

    manifest = loadModelManifest(manifestPath)
    return {
        "schemaVersion": 1,
        "modelRevision": manifest["revision"],
        "encodingVersion": manifest["encodingVersion"],
        "memoryCount": memoryCount,
        "memoryChunkCount": encodedChunks,
        "queryCount": int(queryVectors.shape[0]),
        "loadSeconds": loadSeconds,
        "memoryEncodeSeconds": memorySeconds,
        "queryEncodeSeconds": querySeconds,
        "rssBytes": {
            "baseline": baselineRSS,
            "loaded": loadedRSS,
            "indexed": indexedRSS,
            "finished": finishedRSS,
            "maxObservedDelta": max(loadedRSS, indexedRSS, finishedRSS) - baselineRSS,
        },
        "note": "RSS 为阶段采样，不代表采样间的瞬时峰值",
    }


def _buildParser() -> argparse.ArgumentParser:
    """构造 encoder/calibrate/evaluate/benchmark 四个离线子命令。"""
    parser = argparse.ArgumentParser(description="LLM memory 离线评测")
    subparsers = parser.add_subparsers(dest="command", required=True)

    encoderParser = subparsers.add_parser("encoder", help="运行编码器资源基准")
    encoderParser.add_argument("--memories", type=int, default=1000)
    encoderParser.add_argument("--queries", type=int, default=100)
    encoderParser.add_argument("--model-dir", default=LLM_MEMORY_MODEL_DIR)
    encoderParser.add_argument("--manifest", default=LLM_MEMORY_MODEL_MANIFEST_PATH)
    encoderParser.add_argument(
        "--output",
        default=str(Path(LLM_MEMORY_REPORT_DIR) / "encoder.json"),
    )

    calibrateParser = subparsers.add_parser(
        "calibrate",
        help="只用 calibration split 生成待审查阈值",
    )
    calibrateParser.add_argument("--cases", required=True)
    calibrateParser.add_argument("--output", required=True)
    calibrateParser.add_argument("--model-dir", default=LLM_MEMORY_MODEL_DIR)
    calibrateParser.add_argument("--manifest", default=LLM_MEMORY_MODEL_MANIFEST_PATH)

    evaluateParser = subparsers.add_parser(
        "evaluate",
        help="使用固定 calibration 评估 holdout",
    )
    evaluateParser.add_argument("--cases", required=True)
    evaluateParser.add_argument("--split", default=HOLDOUT_SPLIT)
    evaluateParser.add_argument(
        "--calibration",
        default=str(LLM_MEMORY_CALIBRATION_PATH),
    )
    evaluateParser.add_argument("--model-dir", default=LLM_MEMORY_MODEL_DIR)
    evaluateParser.add_argument("--manifest", default=LLM_MEMORY_MODEL_MANIFEST_PATH)
    evaluateParser.add_argument("--output")
    evaluateParser.add_argument(
        "--modes",
        nargs="+",
        choices=EVALUATION_MODES,
        default=list(EVALUATION_MODES),
    )

    benchmarkParser = subparsers.add_parser(
        "benchmark",
        help="运行本地编码器热查询和并发基准",
    )
    benchmarkParser.add_argument("--memories", type=int, default=1000)
    benchmarkParser.add_argument("--queries", type=int, default=100)
    benchmarkParser.add_argument("--concurrency", nargs="+", type=int, default=[1, 2, 4])
    benchmarkParser.add_argument("--model-dir", default=LLM_MEMORY_MODEL_DIR)
    benchmarkParser.add_argument("--manifest", default=LLM_MEMORY_MODEL_MANIFEST_PATH)
    benchmarkParser.add_argument("--output")
    return parser


def _createScorer(modelDir, manifestPath):
    """创建单个固定模型 encoder 及其离线评分适配器。"""
    encoder = MemoryEncoder(modelDir=modelDir, manifestPath=manifestPath)
    return encoder, EncoderSemanticScorer(encoder)


def main() -> int:
    """执行离线子命令，并确保创建过的 encoder 在退出前关闭。"""
    args = _buildParser().parse_args()
    encoder = None
    try:
        if args.command == "encoder":
            report = runEncoderBenchmark(
                memoryCount=args.memories,
                queryCount=args.queries,
                modelDir=args.model_dir,
                manifestPath=args.manifest,
            )
            _writeReport(report, args.output)
            return 0

        if args.command == "calibrate":
            cases, datasetSha256 = loadEvaluationCases(args.cases)
            encoder, scorer = _createScorer(args.model_dir, args.manifest)
            report = calibrateRetrievalThresholds(
                cases,
                semanticScorer=scorer,
                manifestPath=args.manifest,
                datasetSha256=datasetSha256,
            )
            if Path(args.output).resolve() == Path(
                LLM_MEMORY_CALIBRATION_PATH
            ).resolve():
                raise EvaluationError(
                    "calibrate 不允许直接覆盖正式 retrievalCalibration.json"
                )
            _writeReport(report, args.output)
            return 0

        if args.command == "evaluate":
            cases, datasetSha256 = loadEvaluationCases(args.cases)
            calibration, thresholds = loadApprovedCalibration(
                args.calibration,
                manifestPath=args.manifest,
                datasetSha256=datasetSha256,
            )
            semanticEnabled = any(
                thresholds[name] is not None
                for name in ("semanticCurrent", "semanticAssisted")
            ) and any(mode in {"hybrid", "hybrid+hint"} for mode in args.modes)
            scorer = None
            if semanticEnabled:
                encoder, scorer = _createScorer(args.model_dir, args.manifest)
            report = evaluateRetrievalCases(
                cases,
                thresholds,
                semanticScorer=scorer,
                split=args.split,
                modes=tuple(args.modes),
            )
            report["datasetSha256"] = datasetSha256
            report["calibration"] = {
                "path": str(args.calibration),
                "datasetSha256": calibration.get("datasetSha256"),
                "modelRevision": calibration.get("modelRevision"),
            }
            _writeReport(report, args.output)
            return 0

        report = runRetrievalBenchmark(
            memoryCount=args.memories,
            queryCount=args.queries,
            concurrency=tuple(args.concurrency),
            modelDir=args.model_dir,
            manifestPath=args.manifest,
        )
        _writeReport(report, args.output)
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"memory 评测失败: {exc}", file=sys.stderr)
        return 1
    finally:
        if encoder is not None:
            encoder.close()


if __name__ == "__main__":
    raise SystemExit(main())
