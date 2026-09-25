#!/usr/bin/env python3
"""
scripts/evaluateMemory.py

回答一个问题：hybrid 检索的分数阈值该定多少、定完效果好不好。

工作对象是人工标注的脱敏测试数据（JSON fixture，每条场景写明
「这些记忆里哪些必须召回 / 哪些无所谓 / 哪些禁止召回」），全程
不碰生产数据库、不调用生成模型。子命令分工：

- calibrate：只拿标成 calibration split 的场景试出各通道阈值，
  产出「候选」文件——必须有人审查批准后才能换成正式的；
- evaluate：拿未参与调阈值的 holdout split 验收，
  只认已批准的正式阈值文件；
- validate：只校验 fixture 契约并输出数据集散列，不加载模型；
- margin：仅用 calibration 比较绝对阈值与 top-margin，产出实验报告；
- encoder / benchmark：测模型加载耗时、内存占用、查询延迟。

评测直接 import 线上检索用的那几个函数来打分，不在脚本里另写一套
评分逻辑。它覆盖查询构造、通道打分、融合与预算结果，但不等同于完整
线上链路：数据库 IO、runtime 队列、deadline 与事件循环仍需另做冒烟测试。
"""

import argparse
from dataclasses import fields, is_dataclass
import hashlib
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
    buildSemanticQueryPlan,
    deduplicateMemoryCandidates,
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
# legacy = 旧选择规则基线；lexical = 仅词面通道；
# hybrid / hybrid+hint = 语义通道不带 / 带 retrievalHint 编码（对照实验）
EVALUATION_MODES = ("legacy", "lexical", "hybrid", "hybrid+hint")
# fixture 按 groupID 固定划入两 split：calibration 调阈值，holdout 验收；
# 同一事实的近似场景只进一个 split，防止校准集向保留集泄漏
CALIBRATION_SPLIT = "calibration"
HOLDOUT_SPLIT = "holdout"
CALIBRATION_PRECISION_TARGET = 0.95
# 两种分数量纲不同，实验网格分别声明；这些值不属于线上配置。
SEMANTIC_MARGIN_GRID = (0.01, 0.02, 0.05, 0.10)
LEXICAL_MARGIN_GRID = (0.5, 1.0, 2.0, 5.0)
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


