"""
utils/llm/memory/retrieval.py

记忆检索的编排层：读取候选、计算通道分数、选出最终进入 prompt 的条目。

配置项 memoryRetrievalMode 决定检索路径——legacy 为重构前的原逻辑
（每 scope 限量取池、priority 排序、取前 10 条），是当前生产默认；
hybrid 默认 local 后端（全量候选 → 词面/语义打分 → 阈值 → 名次融合），
另有显式配置的 llm 后端（三路 top32 并集 → 单次远程选 ID）。local
阈值完成校准前不放行情境记忆，llm 独立于该阈值门控。两种模式下
pinned 常驻记忆均走独立预算，不参与打分竞争。

错误处理原则：故障不打断回复，调用方取消正常传播。选择故障可保留已经
取得且通过最终复核的 pinned；数据库故障则返回空结果，diagnostics 记原因。
特别地，hybrid 内部故障不会退回 legacy 的选择逻辑——否则线上无法
区分新逻辑是否在实际工作。
"""

import asyncio
import json
import math
import re
import time
import unicodedata
import secrets
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from config import (
    LLM_MEMORY_CALIBRATION_PATH,
    LLM_MEMORY_CONTEXT_MAX_CHARS,
    LLM_MEMORY_FINALIZE_RESERVE_SECONDS,
    LLM_MEMORY_MAX_ACTIVE_RETRIEVALS,
    LLM_MEMORY_PINNED_MAX_CHARS,
    LLM_MEMORY_QUERY_HISTORY_LIMIT,
    LLM_MEMORY_QUERY_HISTORY_MAX_CHARS,
    LLM_MEMORY_QUERY_HISTORY_SECONDS,
    LLM_MEMORY_RETRIEVAL_TIMEOUT_SECONDS,
    LLM_MEMORY_RETRIEVE_PER_SCOPE,
    LLM_MEMORY_RETRIEVE_TOTAL,
    LLM_MEMORY_RRF_K,
    LLM_MEMORY_SELECTOR_BASE_URL,
    LLM_MEMORY_SELECTOR_API_KEY,
    LLM_MEMORY_SELECTOR_PROXY,
)

from utils.core.stateManager import getStateManager
from utils.llm.config import loadLLMConfig, getMemorySelectorSettings
from utils.llm.promptSafety import neutralizePromptDelimiters

from .database import (
    MEMORY_MODE_CONTEXTUAL,
    MEMORY_MODE_PINNED,
    MEMORY_SCOPE_RANK,
    getMemoryCandidates,
    getMemorySnapshots,
    selectLegacyMemoryCandidates,
)
from .encoder import loadModelManifest
from .lexical import scoreLexicalCandidates
from .selector import (
    buildSelectorCandidates,
    buildSelectorPayload,
    buildSelectorRequest,
    validateSelectorResponse,
    buildMessagesSelectorRequest,
    validateMessagesSelectorResponse,
)
from .types import (
    MemoryQuery,
    MemoryRetrievalResult,
    buildMemoryStateFingerprint,
)


# calibration 不只绑定 BM25 的数值实现，也绑定词面分数的准入语义；修改
# tokenizer、权重、归一化或从绝对 threshold 改为 top-margin 时必须 bump。
# 仅调整脱敏 fixture 时保留版本，改由 calibration 的 dataset hash 失效旧结果。
LEXICAL_VERSION = "memory-bm25-v1"
# 当前占用检索容量的请求数（与 _RetrievalLease 配合的全局记账）
_activeRetrievals = 0
_CHANNEL_NAMES = ("semanticCurrent", "semanticAssisted", "lexical")




def _recordDegradedReason(diagnostics: dict, reason: str | None) -> None:
    """记录检索降级原因且不让后续通道覆盖先前原因。

    ``degradedReason`` 保留首个原因，兼容原有 status/日志消费者；
    ``degradedReasons`` 收集同一次请求的全部原因，避免 lexical、semantic
    和 calibration 同时异常时只剩最后一次赋值，导致排障方向错误。
    原因码不包含查询正文、memory 正文或 hint。
    """
    if not reason:
        return
    reasons = diagnostics.setdefault("degradedReasons", [])
    if reason not in reasons:
        reasons.append(reason)
    if diagnostics.get("degradedReason") is None:
        diagnostics["degradedReason"] = reason


def _setChannelDiagnostic(
    diagnostics: dict,
    channelName: str,
    *,
    status: str | None = None,
    scoreCount: int | None = None,
    reason: str | None = None,
) -> None:
    """更新单个通道的结构化状态，不记录任何用户内容。"""
    channel = diagnostics.setdefault("channelDiagnostics", {}).setdefault(
        channelName,
        {"status": "notStarted", "scoreCount": 0},
    )
    if status is not None:
        channel["status"] = status
    if scoreCount is not None:
        channel["scoreCount"] = int(scoreCount)
    if reason is not None:
        channel["reason"] = reason




class _RetrievalLease:
    """
    一次检索的并发名额记账：检索开始时占用名额，结束时归还。

    同时最多 4 个检索在执行（LLM_MEMORY_MAX_ACTIVE_RETRIEVALS），超出的
    请求直接返回空结果。超时场景需要特殊处理：调用方等待超时返回后，
    已提交到线程池的评分任务仍在执行——名额必须等该任务真正结束才能
    归还（deferUntil），否则连续超时会绕过并发上限，实际同时执行的
    任务数超出限制。
    """

    def __init__(self):
        """同时跟踪调用方和仍在后台运行的任务，全部结束才归还容量。"""
        self._pending = set()
        self._releaseRequested = False
        self._released = False
        self.completion = asyncio.get_running_loop().create_future()


    def deferUntil(self, task: asyncio.Future) -> None:
        """为任务注册完成回调，由回调在任务结束后归还名额。"""
        if self._released:
            return
        self._pending.add(task)
        task.add_done_callback(self._releaseAfterTask)


    def _releaseAfterTask(self, task: asyncio.Future) -> None:
        """后台任务完成不代表远程选择已结束，等待请求方也释放名额。"""
        try:
            task.exception()
        except (asyncio.CancelledError, Exception):
            pass
        self._pending.discard(task)
        if self._releaseRequested and not self._pending:
            self._releaseNow()


    def _releaseNow(self) -> None:
        """只归还一次全局名额。"""
        global _activeRetrievals
        if self._released:
            return
        self._released = True
        _activeRetrievals = max(0, _activeRetrievals - 1)
        self.completion.set_result(None)


    def release(self) -> None:
        """检索正常结束时立即归还名额（已注册回调延期归还的除外）。"""
        self._releaseRequested = True
        if not self._pending:
            self._releaseNow()




def _normalizeText(text) -> str:
    return unicodedata.normalize("NFKC", str(text or "")).strip()


def _historyTimestamp(value):
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None