def _normalizeQueryNow(value, caseID: str) -> datetime | None:
    """解析可选的评测时钟，使近期历史窗口不依赖执行当天。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise EvaluationError(f"{caseID}.queryNow 必须是 ISO datetime") from exc


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
    declaredCounts = {}
    if isinstance(rawData, list):
        rawCases = rawData
    elif isinstance(rawData, dict):
        if rawData.get("schemaVersion", 1) != 1:
            raise EvaluationError("fixture schemaVersion 不受支持")
        rawCases = rawData.get("cases")
        declaredCounts = {
            split: rawData.get(f"{split}CaseCount")
            for split in (CALIBRATION_SPLIT, HOLDOUT_SPLIT)
            if f"{split}CaseCount" in rawData
        }
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
        requiredPinnedIDs = _normalizeIDSet(
            rawCase.get("requiredPinnedIDs", []),
            "requiredPinnedIDs",
            caseID,
        )
        allowedPinnedIDs = _normalizeIDSet(
            rawCase.get("allowedPinnedIDs", []),
            "allowedPinnedIDs",
            caseID,
        )
        forbiddenPinnedIDs = _normalizeIDSet(
            rawCase.get("forbiddenPinnedIDs", []),
            "forbiddenPinnedIDs",
            caseID,
        )
        knownIDs = set(memoryIDs)
        for fieldName, ids in (
            ("requiredIDs", requiredIDs),
            ("allowedIDs", allowedIDs),
            ("forbiddenIDs", forbiddenIDs),
            ("requiredPinnedIDs", requiredPinnedIDs),
            ("allowedPinnedIDs", allowedPinnedIDs),
            ("forbiddenPinnedIDs", forbiddenPinnedIDs),
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
        if requiredPinnedIDs.intersection(forbiddenPinnedIDs):
            raise EvaluationError(
                f"{caseID} requiredPinnedIDs 与 forbiddenPinnedIDs 冲突"
            )
        if allowedPinnedIDs.intersection(forbiddenPinnedIDs):
            raise EvaluationError(
                f"{caseID} allowedPinnedIDs 与 forbiddenPinnedIDs 冲突"
            )

        pinnedIDs = {
            memory["id"] for memory in memories
            if memory.get("mode") == MEMORY_MODE_PINNED
        }
        for fieldName, ids in (
            ("requiredPinnedIDs", requiredPinnedIDs),
            ("allowedPinnedIDs", allowedPinnedIDs),
            ("forbiddenPinnedIDs", forbiddenPinnedIDs),
        ):
            nonPinnedIDs = ids.difference(pinnedIDs)
            if nonPinnedIDs:
                raise EvaluationError(
                    f"{caseID}.{fieldName} 必须只引用 pinned memory: "
                    f"{sorted(nonPinnedIDs)}"
                )
        contextualLabelsOnPinned = (
            requiredIDs | allowedIDs | forbiddenIDs
        ).intersection(pinnedIDs)
        if contextualLabelsOnPinned:
            raise EvaluationError(
                f"{caseID} contextual 标注引用了 pinned memory: "
                f"{sorted(contextualLabelsOnPinned)}；请使用 *PinnedIDs"
            )

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

        normalizedQuery = _normalizeQuery(rawCase["query"], caseID)
        normalizedQueryNow = _normalizeQueryNow(
            rawCase.get("queryNow"),
            caseID,
        )
        if normalizedQuery.history and normalizedQueryNow is None:
            raise EvaluationError(
                f"{caseID}.queryNow 在包含 history 时不能为空"
            )

        cases.append({
            "caseID": caseID,
            "groupID": groupID,
            "split": split,
            "scope": _normalizeScope(rawCase.get("scope"), caseID),
            "memories": memories,
            "query": normalizedQuery,
            "queryNow": normalizedQueryNow,
            "requiredIDs": requiredIDs,
            "allowedIDs": allowedIDs,
            "forbiddenIDs": forbiddenIDs,
            "requiredPinnedIDs": requiredPinnedIDs,
            "allowedPinnedIDs": allowedPinnedIDs,
            "forbiddenPinnedIDs": forbiddenPinnedIDs,
            "allowAbstain": allowAbstain,
            "subsets": subsets,
            "metadata": dict(rawCase.get("metadata", {}))
            if isinstance(rawCase.get("metadata", {}), dict) else {},
        })

    actualCounts = {
        split: sum(1 for case in cases if case["split"] == split)
        for split in (CALIBRATION_SPLIT, HOLDOUT_SPLIT)
    }
    for split, declaredCount in declaredCounts.items():
        if (
            isinstance(declaredCount, bool)
            or not isinstance(declaredCount, int)
            or declaredCount != actualCounts[split]
        ):
            raise EvaluationError(
                f"{split}CaseCount 与实际场景数不一致"
            )
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


def _normalizeSemanticScoreSet(rawScores) -> dict[str, dict[int, float]]:
    """把 scorer 的单查询结果规范为 base/enhanced 两组有限分数。

    旧测试替身若只返回一个 score map，则两种表示共用它；真实 encoder
    必须返回两个命名字段，才能让评测分别控制准入与排序。
    """
    if isinstance(rawScores, dict) and (
        "base" in rawScores or "enhanced" in rawScores
    ):
        baseScores = _normalizeScoreMap(rawScores.get("base", {}))
        enhancedScores = _normalizeScoreMap(
            rawScores.get("enhanced", baseScores)
        )
        return {"base": baseScores, "enhanced": enhancedScores}

    normalized = _normalizeScoreMap(rawScores)
    return {"base": normalized, "enhanced": dict(normalized)}


def _invokeSemanticScorer(
    semanticScorer,
    queryTexts: list[str],
    candidates: list[dict],
    *,
    channelNames: tuple[str, ...] | None = None,
) -> list[dict[str, dict[int, float]]]:
    """适配评测 scorer 的返回形态，并统一校验双表示 score map。

    接受多种替身（测试 fake / EncoderSemanticScorer / 裸函数），
    让单测能注入 mock 而不依赖真实模型；坏 ID 和非有限分数静默丢弃。
    """
    if semanticScorer is None or not queryTexts:
        return [
            {"base": {}, "enhanced": {}}
            for _ in queryTexts
        ]

    scorer = getattr(semanticScorer, "score", semanticScorer)
    rawResult = scorer(queryTexts, candidates)

    if isinstance(rawResult, dict) and any(
        name in rawResult for name in CHANNEL_NAMES
    ):
        names = channelNames or CHANNEL_NAMES
        return [
            _normalizeSemanticScoreSet(rawResult.get(name, {}))
            for name in names[:len(queryTexts)]
        ]
    if isinstance(rawResult, (list, tuple)):
        if len(rawResult) == len(queryTexts):
            return [_normalizeSemanticScoreSet(value) for value in rawResult]
        if len(rawResult) == 1 and len(queryTexts) > 1:
            normalized = _normalizeSemanticScoreSet(rawResult[0])
            return [{
                "base": dict(normalized["base"]),
                "enhanced": dict(normalized["enhanced"]),
            } for _ in queryTexts]
    if isinstance(rawResult, dict):
        normalized = _normalizeSemanticScoreSet(rawResult)
        return [{
            "base": dict(normalized["base"]),
            "enhanced": dict(normalized["enhanced"]),
        } for _ in queryTexts]
    raise EvaluationError("semantic scorer 必须返回 score map 或 score map 数组")


def scoreCaseChannels(
    case: dict,
    *,
    semanticScorer=None,
    thresholds: dict[str, float | None] | None = None,
) -> tuple[
    dict[str, dict[int, float]],
    dict[str, dict[int, float]],
]:
    """返回三通道准入分，以及两个语义通道的 enhanced 排序分。

    ``thresholds`` 决定语义查询计划，必须与线上准入使用的阈值相同：
    当 current 与 assisted 文本相同时，只会给 canonical 通道评分；
    current 关闭时才允许 assisted 接管。省略该参数时按两个语义通道
    都启用处理。第一组语义分数始终来自 base，供准入与 calibration；
    第二组才来自含 hint 的 enhanced，供 hybrid+hint 排序。
    """
    candidates = _scopeCandidates(case)
    contextual = _contextualCandidates(candidates)
    currentText, assistedText, lexicalText = buildQueryTexts(
        case["query"],
        now=case.get("queryNow"),
    )
    scores = {name: {} for name in CHANNEL_NAMES}
    semanticRankingScores = {
        "semanticCurrent": {},
        "semanticAssisted": {},
    }

    if lexicalText:
        scores["lexical"] = scoreLexicalCandidates(lexicalText, contextual)

    activeSemanticThresholds = thresholds
    if activeSemanticThresholds is None:
        activeSemanticThresholds = {
            "semanticCurrent": 0.0,
            "semanticAssisted": 0.0,
        }
    semanticPlan = buildSemanticQueryPlan(
        currentText,
        assistedText,
        activeSemanticThresholds,
    )
    semanticNames = [name for name, _ in semanticPlan]
    semanticTexts = [textValue for _, textValue in semanticPlan]

    if semanticTexts and semanticScorer is not None:
        semanticResults = _invokeSemanticScorer(
            semanticScorer,
            semanticTexts,
            contextual,
            channelNames=tuple(semanticNames),
        )
        for name, result in zip(semanticNames, semanticResults):
            scores[name] = result["base"]
            semanticRankingScores[name] = result["enhanced"]
    return scores, semanticRankingScores


class EncoderSemanticScorer:
    """将 ``MemoryEncoder`` 适配为离线评测用的语义评分器。"""

    def __init__(self, encoder):
        """持有单个 encoder，并按 memory 内容缓存成对的 chunk 矩阵。"""
        self.encoder = encoder
        self._memoryCache = {}

    def _cacheKey(self, memory: dict):
        """构造绑定正文、标签和 hint 的双表示缓存键。"""
        return (
            int(memory["id"]),
            _canonicalDigest({
                "content": memory.get("content", ""),
                "tags": memory.get("tags", []),
                "retrievalHint": memory.get("retrievalHint"),
            }),
        )

    def score(
        self,
        queryTexts: list[str],
        candidates: list[dict],
    ) -> list[dict[str, dict[int, float]]]:
        """编码查询，并返回每个查询的 base/enhanced 最大 chunk 分数。"""
        queryVectors = self.encoder.encodeQueries(queryTexts)
        results = [
            {"base": {}, "enhanced": {}}
            for _ in queryTexts
        ]
        for memory in candidates:
            key = self._cacheKey(memory)
            representations = self._memoryCache.get(key)
            if representations is None:
                representations = self.encoder.encodeMemoryRepresentations(memory)
                self._memoryCache[key] = representations
            for queryIndex in range(len(queryTexts)):
                baseSimilarities = (
                    representations.base @ queryVectors[queryIndex]
                )
                baseScore = float(baseSimilarities.max())
                if representations.enhanced is representations.base:
                    enhancedScore = baseScore
                else:
                    enhancedSimilarities = (
                        representations.enhanced @ queryVectors[queryIndex]
                    )
                    enhancedScore = float(enhancedSimilarities.max())
                memoryID = int(memory["id"])
                if math.isfinite(baseScore):
                    results[queryIndex]["base"][memoryID] = baseScore
                if math.isfinite(enhancedScore):
                    results[queryIndex]["enhanced"][memoryID] = enhancedScore
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
    """复现 contextual 的 legacy 截断；pinned 由调用方走独立预算。"""
    byScope = defaultdict(list)
    # legacy 的 20/10 是 contextual 配额，不能让常驻 pinned 干扰离线
    # 基线；这与线上 retrieveMemoryContext 的分流保持一致。
    for memory in _contextualCandidates(candidates):
        byScope[(memory.get("scope_type"), memory.get("scope_id"))].append(memory)
    pool = []
    for scopeMemories in byScope.values():
        pool.extend(selectLegacyMemoryCandidates(
            scopeMemories,
            totalLimit=max(int(perScopeLimit), 0),
        ))
    return selectLegacyMemoryCandidates(pool, totalLimit=totalLimit)


def _topMarginEvidence(scores: dict[int, float]) -> dict:
    """取绝对阈值过滤前的前两名，返回无正文的通道领先证据。

    语义调用方必须传 base；不能用 enhanced 的高分反向授予准入资格。
    第二名即使低于绝对阈值也必须参与比较。仅有一个有效分数时差值未知，
    不把缺失的竞争者当成零分或无穷大的领先；同分自然得到零 margin。
    """
    ordered = sorted(
        _normalizeScoreMap(scores).items(),
        key=lambda item: (-item[1], item[0]),
    )
    top = ordered[0] if ordered else (None, None)
    second = ordered[1] if len(ordered) > 1 else (None, None)
    return {
        "scoreCount": len(ordered),
        "topMemoryID": top[0],
        "topScore": top[1],
        "secondMemoryID": second[0],
        "secondScore": second[1],
        "topGap": top[1] - second[1] if second[1] is not None else None,
    }


def _evaluateSingleCase(
    case: dict,
    mode: str,
    thresholds: dict[str, float | None],
    *,
    semanticScorer=None,
    channelScores: dict[str, dict[int, float]] | None = None,
    semanticRankingScores: dict[str, dict[int, float]] | None = None,
    perScopeLimit: int = LLM_MEMORY_RETRIEVE_PER_SCOPE,
    totalLimit: int = LLM_MEMORY_RETRIEVE_TOTAL,
    maxChars: int = LLM_MEMORY_CONTEXT_MAX_CHARS,
    pinnedMaxChars: int = LLM_MEMORY_PINNED_MAX_CHARS,
    channelMargins: dict[str, float] | None = None,
    topOneOnly: bool = False,
) -> dict:
    """执行单场景；margin 与 topOneOnly 仅供离线消融实验显式启用。

    margin 只关闭证据不足的通道，不改变该通道的绝对阈值，也不限制
    通过后的候选数。topOneOnly 则在去重后截断 contextual，量出只取
    一条的损失；两者都不影响 pinned，默认关闭时保持线上等价行为。
    """
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
            channelScores, semanticRankingScores = scoreCaseChannels(
                case,
                semanticScorer=semanticScorer,
                thresholds=thresholds,
            )
        activeThresholds = {
            name: thresholds.get(name)
            if mode != "lexical" or name == "lexical"
            else None
            for name in CHANNEL_NAMES
        }
        marginEvidence = {}
        for name, minimumGap in (channelMargins or {}).items():
            evidence = _topMarginEvidence(channelScores.get(name, {}))
            gap = evidence["topGap"]
            passed = gap is not None and gap >= minimumGap
            marginEvidence[name] = {
                **evidence,
                "minimumGap": minimumGap,
                "passed": passed,
                "absoluteThreshold": activeThresholds[name],
            }
            # 关闭通道表达弃权，保留原始分数用于解释；不能让 enhanced
            # 的领先幅度替代 base 证据，也不据此启用其他备用通道。
            if not passed:
                activeThresholds[name] = None
        contextual, selectionDiagnostics = selectContextualCandidates(
            _contextualCandidates(candidates),
            channelScores,
            activeThresholds,
            semanticRankingScores=(
                semanticRankingScores
                if mode == "hybrid+hint"
                else None
            ),
            includeEvidence=True,
        )
        if channelMargins:
            selectionDiagnostics["marginEvidence"] = marginEvidence

    # 与线上汇合点保持一致：先让 pinned 占据其排序位置，再按 scope+正文
    # 去重。否则离线指标会统计生产 prompt 根本不会包含的重复候选。
    combined = deduplicateMemoryCandidates([*pinned, *contextual])
    pinned = [
        memory for memory in combined
        if memory.get("mode") == MEMORY_MODE_PINNED
    ]
    contextual = [
        memory for memory in combined
        if memory.get("mode", MEMORY_MODE_CONTEXTUAL) == MEMORY_MODE_CONTEXTUAL
    ]
    contextualAfterDeduplication = {int(memory["id"]) for memory in contextual}
    if topOneOnly:
        selectionDiagnostics["topOneDroppedIDs"] = [
            int(memory["id"]) for memory in contextual[1:]
        ]
        contextual = contextual[:1]
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
    renderedContextual = set(selectedContextualIDs)
    for evidence in selectionDiagnostics.get("candidateEvidence", []):
        memoryID = int(evidence["memoryID"])
        evidence["survivedDeduplication"] = (
            memoryID in contextualAfterDeduplication
            if evidence["fusedQualified"]
            else None
        )
        evidence["rendered"] = memoryID in renderedContextual
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
    顶替 required 计入召回完成；forbidden = 召回即失败。Pinned 使用
    独立的可选标注字段，避免把常驻预算条目混入 contextual 指标。
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
    predictedPinnedIDs = set(result.get("pinnedIDs", []))
    requiredPinnedIDs = set(case.get("requiredPinnedIDs", set()))
    allowedPinnedIDs = set(case.get("allowedPinnedIDs", set()))
    forbiddenPinnedIDs = set(case.get("forbiddenPinnedIDs", set()))
    acceptablePinnedIDs = requiredPinnedIDs.union(allowedPinnedIDs)
    pinnedHitIDs = predictedPinnedIDs.intersection(requiredPinnedIDs)
    pinnedFalsePositiveIDs = (
        predictedPinnedIDs.difference(acceptablePinnedIDs)
        if requiredPinnedIDs or allowedPinnedIDs or forbiddenPinnedIDs
        else set()
    )
    pinnedForbiddenHitIDs = predictedPinnedIDs.intersection(forbiddenPinnedIDs)
    pinnedRecall = (
        len(pinnedHitIDs) / len(requiredPinnedIDs)
        if requiredPinnedIDs else None
    )
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
        "requiredPinnedIDs": sorted(requiredPinnedIDs),
        "allowedPinnedIDs": sorted(allowedPinnedIDs),
        "forbiddenPinnedIDs": sorted(forbiddenPinnedIDs),
        "hitRequiredPinnedIDs": sorted(pinnedHitIDs),
        "missedRequiredPinnedIDs": sorted(
            requiredPinnedIDs.difference(predictedPinnedIDs)
        ),
        "pinnedFalsePositiveIDs": sorted(pinnedFalsePositiveIDs),
        "pinnedForbiddenHitIDs": sorted(pinnedForbiddenHitIDs),
        "pinnedRecall": pinnedRecall,
        "precision": precision,
        "recall": recall,
        "abstained": not predictedIDs,
        "abstainAllowed": case["allowAbstain"],
        "subsets": list(case.get("subsets", [])),
    }


def _aggregateMetrics(caseResults: list[dict]) -> dict:
    """把逐场景结果汇总成总体指标，并给出「能不能上线」的判定。

    统计口径：precision = 全部场景选中的记忆里标注认可的比例（只统计
    contextual，pinned 另行统计）；recall = contextual 必须召回的命中
    比例。某场景一条未选计为「弃权」——全部弃权时 precision 记 None 而非
    100%（零预测不能计为精确）。qualityGate 是启用 hybrid 的硬条件：
    contextual 至少 30 个场景、精确率 ≥95%、召回率 ≥80%、禁入条目零命中，
    并且在存在 pinned 标注时满足 pinned recall ≥80%、pinned 禁入零命中，
    全部条件满足才通过。
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
    pinnedRequiredCount = sum(
        len(result.get("requiredPinnedIDs", [])) for result in caseResults
    )
    pinnedHitCount = sum(
        len(result.get("hitRequiredPinnedIDs", [])) for result in caseResults
    )
    pinnedForbiddenHitCount = sum(
        len(result.get("pinnedForbiddenHitIDs", [])) for result in caseResults
    )
    pinnedCaseCount = sum(
        1
        for result in caseResults
        if (
            result.get("requiredPinnedIDs")
            or result.get("allowedPinnedIDs")
            or result.get("forbiddenPinnedIDs")
        )
    )
    abstainedCount = sum(1 for result in caseResults if result["abstained"])
    unexpectedAbstentionCount = sum(
        1 for result in caseResults
        if result["abstained"] and not result["abstainAllowed"]
    )
    precision = acceptedCount / predictedCount if predictedCount else None
    recall = hitCount / requiredCount if requiredCount else None
    pinnedRecall = (
        pinnedHitCount / pinnedRequiredCount
        if pinnedRequiredCount else None
    )
    pinnedGatePassed = (
        pinnedForbiddenHitCount == 0
        and (
            pinnedRecall is None
            or pinnedRecall >= 0.80
        )
    )

    return {
        "caseCount": len(caseResults),
        "predictedContextualCount": predictedCount,
        "acceptedPredictionCount": acceptedCount,
        "requiredCount": requiredCount,
        "requiredHitCount": hitCount,
        "forbiddenHitCount": forbiddenHitCount,
        "pinnedCaseCount": pinnedCaseCount,
        "pinnedRequiredCount": pinnedRequiredCount,
        "pinnedRequiredHitCount": pinnedHitCount,
        "pinnedForbiddenHitCount": pinnedForbiddenHitCount,
        "pinnedRecall": pinnedRecall,
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
            "pinnedRecallTarget": 0.80,
            "pinnedRecallPassed": pinnedGatePassed,
            "pinnedForbiddenPassed": pinnedForbiddenHitCount == 0,
            "pinnedPassed": pinnedGatePassed,
            "passed": (
                len(caseResults) >= 30
                and
                precision is not None
                and precision >= 0.95
                and recall is not None
                and recall >= 0.80
                and forbiddenHitCount == 0
                and pinnedGatePassed
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
            semanticRankingScores = None
            if mode in {"lexical", "hybrid", "hybrid+hint"}:
                cacheKey = case["caseID"]
                if cacheKey not in channelCache:
                    channelCache[cacheKey] = scoreCaseChannels(
                        case,
                        semanticScorer=semanticScorer,
                        thresholds=thresholds,
                    )
                channelScores, semanticRankingScores = channelCache[cacheKey]
            caseResults.append(_scoreCaseResult(
                case,
                _evaluateSingleCase(
                    case,
                    mode,
                    thresholds,
                    semanticScorer=semanticScorer,
                    channelScores=channelScores,
                    semanticRankingScores=semanticRankingScores,
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
        "schemaVersion": 2,
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
) -> dict[str, list[dict]]:
    """把 calibration 场景展开为各通道的候选分数与标注观察值。

    校准时阈值尚未产生，因而分别运行一次 current-only 和
    assisted-only 查询计划，收集两个通道各自的原始分布。对于相同的
    query 文本，这两次观察用于校准“current 关闭时 assisted 接管”的
    备用路径，但在实际评估/线上融合时仍只会启用一个 canonical 通道。
    topGap 记录该通道全池前两名的 base/词面差值，仅供 margin 研究过滤
    观察集；普通绝对阈值校准继续只读取 score，不使用差值。
    """
    observations = {name: [] for name in CHANNEL_NAMES}
    for case in cases:
        currentScores, _ = scoreCaseChannels(
            case,
            semanticScorer=semanticScorer,
            thresholds={
                "semanticCurrent": 0.0,
                "semanticAssisted": None,
            },
        )
        assistedScores, _ = scoreCaseChannels(
            case,
            semanticScorer=semanticScorer,
            thresholds={
                "semanticCurrent": None,
                "semanticAssisted": 0.0,
            },
        )
        scores = {
            "semanticCurrent": currentScores["semanticCurrent"],
            "semanticAssisted": assistedScores["semanticAssisted"],
            # 词面结果与语义 hint 无关；只取一份，避免重复观察污染
            # channel report 的样本数。
            "lexical": currentScores["lexical"],
        }
        positiveIDs = set(case["requiredIDs"]).union(case["allowedIDs"])
        forbiddenIDs = set(case["forbiddenIDs"])
        for channelName in CHANNEL_NAMES:
            topGap = _topMarginEvidence(scores[channelName])["topGap"]
            for memoryID, score in scores[channelName].items():
                observations[channelName].append({
                    "caseID": case["caseID"],
                    "memoryID": memoryID,
                    "score": score,
                    "topGap": topGap,
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
    scoreCounts = defaultdict(lambda: [0, 0, 0])
    for observation in observations:
        score = float(observation["score"])
        if score >= 0:
            counts = scoreCounts[score]
            counts[0] += 1
            counts[1] += bool(observation["positive"])
            counts[2] += bool(observation["forbidden"])
    finiteScores = sorted(score for score in scoreCounts if math.isfinite(score))
    best = None
    qualifiedCount, acceptedCount, forbiddenHits = scoreCounts.get(math.inf, (0, 0, 0))
    # 从高到低累计分数桶，与逐阈值重扫全部观察值等价，复杂度从 O(N²)
    # 降到 O(N log N)。同分整桶加入，不能人为拆开相同分数的正负候选。
    # 每遇到合格点都更新，最终留下最低可行阈值；precision 不保证单调，
    # 因此不能遇到首个合格点就提前停止。
    for threshold in reversed(finiteScores):
        count, positives, forbidden = scoreCounts[threshold]
        qualifiedCount += count
        acceptedCount += positives
        forbiddenHits += forbidden
        precision = acceptedCount / qualifiedCount
        if precision >= precisionTarget and forbiddenHits <= maxForbiddenHits:
            best = {
                "threshold": threshold,
                "qualifiedCount": qualifiedCount,
                "acceptedCount": acceptedCount,
                "precision": precision,
                "forbiddenHits": forbiddenHits,
            }

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
) -> dict:
    """只用 calibration split 的 base 分数生成候选 calibration。

    retrievalHint 永远不参与阈值选择；它只在上线等价的 hybrid+hint
    评测中重排已经通过这里阈值的记忆。
    """
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
        "schemaVersion": 2,
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
        "semanticAdmissionRepresentation": "base",
        "semanticRankingRepresentation": "enhanced",
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


def _marginStudyMetrics(results: list[dict]) -> dict:
    """复用检索指标，补上无答案误召回和多 required 完整召回的分母。

    实验来自 calibration，去掉上线 gate，避免样本内分数被误认成验收。
    无答案要求 required/allowed 都为空；只有 allowed 的场景不算无答案。
    """
    metrics = _aggregateMetrics(results)
    metrics.pop("qualityGate")
    metrics.pop("minimumCaseCountMet")
    noAnswer = [
        result for result in results
        if not result["requiredIDs"] and not result["allowedIDs"]
    ]
    multiRequired = [result for result in results if len(result["requiredIDs"]) > 1]
    falseRecallCount = sum(not result["abstained"] for result in noAnswer)
    completeCount = sum(not result["missedRequiredIDs"] for result in multiRequired)
    metrics.update({
        "noAnswerCaseCount": len(noAnswer),
        "noAnswerFalseRecallCount": falseRecallCount,
        "noAnswerFalseRecallRate": falseRecallCount / len(noAnswer) if noAnswer else None,
        "multiRequiredCaseCount": len(multiRequired),
        "multiRequiredCompleteCount": completeCount,
        "multiRequiredCompleteRate": completeCount / len(multiRequired) if multiRequired else None,
    })
    return metrics


def studyTopMargins(
    cases: list[dict],
    *,
    semanticScorer=None,
    manifestPath: str | Path = LLM_MEMORY_MODEL_MANIFEST_PATH,
    semanticMargins: tuple[float, ...] = SEMANTIC_MARGIN_GRID,
    lexicalMargins: tuple[float, ...] = LEXICAL_MARGIN_GRID,
) -> dict:
    """仅在 calibration 上研究绝对阈值与通道 top-gap 的组合。

    每次只变一个通道：fixed 保留基线绝对阈值；recalibrated 在 margin
    过滤后的观察集上重选绝对阈值，仍要求 precision >= 0.95、forbidden=0。
    后者能检验 margin 是否允许降低绝对阈值，而不仅仅是多删几条记忆。
    所有变体走同一 RRF、去重和预算；不把各通道单独最好的方案拼成最优。
    """
    for grid in (semanticMargins, lexicalMargins):
        if not grid or any(
            not isinstance(value, (int, float)) or isinstance(value, bool)
            or not math.isfinite(value) or value <= 0
            for value in grid
        ):
            raise EvaluationError("margin 网格必须是非空的有限正数列表")
    calibrationCases = splitEvaluationCases(cases, CALIBRATION_SPLIT)
    if not any(
        not case["requiredIDs"] and not case["allowedIDs"]
        and case["allowAbstain"] for case in calibrationCases
    ) or not any(len(case["requiredIDs"]) > 1 for case in calibrationCases):
        raise EvaluationError("margin 实验需要无答案和多 required 的 calibration 场景")

    baselineCalibration = calibrateRetrievalThresholds(
        cases,
        semanticScorer=semanticScorer,
        manifestPath=manifestPath,
    )
    baselineThresholds = baselineCalibration["thresholds"]
    observations = _calibrationObservations(
        calibrationCases, semanticScorer=semanticScorer,
    )
    variants = [{
        "variantID": "absolute-only",
        "thresholds": baselineThresholds,
        "channelMargins": {},
        "topOneOnly": False,
    }, {
        "variantID": "top-one-control",
        "thresholds": baselineThresholds,
        "channelMargins": {},
        "topOneOnly": True,
    }]
    for channelName in CHANNEL_NAMES:
        grid = lexicalMargins if channelName == "lexical" else semanticMargins
        for minimumGap in sorted(set(grid)):
            filtered = [
                item for item in observations[channelName]
                if item["topGap"] is not None and item["topGap"] >= minimumGap
            ]
            threshold, channelReport = _chooseThreshold(filtered)
            for strategy in ("fixed", "recalibrated"):
                thresholds = dict(baselineThresholds)
                if strategy == "recalibrated":
                    thresholds[channelName] = threshold
                variants.append({
                    "variantID": f"{channelName}:{strategy}:{minimumGap:g}",
                    "thresholds": thresholds,
                    "channelMargins": {channelName: minimumGap},
                    "topOneOnly": False,
                    "channelCalibration": channelReport if strategy == "recalibrated" else None,
                })

    scoreCache = {}
    baselineResults = {}
    trials = []
    for variant in variants:
        thresholds = variant["thresholds"]
        results = []
        for case in calibrationCases:
            # 查询计划只由通道是否启用决定。重校准可能关闭 current，从而
            # 让同文本的 assisted 接管；缓存键必须区分这两种计划。
            key = (case["caseID"], *(
                thresholds[name] is not None for name in CHANNEL_NAMES[:2]
            ))
            if key not in scoreCache:
                scoreCache[key] = scoreCaseChannels(
                    case, semanticScorer=semanticScorer, thresholds=thresholds,
                )
            scores, rankingScores = scoreCache[key]
            scored = _scoreCaseResult(case, _evaluateSingleCase(
                case,
                "hybrid+hint",
                thresholds,
                channelScores=scores,
                semanticRankingScores=rankingScores,
                channelMargins=variant["channelMargins"],
                topOneOnly=variant["topOneOnly"],
            ))
            if variant["variantID"] == "absolute-only":
                baselineResults[case["caseID"]] = scored
            baseline = baselineResults[case["caseID"]]
            scored["lostRequiredIDs"] = sorted(
                set(baseline["hitRequiredIDs"]) - set(scored["hitRequiredIDs"])
            )
            scored["recoveredRequiredIDs"] = sorted(
                set(scored["hitRequiredIDs"]) - set(baseline["hitRequiredIDs"])
            )
            # 基线保留全池分数，方便追查多 required 原本丢在哪一层；其余
            # 网格点只保留通道判据，避免把相同证据复制数十次。均不含正文。
            if variant["variantID"] != "absolute-only":
                scored["diagnostics"] = {
                    name: scored["diagnostics"][name]
                    for name in ("marginEvidence", "topOneDroppedIDs")
                    if name in scored["diagnostics"]
                }
            results.append(scored)
        metrics = _marginStudyMetrics(results)
        metrics["lostRequiredCount"] = sum(len(result["lostRequiredIDs"]) for result in results)
        metrics["recoveredRequiredCount"] = sum(len(result["recoveredRequiredIDs"]) for result in results)
        metrics["subsets"] = {
            subset: _marginStudyMetrics([
                result for result in results if subset in result["subsets"]
            ])
            for subset in sorted({subset for result in results for subset in result["subsets"]})
        }
        trials.append({**variant, "metrics": metrics, "cases": results})

    channelEvidence = {}
    for channelName, items in observations.items():
        byCase = defaultdict(dict)
        for item in items:
            byCase[item["caseID"]][item["memoryID"]] = item
        evidence = []
        for caseID, byID in byCase.items():
            top = _topMarginEvidence({memoryID: item["score"] for memoryID, item in byID.items()})
            label = byID[top["topMemoryID"]]
            evidence.append({
                "caseID": caseID,
                **top,
                "topAcceptable": label["positive"],
                "topForbidden": label["forbidden"],
                "passesBaselineAbsolute": (
                    baselineThresholds[channelName] is not None
                    and top["topScore"] >= baselineThresholds[channelName]
                ),
            })
        channelEvidence[channelName] = evidence

    reviewStatuses = defaultdict(int)
    for case in calibrationCases:
        reviewStatuses[case.get("metadata", {}).get("reviewStatus", "unspecified")] += 1
    return {
        "schemaVersion": 1,
        "reportType": "top-margin-study",
        "status": "experimental",
        "productionEligible": False,
        "split": CALIBRATION_SPLIT,
        "caseCount": len(calibrationCases),
        "datasetSha256": _casesDigest(cases),
        "reviewStatusCounts": dict(reviewStatuses),
        "baselineCalibration": baselineCalibration,
        "semanticMargins": sorted(set(semanticMargins)),
        "lexicalMargins": sorted(set(lexicalMargins)),
        "channelEvidence": channelEvidence,
        "trials": trials,
        "note": (
            "仅为 calibration 样本内实验，不是盲测或上线批准；margin 使用"
            "base/词面原始前两名，缺第二名时弃权。各次只调整一个通道，"
            "通过后仍保留所有过绝对阈值的候选。top-one-control 仅用于量损。"
        ),
    }


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
            scorer.score(["索引预热"], [memory])
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
                scorer.score([queryText], memories)
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
        cachedMatrices = {}
        for representations in scorer._memoryCache.values():
            cachedMatrices[id(representations.base)] = representations.base
            cachedMatrices[id(representations.enhanced)] = representations.enhanced
        matrixBytes = sum(
            int(getattr(matrix, "nbytes", 0))
            for matrix in cachedMatrices.values()
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
            representations = encoder.encodeMemoryRepresentations(memory)
            encodedChunks += int(representations.base.shape[0])
            if representations.enhanced is not representations.base:
                encodedChunks += int(representations.enhanced.shape[0])
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
    """构造 fixture 校验、校准、评估、margin 研究和资源基准子命令。"""
    parser = argparse.ArgumentParser(description="LLM memory 离线评测")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validateParser = subparsers.add_parser(
        "validate",
        help="只校验 fixture，不加载模型",
    )
    validateParser.add_argument("--cases", required=True)
    validateParser.add_argument("--output")

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

    marginParser = subparsers.add_parser(
        "margin", help="仅用 calibration 研究绝对阈值 + top-margin，不批准上线",
    )
    marginParser.add_argument("--cases", required=True)
    marginParser.add_argument("--output", required=True)
    marginParser.add_argument("--model-dir", default=LLM_MEMORY_MODEL_DIR)
    marginParser.add_argument("--manifest", default=LLM_MEMORY_MODEL_MANIFEST_PATH)
    marginParser.add_argument("--semantic-margins", nargs="+", type=float, default=list(SEMANTIC_MARGIN_GRID))
    marginParser.add_argument("--lexical-margins", nargs="+", type=float, default=list(LEXICAL_MARGIN_GRID))

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
        if args.command == "validate":
            cases, datasetSha256 = loadEvaluationCases(args.cases)
            report = {
                "schemaVersion": 1,
                "caseCount": len(cases),
                "calibrationCaseCount": len(splitEvaluationCases(
                    cases,
                    CALIBRATION_SPLIT,
                )),
                "holdoutCaseCount": len(splitEvaluationCases(
                    cases,
                    HOLDOUT_SPLIT,
                )),
                "datasetSha256": datasetSha256,
            }
            _writeReport(report, args.output)
            return 0

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

        if args.command == "margin":
            # 研究报告不是阈值配置。即使用户给错路径，也不能覆盖正式
            # calibration、输入 fixture 或模型清单；在加载模型前检查。
            protectedPaths = (LLM_MEMORY_CALIBRATION_PATH, args.cases, args.manifest)
            if Path(args.output).resolve() in {
                Path(path).resolve() for path in protectedPaths
            }:
                raise EvaluationError("margin 报告不允许覆盖正式 calibration、fixture 或 manifest")
            cases, _ = loadEvaluationCases(args.cases)
            encoder, scorer = _createScorer(args.model_dir, args.manifest)
            report = studyTopMargins(
                cases,
                semanticScorer=scorer,
                manifestPath=args.manifest,
                semanticMargins=tuple(args.semantic_margins),
                lexicalMargins=tuple(args.lexical_margins),
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