def _recentHistory(query: MemoryQuery, *, now: datetime) -> list[dict]:
    """
    从聊天记录中筛选可作为语义背景的近期消息。

    约束：只从调用方已经加载的近期历史中筛选，最多 20 条、最近 30
    分钟内且总计 600 字符；reaction 与本轮消息重复的文本排除。筛选
    结果只拼入「辅助语义查询」（见 buildQueryTexts），不进入词面查询
    ——否则较早对话中的字面词会使不相关记忆凭关键词命中。调用方的
    历史窗口必须不小于此处的候选上限；当前由 30 条共享快照提供，
    因而 memory 不会依据模型看不到的更早消息召回记忆。
    """
    # 与本轮消息重复的历史排除：turns 中已计算过一次，重复出现只放大词面误召回。
    duplicateTexts = {
        normalized
        for turn in query.turns
        for normalized in (
            _normalizeText(turn.currentText),
            _normalizeText(turn.replyText),
        )
        if normalized
    }

    eligible = []
    for message in query.history:
        if message.get("direction") == "reaction":
            continue
        content = _normalizeText(message.get("content"))
        timestamp = _historyTimestamp(message.get("timestamp"))
        if not content or timestamp is None or content in duplicateTexts:
            continue
        try:
            ageSeconds = (now - timestamp).total_seconds()
        except (TypeError, ValueError):
            continue
        if ageSeconds < 0 or ageSeconds > LLM_MEMORY_QUERY_HISTORY_SECONDS:
            continue
        eligible.append({**message, "content": content, "timestamp": timestamp})

    selectedReversed = []
    remainingChars = LLM_MEMORY_QUERY_HISTORY_MAX_CHARS
    for message in reversed(eligible[-LLM_MEMORY_QUERY_HISTORY_LIMIT:]):
        if remainingChars <= 0:
            break
        content = message["content"]
        if len(content) > remainingChars:
            content = content[-remainingChars:]
        selectedReversed.append({**message, "content": content})
        remainingChars -= len(content)
    return list(reversed(selectedReversed))




def buildQueryTexts(
    query: MemoryQuery,
    *,
    now: datetime | None = None,
) -> tuple[str, str, str]:
    """将 MemoryQuery 组装为三段查询文本，分别供三个打分通道使用。

    返回 (当前语义查询, 辅助语义查询, 词面查询)：

    - 当前语义查询 = 本轮全部消息正文 + :fb 反馈，代表「用户当前
      在表达什么」，是最重要的语义证据；
    - 辅助语义查询 = 当前语义查询 + 引用消息 + 近期聊天记录，上下文
      更完整，适用于「消息本身很短、需结合背景理解」的场景；
    - 词面查询 = 本轮消息正文 + 引用消息（不含历史），供 BM25 做
      关键词匹配。

    当前消息与反馈均为空时，三段全部返回空串（本轮没有可检索的输入）。
    """
    now = now or datetime.now()
    currentParts = [
        _normalizeText(turn.currentText)
        for turn in query.turns
        if _normalizeText(turn.currentText)
    ]
    feedbackText = _normalizeText(query.feedbackText)
    if feedbackText:
        currentParts.append(feedbackText)
    currentText = "\n".join(currentParts)
    if not currentText:
        return "", "", ""

    replyParts = [
        _normalizeText(turn.replyText)
        for turn in query.turns
        if _normalizeText(turn.replyText)
    ]
    lexicalText = "\n".join([*currentParts, *replyParts])

    assistedParts = [currentText]
    if replyParts:
        assistedParts.append("引用：\n" + "\n".join(replyParts))
    history = _recentHistory(query, now=now)
    if history:
        historyLines = [
            f"{message.get('sender', '')}: {message['content']}".strip()
            for message in history
        ]
        assistedParts.append("近期对话：\n" + "\n".join(historyLines))
    assistedText = "\n\n".join(assistedParts)
    return currentText, assistedText, lexicalText


def buildSemanticQueryPlan(
    currentText: str,
    assistedText: str,
    thresholds: dict[str, float | None],
) -> list[tuple[str, str]]:
    """根据已启用的阈值决定本轮实际使用哪些语义通道。

    ``assistedText`` 在没有引用或近期历史时会与 ``currentText`` 完全
    相同。此时若当前通道已启用，只保留 current 这一份 canonical 证据；
    只有 current 被关闭时，才允许 assisted 接管同一文本。这样同一条
    证据不会因为挂上两个通道名称而获得两次 RRF 贡献。查询文本不同
    时，两个启用的通道都可以参加评分。
    """
    plan = []
    seenTexts = set()
    for channelName, textValue in (
        ("semanticCurrent", currentText),
        ("semanticAssisted", assistedText),
    ):
        if not textValue or thresholds.get(channelName) is None:
            continue
        if (
            channelName == "semanticAssisted"
            and textValue == currentText
            and thresholds.get("semanticCurrent") is not None
        ):
            continue
        if textValue in seenTexts:
            continue
        seenTexts.add(textValue)
        plan.append((channelName, textValue))
    return plan


def loadCalibratedThresholds(
    calibrationPath: str | Path = LLM_MEMORY_CALIBRATION_PATH,
    *,
    manifestPath: str | Path | None = None,
) -> tuple[dict[str, float | None], str | None]:
    """读取各通道的命中分数阈值，并校验该阈值文件是否仍然有效。

    阈值定义在 retrievalCalibration.json，由离线评测（evaluateMemory
    calibrate）基于标注数据计算、经人工批准后写入。每份阈值记录其
    依据的模型、编码版本和标注数据；本函数逐项与当前环境比对，
    任一项不匹配（更换模型、数据重标、未经批准的 candidate）即视为
    阈值不可信——三个通道全部禁用，hybrid 检索退化为仅保留 pinned
    常驻记忆。

    返回 (阈值表, None) 或 (全 None, 原因码)。
    """
    try:
        calibration = json.loads(Path(calibrationPath).read_text(encoding="utf-8"))
        if not isinstance(calibration, dict):
            raise ValueError("calibrationRootInvalid")
        manifest = loadModelManifest(manifestPath) if manifestPath else loadModelManifest()
        if calibration.get("schemaVersion") != 2:
            raise ValueError("calibrationSchemaMismatch")
        # 只有显式 approved 才能影响线上准入；缺字段与未知状态都 fail closed。
        if calibration.get("status") == "candidate":
            raise ValueError("calibrationCandidate")
        if calibration.get("status") != "approved":
            raise ValueError("calibrationStatusInvalid")
        if calibration.get("modelRevision") != manifest["revision"]:
            raise ValueError("calibrationModelMismatch")
        if calibration.get("encodingVersion") != manifest["encodingVersion"]:
            raise ValueError("calibrationEncodingMismatch")
        if calibration.get("lexicalVersion") != LEXICAL_VERSION:
            raise ValueError("calibrationLexicalMismatch")
        if (
            calibration.get("semanticAdmissionRepresentation") != "base"
            or calibration.get("semanticRankingRepresentation") != "enhanced"
        ):
            raise ValueError("calibrationRepresentationMismatch")
        datasetHash = calibration.get("datasetSha256")
        if not datasetHash:
            raise ValueError("calibrationDatasetMissing")
        if not isinstance(datasetHash, str) or not re.fullmatch(
            r"[0-9a-f]{64}", datasetHash,
        ):
            raise ValueError("calibrationDatasetInvalid")

        rawThresholds = calibration.get("thresholds")
        if not isinstance(rawThresholds, dict):
            raise ValueError("calibrationThresholdsInvalid")
        thresholds = {}
        for name in ("semanticCurrent", "semanticAssisted", "lexical"):
            value = rawThresholds.get(name)
            if value is None:
                thresholds[name] = None
            elif (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError("calibrationThresholdsInvalid")
            else:
                thresholds[name] = float(value)
        return thresholds, None
    except (
        OSError,
        RuntimeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ) as e:
        # 原因码分两档：本函数自产的 ValueError 消息就是稳定的英文码
        # （calibrationXxx），直接采用以保留细粒度；外部异常（OSError/
        # JSONDecodeError/encoder 抛出的中文错误）消息语种与格式不稳定，
        # 退用异常类名，保证原因码始终是可断言的英文标识。
        reasonCode = (
            type(e).__name__
            if isinstance(e, json.JSONDecodeError)
            else str(e) if isinstance(e, ValueError)
            else type(e).__name__
        )
        return {
            "semanticCurrent": None,
            "semanticAssisted": None,
            "lexical": None,
        }, reasonCode


def _rankQualified(
    scores: dict[int, float],
    threshold: float | None,
    *,
    rankingScores: dict[int, float] | None = None,
) -> dict[int, int]:
    """先按准入分过阈值，再按独立排序分排名（同分并列）。

    名次（而非原始分数）作为 RRF 融合的输入——三通道分数量纲不同
    （余弦相似度 vs BM25 分），不可直接相加，统一转换为名次参与计算。
    rankingScores 仅能改变已经过 scores 阈值的 ID 顺序；缺失或非法的排序
    分回退到准入分，绝不能据此新增候选。
    """
    if threshold is None:
        return {}
    ordered = []
    for memoryID, admissionScore in scores.items():
        if (
            not isinstance(admissionScore, (int, float))
            or isinstance(admissionScore, bool)
            or not math.isfinite(admissionScore)
            or admissionScore < threshold
        ):
            continue

        rankingScore = (
            rankingScores.get(memoryID)
            if rankingScores is not None
            else admissionScore
        )
        if (
            not isinstance(rankingScore, (int, float))
            or isinstance(rankingScore, bool)
            or not math.isfinite(rankingScore)
        ):
            rankingScore = admissionScore
        ordered.append((memoryID, float(rankingScore)))
    ordered.sort(key=lambda item: (-item[1], item[0]))
    ranks = {}
    previousScore = None
    currentRank = 0
    for position, (memoryID, score) in enumerate(ordered, start=1):
        if previousScore is None or score != previousScore:
            currentRank = position
            previousScore = score
        ranks[memoryID] = currentRank
    return ranks


def _finiteScore(value) -> float | None:
    """将可用于离线诊断的有限数值规范化为 float。"""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    score = float(value)
    return score if math.isfinite(score) else None


def _buildSelectionEvidence(
    candidateIDs: list[int],
    channelScores: dict[str, dict[int, float]],
    semanticRankingScores: dict[str, dict[int, float]] | None,
    thresholds: dict[str, float | None],
    channelRanks: dict[str, dict[int, int]],
    fusedScores: dict[int, float],
    selectedIDs: list[int],
) -> tuple[dict, list[dict]]:
    """生成不含正文和 hint 的离线证据，解释准入与排序两个阶段。

    语义阈值和 thresholdMargin 始终基于 base；enhanced 只体现在排序分、
    排名和 gap 中。证据量随候选池线性增长，因此只能由离线评测显式开启。
    """
    useEnhancedRanking = semanticRankingScores is not None
    semanticRankingScores = semanticRankingScores or {}
    channelEvidence = {}
    admissionScoresByChannel = {}
    effectiveRankingScoresByChannel = {}
    admissionRanksByChannel = {}
    rawRanksByChannel = {}
    for channelName in _CHANNEL_NAMES:
        admissionScores = {
            memoryID: score
            for memoryID in candidateIDs
            if (score := _finiteScore(
                channelScores.get(channelName, {}).get(memoryID)
            )) is not None
        }
        isSemantic = channelName in _CHANNEL_NAMES[:2]
        rawRankingScores = (
            semanticRankingScores.get(channelName, {})
            if isSemantic and useEnhancedRanking
            else admissionScores
        )
        effectiveRankingScores = {}
        for memoryID, admissionScore in admissionScores.items():
            rankingScore = _finiteScore(rawRankingScores.get(memoryID))
            effectiveRankingScores[memoryID] = (
                rankingScore
                if rankingScore is not None
                else admissionScore
            )
        admissionScoresByChannel[channelName] = admissionScores
        effectiveRankingScoresByChannel[channelName] = effectiveRankingScores
        admissionRanksByChannel[channelName] = _rankQualified(
            admissionScores,
            float("-inf"),
        )
        rawRanksByChannel[channelName] = _rankQualified(
            admissionScores,
            float("-inf"),
            rankingScores=effectiveRankingScores,
        )
        ordered = sorted(
            effectiveRankingScores.items(),
            key=lambda item: (-item[1], item[0]),
        )
        admissionOrdered = sorted(
            admissionScores.items(),
            key=lambda item: (-item[1], item[0]),
        )
        top = ordered[0] if ordered else (None, None)
        second = ordered[1] if len(ordered) > 1 else (None, None)
        admissionTop = (
            admissionOrdered[0]
            if admissionOrdered
            else (None, None)
        )
        channelEvidence[channelName] = {
            "threshold": _finiteScore(thresholds.get(channelName)),
            "admissionRepresentation": "base" if isSemantic else "lexical",
            "rankingRepresentation": (
                "enhanced"
                if isSemantic and useEnhancedRanking
                else "base" if isSemantic else "lexical"
            ),
            "topAdmissionMemoryID": admissionTop[0],
            "topAdmissionScore": admissionTop[1],
            "topMemoryID": top[0],
            "topScore": top[1],
            "secondMemoryID": second[0],
            "secondScore": second[1],
            "topSecondGap": (
                top[1] - second[1]
                if top[1] is not None and second[1] is not None
                else None
            ),
        }

    selectionPositions = {
        memoryID: position
        for position, memoryID in enumerate(selectedIDs, start=1)
    }
    candidateEvidence = []
    for memoryID in candidateIDs:
        qualifiedChannels = [
            channelName for channelName in _CHANNEL_NAMES
            if memoryID in channelRanks[channelName]
        ]
        contributions = {
            channelName: 1.0 / (
                LLM_MEMORY_RRF_K + channelRanks[channelName][memoryID]
            )
            for channelName in qualifiedChannels
        }
        channels = {}
        for channelName in _CHANNEL_NAMES:
            admissionScore = admissionScoresByChannel[channelName].get(memoryID)
            rankingScore = effectiveRankingScoresByChannel[channelName].get(memoryID)
            threshold = channelEvidence[channelName]["threshold"]
            topScore = channelEvidence[channelName]["topScore"]
            isSemantic = channelName in _CHANNEL_NAMES[:2]
            rawEnhancedScore = _finiteScore(
                semanticRankingScores.get(channelName, {}).get(memoryID)
            )
            channels[channelName] = {
                # score 保留为 base/lexical 准入分，兼容既有报告消费者。
                "score": admissionScore,
                "admissionScore": admissionScore,
                "rankingScore": rankingScore,
                "admissionRank": admissionRanksByChannel[channelName].get(memoryID),
                "rank": rawRanksByChannel[channelName].get(memoryID),
                "qualified": memoryID in channelRanks[channelName],
                "qualifiedRank": channelRanks[channelName].get(memoryID),
                "thresholdMargin": (
                    admissionScore - threshold
                    if admissionScore is not None and threshold is not None
                    else None
                ),
                "gapFromTop": (
                    topScore - rankingScore
                    if topScore is not None and rankingScore is not None
                    else None
                ),
                "rankingBlockedByAdmission": (
                    isSemantic
                    and useEnhancedRanking
                    and rawEnhancedScore is not None
                    and memoryID not in channelRanks[channelName]
                ),
            }
        candidateEvidence.append({
            "memoryID": memoryID,
            "fusedQualified": memoryID in fusedScores,
            "selectionPosition": selectionPositions.get(memoryID),
            "rrfScore": fusedScores.get(memoryID),
            "supportCount": len(qualifiedChannels),
            "qualifiedChannels": qualifiedChannels,
            "rrfContributions": contributions,
            "channels": channels,
        })
    return channelEvidence, candidateEvidence


def _timestampSortValue(value) -> float:
    timestamp = _historyTimestamp(value)
    if timestamp is None:
        return float("-inf")
    try:
        return timestamp.timestamp()
    except (OSError, OverflowError, ValueError):
        return float("-inf")


def selectContextualCandidates(
    candidates: list[dict],
    channelScores: dict[str, dict[int, float]],
    thresholds: dict[str, float | None],
    *,
    semanticRankingScores: dict[str, dict[int, float]] | None = None,
    includeEvidence: bool = False,
) -> tuple[list[dict], dict]:
    """各通道先独立准入，再经名次融合生成最终候选列表。

    channelScores 中的两个语义分数都来自不含 hint 的 base 表示，是唯一
    准入依据；semanticRankingScores 可来自含 hint 的 enhanced 表示，但
    只重排已经通过相应 base 阈值的 ID。词面通道继续用自身分数同时准入
    和排序。记忆在任一通道过关即入选，多通道同时过关则通过 RRF 累加
    1/(K+名次)，K=60。

    排序兜底键依次为 融合分 > priority > scope 专属度 > 更新时间 > ID，
    保证相同输入始终得到相同顺序（评测可复现的前提）。离线评测可用
    includeEvidence=True 获取逐候选数值证据；默认关闭，避免线上按候选池
    规模扩张 diagnostics。
    """
    channelRanks = {}
    for name in _CHANNEL_NAMES:
        rankingScores = (
            semanticRankingScores.get(name, {})
            if semanticRankingScores is not None and name in _CHANNEL_NAMES[:2]
            else None
        )
        channelRanks[name] = _rankQualified(
            channelScores.get(name, {}),
            thresholds.get(name),
            rankingScores=rankingScores,
        )
    fusedScores = {}
    for ranks in channelRanks.values():
        for memoryID, rank in ranks.items():
            fusedScores[memoryID] = fusedScores.get(memoryID, 0.0) + (
                1.0 / (LLM_MEMORY_RRF_K + rank)
            )

    byID = {int(memory["id"]): memory for memory in candidates}
    selected = [byID[memoryID] for memoryID in fusedScores if memoryID in byID]
    selected.sort(
        key=lambda memory: (
            fusedScores[int(memory["id"])],
            int(memory.get("priority", 0)),
            MEMORY_SCOPE_RANK.get(memory.get("scope_type"), 0),
            _timestampSortValue(memory.get("updated_at")),
            int(memory.get("id", 0)),
        ),
        reverse=True,
    )
    diagnostics = {
        "semanticCurrentQualified": len(channelRanks["semanticCurrent"]),
        "semanticAssistedQualified": len(channelRanks["semanticAssisted"]),
        "lexicalQualified": len(channelRanks["lexical"]),
        "fusedQualified": len(selected),
    }
    if includeEvidence:
        channelEvidence, candidateEvidence = _buildSelectionEvidence(
            list(byID),
            channelScores,
            semanticRankingScores,
            thresholds,
            channelRanks,
            fusedScores,
            [int(memory["id"]) for memory in selected],
        )
        diagnostics["channelEvidence"] = channelEvidence
        diagnostics["candidateEvidence"] = candidateEvidence
    return selected, diagnostics


def deduplicateMemoryCandidates(memories: list[dict]) -> list[dict]:
    """按 scope 与规范化正文去重，并保留输入中排序更靠前的记录。

    ID 与 mode 不参与判重：同一 scope 的相同事实可能在更新期间以不同
    ID 或 pinned/contextual 形态并存，最终 prompt 只能保留一份。该纯函数
    同时供线上检索与离线评测使用，避免评测指标统计线上不会注入的重复项。
    """
    result = []
    seen = set()
    for memory in memories:
        key = (
            memory.get("scope_type"),
            memory.get("scope_id"),
            _normalizeText(memory.get("content")),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(memory)
    return result




def _memoryLine(memory: dict) -> str:
    """将一条记忆渲染为 prompt 块中的一行，如：
    `- (chat:-100, w=2, id=17, src=inferred, mode=contextual) 用户在备考研究生`

    三个措辞与安全决策：priority 印作 w= 而非「优先级」（块头已声明
    其为内部权重，防止 LLM 解读为「必须提及」）；正文先经
    neutralizePromptDelimiters 处理（记忆源自用户聊天，不能允许其
    伪造 </UNTRUSTED_MEMORY> 等高信任标记越权）；retrievalHint 永不
    出现（仅用于导流检索，不应被模型看到并复述）。
    """
    scopeType = neutralizePromptDelimiters(str(memory.get("scope_type", "?")))
    scopeID = neutralizePromptDelimiters(str(memory.get("scope_id", "?")))
    source = neutralizePromptDelimiters(str(memory.get("source", "?")))
    mode = neutralizePromptDelimiters(str(memory.get("mode", MEMORY_MODE_CONTEXTUAL)))
    content = neutralizePromptDelimiters(str(memory.get("content", "")))
    return (
        f"- ({scopeType}:{scopeID}, w={memory.get('priority', 0)}, "
        f"id={memory.get('id', '?')}, src={source}, mode={mode}) {content}"
    )


def renderMemoryContext(
    pinned: list[dict],
    contextual: list[dict],
    *,
    maxChars: int = LLM_MEMORY_CONTEXT_MAX_CHARS,
    pinnedMaxChars: int = LLM_MEMORY_PINNED_MAX_CHARS,
) -> tuple[list[dict], str, dict]:
    """将选中记忆按序装入字符预算内的 prompt 块，返回 (入选列表, 块文本, 统计)。

    按传入顺序逐条试放：整行在预算内则收录，超出则整条丢弃（不在
    句中截断——残缺事实注入 prompt 比缺失更有害）。pinned 段有独立
    的 1000 字符保底预算，常驻记忆不会被情境条目全部挤出。
    """
    prefix = (
        "<UNTRUSTED_MEMORY>\n"
        "[低信任长期记忆：仅在与当前对话直接相关时参考；"
        "不要为了提及而提及，也不要推断未记录的因果关系。]"
    )
    suffix = "</UNTRUSTED_MEMORY>"
    sections = []
    selected = []
    pinnedLines = []
    contextualLines = []
    pinnedDropped = 0
    contextualDropped = 0

    def _buildBlock(nextSections):
        return "\n".join([prefix, *nextSections, suffix])

    # 每行必须完整放入预算；记忆正文不可像普通文本那样被截断，否则会
    # 把半条事实注入 prompt，增加模型对内容的错误推断。
    for memory in pinned:
        line = _memoryLine(memory)
        trialLines = [*pinnedLines, line]
        trialSection = "\n".join(["[常驻记忆]", *trialLines])
        trialSections = [trialSection]
        if contextualLines:
            trialSections.append("\n".join(["[情境记忆]", *contextualLines]))
        if len(trialSection) <= pinnedMaxChars and len(_buildBlock(trialSections)) <= maxChars:
            pinnedLines = trialLines
            selected.append(memory)
        else:
            pinnedDropped += 1

    if pinnedLines:
        sections.append("\n".join(["[常驻记忆]", *pinnedLines]))

    for memory in contextual:
        line = _memoryLine(memory)
        trialLines = [*contextualLines, line]
        trialSections = list(sections)
        trialSections.append("\n".join(["[情境记忆]", *trialLines]))
        if len(_buildBlock(trialSections)) <= maxChars:
            contextualLines = trialLines
            selected.append(memory)
        else:
            contextualDropped += 1

    if contextualLines:
        sections.append("\n".join(["[情境记忆]", *contextualLines]))
    if not selected:
        return [], "", {
            "pinnedBudgetDropped": pinnedDropped,
            "contextualBudgetDropped": contextualDropped,
            "contextChars": 0,
        }

    contextBlock = _buildBlock(sections)
    if len(contextBlock) > maxChars:
        raise RuntimeError("memory context 超过字符预算")
    return selected, contextBlock, {
        "pinnedBudgetDropped": pinnedDropped,
        "contextualBudgetDropped": contextualDropped,
        "contextChars": len(contextBlock),
    }


def sortPinnedMemories(memories: list[dict]) -> list[dict]:
    """按 priority、scope 专属度、更新时间和 ID 稳定排序 pinned。"""
    return sorted(
        memories,
        key=lambda memory: (
            int(memory.get("priority", 0)),
            MEMORY_SCOPE_RANK.get(memory.get("scope_type"), 0),
            _timestampSortValue(memory.get("updated_at")),
            int(memory.get("id", 0)),
        ),
        reverse=True,
    )


_sortPinned = sortPinnedMemories


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


async def _withDeadline(
    awaitable,
    deadline: float,
    *,
    lease: _RetrievalLease,
):
    """在统一 deadline 内等待任务，并在无法取消时延后释放检索容量。

    to_thread 提交的任务超时后仍在执行；此时将容量释放注册到任务
    完成回调上（lease.deferUntil），并发上限才不会被连续超时穿透。
    """
    remaining = _remaining(deadline)
    if remaining <= 0:
        if hasattr(awaitable, "close"):
            awaitable.close()
        raise asyncio.TimeoutError

    task = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
    except BaseException:
        if not task.done():
            lease.deferUntil(task)
        raise


async def _validateSelected(
    selected: list[dict],
    *,
    deadline: float,
    lease: _RetrievalLease,
) -> set[int]:
    """选中之后、注入 prompt 之前重读数据库，剔除窗口期内变更的条目。

    评分、排序到实际使用之间存在等待，LLM 选择还可能额外耗时 30 秒，期间 ops 可能
    修改、删除或禁用某条刚被选中的记忆。为每条重算状态指纹并与
    选中时比对，不一致者丢弃，返回仍然有效的 ID 集合。
    """
    if not selected:
        return set()
    snapshots = await _withDeadline(
        getMemorySnapshots(memory["id"] for memory in selected),
        deadline,
        lease=lease,
    )
    expectedStates = {
        int(memory["id"]): buildMemoryStateFingerprint(memory)
        for memory in selected
    }
    return {
        int(snapshot["id"])
        for snapshot in snapshots
        if snapshot.get("enabled")
        and expectedStates.get(int(snapshot["id"])) == buildMemoryStateFingerprint(snapshot)
    }


def _selectorQuery(query: MemoryQuery, *, now: datetime) -> dict:
    """只将当前消息与已裁剪历史交给选择器，移除聊天和数据库身份字段。"""
    return {
        "turns": [{
            "currentText": _normalizeText(turn.currentText),
            "replyText": _normalizeText(turn.replyText),
            "currentSender": str(turn.currentSender),
            "replySender": str(turn.replySender),
        } for turn in query.turns],
        "feedbackText": _normalizeText(query.feedbackText),
        "history": [{
            "content": message["content"],
            "sender": str(message.get("sender", "")),
            "direction": str(message.get("direction", "")),
            "timestamp": message["timestamp"].isoformat(),
        } for message in _recentHistory(query, now=now)],
    }


async def _selectWithLLM(
    candidates: list[dict], channelScores: dict, query: MemoryQuery,
    *, now: datetime, settings: dict, diagnostics: dict, lease: _RetrievalLease, owner,
) -> list[dict]:
    """只消费经过协议校验的 primary ID；错误交给编排层保留可复核 pinned。"""
    # 延迟导入避免 client 包的主生成入口与 contextBuilder/retrieval 构成循环。
    from utils.llm.client.memorySelection import requestMemorySelection, MemorySelectionError
    from .selector import SelectorProtocolError

    selectionStarted = time.monotonic()
    if getStateManager().getShutdownEvent().is_set() or not owner.selectorAccepting():
        raise MemorySelectionError("selectorStopping")
    pool = buildSelectorCandidates(candidates, channelScores)
    diagnostics["selectorCandidateCount"] = len(pool)
    if not pool:
        diagnostics["selectorStatus"] = "emptyPool"
        return []
    payload, handles = buildSelectorPayload(_selectorQuery(query, now=now), pool, queryNow=now.isoformat())
    marker = secrets.token_hex(6)
    protocol = settings["protocol"]
    diagnostics["selectorProtocol"] = protocol
    diagnostics["selectorEffortApplied"] = settings["effort"] if protocol == "responses" else None
    if protocol == "messages":
        body = buildMessagesSelectorRequest(payload, model=settings["model"], marker=marker)
    else:
        body = buildSelectorRequest(payload, model=settings["model"], effort=settings["effort"], marker=marker)
    lifecycle = {}
    try:
        # 请求组装同属选择预算；本地准备耗尽期限时不能再获得完整网络等待时间。
        remaining = settings["timeoutSeconds"] - (time.monotonic() - selectionStarted)
        if remaining <= 0:
            raise MemorySelectionError("selectorTimeout")
        response = await requestMemorySelection(
            body, baseURL=LLM_MEMORY_SELECTOR_BASE_URL, apiKey=LLM_MEMORY_SELECTOR_API_KEY,
            proxy=LLM_MEMORY_SELECTOR_PROXY, timeoutSeconds=remaining,
            protocol=protocol, lease=lease, owner=owner, lifecycle=lifecycle,
        )
        validator = validateMessagesSelectorResponse if protocol == "messages" else validateSelectorResponse
        selected = validator(response, payload, model=settings["model"], marker=marker)
        # 同步解析不能被asyncio取消打断，返回前再次核对完整选择耗时。
        if time.monotonic() - selectionStarted >= settings["timeoutSeconds"]:
            raise MemorySelectionError("selectorTimeout")
    except (MemorySelectionError, SelectorProtocolError) as exc:
        # 两个专用类型仅允许静态原因码；未知异常仍由外层统一隐藏。
        diagnostics["selectorFailure"] = str(exc)
        raise
    finally:
        # 后台清理可以继续更新自己的记录，但不能追改已经返回的diagnostics。
        diagnostics["selectorLifecycle"] = deepcopy(lifecycle)
    diagnostics["selectorStatus"] = "ready"
    diagnostics["selectorUsage"] = selected["usage"]
    if protocol == "messages":
        diagnostics["selectorTextFormat"] = selected["textFormat"]
        diagnostics["selectorUsageAccounting"] = selected["usageAccounting"]
    diagnostics["selectorOptionalIgnored"] = len(selected["optionalOrder"])
    # 返回原快照，模型不能注入改写后的正文或扩大候选范围。
    return [handles[handle] for handle in selected["primaryOrder"]]


async def retrieveMemoryContext(
    *,
    chatID,
    query: MemoryQuery,
    userID=None,
    sessionID=None,
    llmConfig: dict | None = None,
    legacyLimits: tuple[int, int] | None = None,
) -> MemoryRetrievalResult:
    """检索入口：contextBuilder 每次组装上下文时调用，返回成品记忆块。

    按配置走两条路径：legacy（重构前的原行为——每 scope 限量取池、
    按 priority 排序截前 10 条）或 hybrid（local 阈值选择 / llm 远程选 ID）。
    legacy/local 总预算 2 秒；llm 为本地 2 秒、远程最多 30 秒、收尾 0.1 秒。
    同时最多 4 个检索，无排队。选择失败仅保留可复核的 pinned，数据库
    故障返回空；外部取消正常传播，不将其伪装成成功降级。

    收尾分两遍渲染：先按候选快照渲染一遍确定入选集合，再回数据库
    复核剔除窗口期变更的条目，最后用幸存集合重新渲染——保证
    contextBlock 的每一行与 items 列表完全一致，不出现块内有、
    列表内无的记忆。
    """
    global _activeRetrievals

    started = time.monotonic()
    diagnostics = {
        "mode": "legacy",
        "candidateCount": 0,
        "contextualCandidateCount": 0,
        "pinnedCandidateCount": 0,
        "degradedReason": None,
        "degradedReasons": [],
        "channelDiagnostics": {
            channelName: {"status": "notStarted", "scoreCount": 0}
            for channelName in _CHANNEL_NAMES
        },
        "semanticCache": None,
    }
    if getStateManager().getShutdownEvent().is_set():
        _recordDegradedReason(diagnostics, "retrievalStopping")
        return MemoryRetrievalResult(diagnostics=diagnostics)
    # 并发闸门：全局同时最多 LLM_MEMORY_MAX_ACTIVE_RETRIEVALS 个检索，
    # 超出的直接空手返回——检索是回复生成的旁路，排队等待不如缺席。
    if _activeRetrievals >= LLM_MEMORY_MAX_ACTIVE_RETRIEVALS:
        _recordDegradedReason(diagnostics, "retrievalCapacity")
        return MemoryRetrievalResult(diagnostics=diagnostics)

    _activeRetrievals += 1
    lease = _RetrievalLease()
    selectorOwner = None
    deadline = started + LLM_MEMORY_RETRIEVAL_TIMEOUT_SECONDS
    # 此处把 deadline 分为两层：
    # 一层用来给 selectionDeadline 管候选读取与三通道打分，它们占了预算的大头，
    # 再挤出 LLM_MEMORY_FINALIZE_RESERVE_SECONDS 秒（默认 0.1）给另一层，
    # 完整 deadline 留给末尾的数据库复核与重渲染——通道评分把时间耗尽时
    # 用来复核和重渲染，而不因第一层占用全部预算时间导致整体超时

    # 此处的 min() 防止保留量被配置得比总预算一半还大，挤占打分阶段。
    finalizeReserve = min(
        LLM_MEMORY_FINALIZE_RESERVE_SECONDS,
        LLM_MEMORY_RETRIEVAL_TIMEOUT_SECONDS / 2,
    )
    selectionDeadline = deadline - finalizeReserve
    try:
        # 模式判定用请求开始时的快照（llmConfig 由调用方传入或此处现读），
        # 一次检索中途切模式不会导致半程混用两套逻辑。
        configSnapshot = deepcopy(llmConfig if llmConfig is not None else loadLLMConfig())
        mode = configSnapshot.get("memoryRetrievalMode", "legacy")
        if mode not in {"legacy", "hybrid"}:
            mode = "legacy"
        diagnostics["mode"] = mode
        selectorSettings = None
        backend = configSnapshot.get("memoryHybridSelector", "local")
        if mode == "hybrid" and backend not in ("local", "llm"):
            _recordDegradedReason(diagnostics, "selectorConfig")
            return MemoryRetrievalResult(diagnostics=diagnostics)
        useLLM = mode == "hybrid" and backend == "llm"
        if useLLM:
            try:
                selectorSettings = getMemorySelectorSettings(configSnapshot)
            except ValueError:
                # 与未知后端同一原因码：配置错误要能从 diagnostics 直接定位，
                # 不能落到外层兜底后只剩异常类名 ValueError。
                _recordDegradedReason(diagnostics, "selectorConfig")
                return MemoryRetrievalResult(diagnostics=diagnostics)
            selectorOwner = getStateManager().getMemoryRuntime()
            if selectorOwner is None or not selectorOwner.registerSelectorRetrieval(lease):
                _recordDegradedReason(diagnostics, "selectorRuntimeUnavailable")
                return MemoryRetrievalResult(diagnostics=diagnostics)
            # llm 候选阶段有独立两秒预算；远程选择和最终复核不占用它。
            selectionDeadline = started + LLM_MEMORY_RETRIEVAL_TIMEOUT_SECONDS
        diagnostics["selector"] = "llm" if useLLM else "local"

        # ===== 分支一：legacy（生产默认）=====
        if mode == "legacy":
            perScopeLimit, totalLimit = legacyLimits or (
                LLM_MEMORY_RETRIEVE_PER_SCOPE,
                LLM_MEMORY_RETRIEVE_TOTAL,
            )
            # legacy 仍保留原有 priority/每 scope/总量规则，hybrid 才使用完整
            # 候选集做语义准入；pinned 在两种模式中都走独立常驻预算。
            allCandidates = await _withDeadline(
                getMemoryCandidates(
                    chatID=chatID, userID=userID, sessionID=sessionID,
                ),
                selectionDeadline,
                lease=lease,
            )
            # 同一份完整读取同时提供 pinned 和 legacy 的 contextual 池。
            # 过去这里再调用 retrieveMemories()，会为同一请求重复读取、解密
            # 数据库；纯函数 selector 复现旧的逐 scope/总量截断而不增加 IO。
            # legacy 的 20/10 配额只约束 contextual：pinned 已经是独立的
            # 常驻预算，不能因为 priority 更高而挤掉情境记忆的候选名额。
            contextualCandidates = [
                memory for memory in allCandidates
                if memory.get("mode", MEMORY_MODE_CONTEXTUAL) == MEMORY_MODE_CONTEXTUAL
            ]
            legacyItems = selectLegacyMemoryCandidates(
                contextualCandidates,
                perScopeLimit=perScopeLimit,
                totalLimit=totalLimit,
            )
            # pinned 从完整候选中单独分拣，和 contextual 的 legacy 配额完全
            # 解耦；这也与离线 evaluate 的 legacyPool 保持同一行为。
            pinned = sortPinnedMemories([
                memory for memory in allCandidates
                if memory.get("mode") == MEMORY_MODE_PINNED
            ])
            contextual = legacyItems
            diagnostics["candidateCount"] = len(allCandidates)
            diagnostics["contextualCandidateCount"] = len(contextualCandidates)
        else:
            allCandidates = await _withDeadline(
                getMemoryCandidates(
                    chatID=chatID, userID=userID, sessionID=sessionID,
                ),
                selectionDeadline,
                lease=lease,
            )
            pinned = sortPinnedMemories([
                memory for memory in allCandidates
                if memory.get("mode") == MEMORY_MODE_PINNED
            ])
            contextualCandidates = [
                memory for memory in allCandidates
                if memory.get("mode", MEMORY_MODE_CONTEXTUAL) == MEMORY_MODE_CONTEXTUAL
            ]
            diagnostics["candidateCount"] = len(allCandidates)
            diagnostics["contextualCandidateCount"] = len(contextualCandidates)

            # ===== 分支二：hybrid =====
            # 查询文本与后端门控先行；local 阈值无效时只有 pinned，
            # llm 独立收集候选，不能把未批准阈值当成实验后端的评分开关。
            queryNow = datetime.now()
            currentText, assistedText, lexicalText = buildQueryTexts(query, now=queryNow)
            # LLM 只用排序收集候选，不拿未批准阈值冒充准入标准。
            if useLLM:
                thresholds, calibrationReason = {}, None
                enabledChannels = dict.fromkeys(_CHANNEL_NAMES, True)
                diagnostics["calibrationPolicy"] = "independentLlmSelector"
            else:
                thresholds, calibrationReason = loadCalibratedThresholds()
                enabledChannels = {
                    name: thresholds.get(name) is not None for name in _CHANNEL_NAMES
                }
            for channelName in _CHANNEL_NAMES:
                _setChannelDiagnostic(
                    diagnostics,
                    channelName,
                    status=(
                        "disabled"
                        if not enabledChannels[channelName]
                        else "enabled"
                    ),
                )
            if calibrationReason:
                _recordDegradedReason(diagnostics, calibrationReason)
                for channelName in _CHANNEL_NAMES:
                    _setChannelDiagnostic(
                        diagnostics,
                        channelName,
                        status="calibrationUnavailable",
                        reason=calibrationReason,
                    )

            channelScores = {
                "semanticCurrent": {},
                "semanticAssisted": {},
                "lexical": {},
            }
            semanticRankingScores = {
                "semanticCurrent": {},
                "semanticAssisted": {},
            }
            # 词面通道：BM25 在线程池跑（纯 CPU，不能占事件循环）；
            # 该通道独立降级——超时/异常只记原因，语义通道照常。
            if lexicalText and enabledChannels["lexical"]:
                try:
                    channelScores["lexical"] = await _withDeadline(
                        asyncio.to_thread(
                            scoreLexicalCandidates,
                            lexicalText,
                            contextualCandidates,
                        ),
                        selectionDeadline,
                        lease=lease,
                    )
                    _setChannelDiagnostic(
                        diagnostics,
                        "lexical",
                        status=(
                            "ready" if channelScores["lexical"] else "empty"
                        ),
                        scoreCount=len(channelScores["lexical"]),
                    )
                except asyncio.TimeoutError:
                    _recordDegradedReason(diagnostics, "lexicalTimeout")
                    _setChannelDiagnostic(
                        diagnostics,
                        "lexical",
                        status="timeout",
                        reason="lexicalTimeout",
                    )
                except Exception as exc:
                    reason = f"lexical:{type(exc).__name__}"
                    _recordDegradedReason(diagnostics, reason)
                    _setChannelDiagnostic(
                        diagnostics,
                        "lexical",
                        status="error",
                        reason=reason,
                    )
            elif not lexicalText:
                _setChannelDiagnostic(diagnostics, "lexical", status="noQuery")

            # 语义通道的查询文本选择由纯函数统一决定：相同文本只保留
            # canonical 通道，current 关闭时才让 assisted 接管。
            if useLLM:
                semanticPlan = []
                for name, textValue in (("semanticCurrent", currentText), ("semanticAssisted", assistedText)):
                    if textValue and textValue not in [value for _, value in semanticPlan]:
                        semanticPlan.append((name, textValue))
            else:
                semanticPlan = buildSemanticQueryPlan(currentText, assistedText, thresholds)
            semanticNames = [name for name, _ in semanticPlan]
            semanticQueries = [textValue for _, textValue in semanticPlan]
            for channelName in _CHANNEL_NAMES[:2]:
                if not enabledChannels[channelName]:
                    continue
                if not semanticQueries:
                    _setChannelDiagnostic(
                        diagnostics,
                        channelName,
                        status="noQuery",
                    )
                elif channelName not in semanticNames:
                    _setChannelDiagnostic(
                        diagnostics,
                        channelName,
                        status="deduplicated",
                    )

            # 只在本后端有有效 semantic query 时触碰 stateManager/runtime。
            # local 未校准、空查询或全部通道关闭时，无需读取缓存诊断。
            runtime = None
            if semanticQueries:
                runtime = getStateManager().getMemoryRuntime()
                if runtime is not None:
                    inspectCache = getattr(runtime, "getSemanticCacheStatus", None)
                    if callable(inspectCache):
                        try:
                            # 这是进入评分前的只读快照：它解释本次语义缺席
                            # 是冷缓存、过期还是容量饱和，不暴露任何正文。
                            diagnostics["semanticCache"] = inspectCache(
                                contextualCandidates
                            )
                        except Exception as exc:
                            diagnostics["semanticCache"] = {
                                "status": "unavailable",
                                "candidateCount": len(contextualCandidates),
                                "reason": type(exc).__name__,
                            }
                    else:
                        diagnostics["semanticCache"] = {
                            "status": "unreported",
                            "candidateCount": len(contextualCandidates),
                        }
                else:
                    diagnostics["semanticCache"] = {
                        "status": "runtimeUnavailable",
                        "candidateCount": len(contextualCandidates),
                    }
            if semanticQueries and runtime is not None:
                try:
                    if useLLM:
                        # runtime 自带 deadline；外层仍约束坏实现，取消不能绕过本地预算。
                        async with asyncio.timeout(_remaining(selectionDeadline)):
                            semanticResults = await runtime.scoreSemantic(
                                semanticQueries, contextualCandidates, deadline=selectionDeadline,
                            )
                    else:
                        semanticResults = await runtime.scoreSemantic(
                            semanticQueries, contextualCandidates, deadline=selectionDeadline,
                        )
                    for name, scoreSet in zip(semanticNames, semanticResults):
                        # runtime 同时返回两种 memory 表示的分数。base 写入
                        # channelScores 参与阈值准入；enhanced 单独保存，只能
                        # 在 selectContextualCandidates 内重排已准入的 ID。
                        channelScores[name] = scoreSet["base"]
                        semanticRankingScores[name] = scoreSet["enhanced"]
                        _setChannelDiagnostic(
                            diagnostics,
                            name,
                            status=(
                                "ready"
                                if channelScores[name]
                                else "empty"
                            ),
                            scoreCount=len(channelScores[name]),
                        )
                except asyncio.TimeoutError:
                    _recordDegradedReason(diagnostics, "semanticTimeout")
                    for name in semanticNames:
                        _setChannelDiagnostic(
                            diagnostics,
                            name,
                            status="timeout",
                            reason="semanticTimeout",
                        )
                except Exception as exc:
                    reason = f"semantic:{type(exc).__name__}"
                    _recordDegradedReason(diagnostics, reason)
                    for name in semanticNames:
                        _setChannelDiagnostic(
                            diagnostics,
                            name,
                            status="error",
                            reason=reason,
                        )
            elif semanticQueries:
                diagnostics["semanticReason"] = "runtimeUnavailable"
                _recordDegradedReason(diagnostics, "semanticRuntimeUnavailable")
                for name in semanticNames:
                    _setChannelDiagnostic(
                        diagnostics,
                        name,
                        status="runtimeUnavailable",
                        reason="semanticRuntimeUnavailable",
                    )

            # 只让 contextual memory 进入三通道筛选；pinned 不参与相关性竞争，
            # 但仍由后面的独立预算和快照复核保护。
            if useLLM:
                contextual = []
                try:
                    # 传输是唯一远程期限控制器；独立cleanup由同次lease/runtime持有。
                    contextual = await _selectWithLLM(
                        contextualCandidates, channelScores, query, now=queryNow,
                        settings=selectorSettings, diagnostics=diagnostics, lease=lease, owner=selectorOwner,
                    )
                except Exception:
                    timedOut = diagnostics.get("selectorFailure") in ("selectorTimeout", "selectorTransportTimeout")
                    diagnostics["selectorStatus"] = "timeout" if timedOut else "failed"
                    _recordDegradedReason(diagnostics, "selectorTimeout" if timedOut else "selectorFailed")
                # 远程失败仅丢弃 contextual；已读到的 pinned 仍须在收尾期限内复核。
                deadline = time.monotonic() + finalizeReserve
            else:
                contextual, selectionDiagnostics = selectContextualCandidates(
                    contextualCandidates,
                    channelScores,
                    thresholds,
                    semanticRankingScores=semanticRankingScores,
                )
                diagnostics.update(selectionDiagnostics)

        # ===== 收尾（两分支共用）：去重 → 初装 → 复核 → 重渲染 =====
        if getStateManager().getShutdownEvent().is_set() or (
                selectorOwner is not None and not selectorOwner.selectorAccepting()):
            _recordDegradedReason(diagnostics, "retrievalStopping")
            return MemoryRetrievalResult(diagnostics=diagnostics)
        # 去重放在两模式汇合点：同一正文可能同时存在于 pinned 与 contextual
        # 池（如升级/降级中途），按 scope+正文去重、保留排序靠前的一条。
        combined = deduplicateMemoryCandidates([*pinned, *contextual])
        pinned = [memory for memory in combined if memory.get("mode") == MEMORY_MODE_PINNED]
        contextual = [
            memory for memory in combined
            if memory.get("mode", MEMORY_MODE_CONTEXTUAL) == MEMORY_MODE_CONTEXTUAL
        ]
        diagnostics["pinnedCandidateCount"] = len(pinned)
        if diagnostics["contextualCandidateCount"] == 0:
            diagnostics["contextualCandidateCount"] = len(contextual)

        # 渲染两遍：第一遍按候选快照确定入选集合与预算，复核剔除窗口期
        # 变更的条目后，第二遍用幸存集合重渲染——保证 contextBlock 的每一行
        # 与 items 一致，预算判断也基于同一集合（这里的 _ 是第一遍的块文本，
        # 复核后必然作废，故不使用）。注意复核用的是完整 deadline，对应
        # 开头的 finalizeReserve 保留量。
        selected, _, budgetDiagnostics = renderMemoryContext(pinned, contextual)
        diagnostics.update(budgetDiagnostics)
        validIDs = await _validateSelected(
            selected,
            deadline=deadline,
            lease=lease,
        )
        if getStateManager().getShutdownEvent().is_set() or (
                selectorOwner is not None and not selectorOwner.selectorAccepting()):
            _recordDegradedReason(diagnostics, "retrievalStopping")
            return MemoryRetrievalResult(diagnostics=diagnostics)
        pinned = [memory for memory in pinned if int(memory["id"]) in validIDs]
        contextual = [memory for memory in contextual if int(memory["id"]) in validIDs]
        selected, contextBlock, finalBudgetDiagnostics = renderMemoryContext(
            pinned,
            contextual,
        )
        diagnostics.update(finalBudgetDiagnostics)
        diagnostics["selectedCount"] = len(selected)
        diagnostics["elapsedMs"] = round((time.monotonic() - started) * 1000, 2)
        return MemoryRetrievalResult(
            items=selected,
            contextBlock=contextBlock,
            diagnostics=diagnostics,
        )
    # 任何异常（含超时）都不外抛：转成带原因码的空结果，记忆缺席降质量，
    # 打断回复生成才是事故。lease 在 finally 归还，挂了回调的延期归还。
    except asyncio.TimeoutError:
        _recordDegradedReason(diagnostics, "retrievalTimeout")
    except Exception as exc:
        _recordDegradedReason(diagnostics, type(exc).__name__)
    finally:
        if selectorOwner is not None:
            selectorOwner.finishSelectorRetrieval(lease)
        lease.release()

    diagnostics["elapsedMs"] = round((time.monotonic() - started) * 1000, 2)
    return MemoryRetrievalResult(diagnostics=diagnostics)
