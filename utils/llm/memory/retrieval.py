"""
utils/llm/memory/retrieval.py

记忆检索的编排层：读取候选、计算通道分数、选出最终进入 prompt 的条目。

配置项 memoryRetrievalMode 决定检索路径——legacy 为重构前的原逻辑
（每 scope 限量取池、priority 排序、取前 10 条），是当前生产默认；
hybrid 为新逻辑（全量候选 → 词面/语义双通道打分 → 各自过阈值 →
名次融合 → 1500 字符预算装块），阈值完成校准前不会启用。两种模式下
pinned 常驻记忆均走独立预算，不参与打分竞争。

错误处理原则：宁可缺少记忆，不提供错误记忆，且绝不抛出异常。阈值文件
缺失、模型未安装、超时等故障均返回空结果，并在 diagnostics 记录原因。
特别地，hybrid 内部故障不会退回 legacy 的选择逻辑——否则线上无法
区分新逻辑是否在实际工作。
"""

import asyncio
import json
import math
import re
import time
import unicodedata
from datetime import datetime
from pathlib import Path

from config import (
    LLM_MEMORY_CALIBRATION_PATH,
    LLM_MEMORY_CONTEXT_MAX_CHARS,
    LLM_MEMORY_MAX_ACTIVE_RETRIEVALS,
    LLM_MEMORY_PINNED_MAX_CHARS,
    LLM_MEMORY_QUERY_HISTORY_LIMIT,
    LLM_MEMORY_QUERY_HISTORY_MAX_CHARS,
    LLM_MEMORY_QUERY_HISTORY_SECONDS,
    LLM_MEMORY_RETRIEVAL_TIMEOUT_SECONDS,
    LLM_MEMORY_RETRIEVE_PER_SCOPE,
    LLM_MEMORY_RETRIEVE_TOTAL,
    LLM_MEMORY_RRF_K,
)

from utils.core.stateManager import getStateManager
from utils.llm.config import loadLLMConfig
from utils.llm.promptSafety import neutralizePromptDelimiters

from .database import (
    MEMORY_MODE_CONTEXTUAL,
    MEMORY_MODE_PINNED,
    MEMORY_SCOPE_RANK,
    getMemoryCandidates,
    getMemorySnapshots,
    retrieveMemories,
)
from .encoder import loadModelManifest
from .lexical import scoreLexicalCandidates
from .types import (
    MemoryQuery,
    MemoryRetrievalResult,
    buildMemoryStateFingerprint,
)


LEXICAL_VERSION = "memory-bm25-v1"
# 当前占用检索容量的请求数（与 _RetrievalLease 配合的全局记账）
_activeRetrievals = 0




class _RetrievalLease:
    """
    一次检索的并发名额记账：检索开始时占用名额，结束时归还。

    同时最多 2 个检索在执行（LLM_MEMORY_MAX_ACTIVE_RETRIEVALS），超出的
    请求直接返回空结果。超时场景需要特殊处理：调用方等待超时返回后，
    已提交到线程池的评分任务仍在执行——名额必须等该任务真正结束才能
    归还（deferUntil），否则连续超时会绕过并发上限，实际同时执行的
    任务数超出限制。
    """

    def __init__(self):
        self._deferred = False
        self._released = False


    def deferUntil(self, task: asyncio.Future) -> None:
        """为任务注册完成回调，由回调在任务结束后归还名额。"""
        if self._deferred or self._released:
            return
        self._deferred = True
        task.add_done_callback(self._releaseAfterTask)


    def _releaseAfterTask(self, task: asyncio.Future) -> None:
        try:
            task.exception()
        except (asyncio.CancelledError, Exception):
            pass
        self._releaseNow()


    def _releaseNow(self) -> None:
        global _activeRetrievals
        if self._released:
            return
        self._released = True
        _activeRetrievals = max(0, _activeRetrievals - 1)


    def release(self) -> None:
        """检索正常结束时立即归还名额（已注册回调延期归还的除外）。"""
        if not self._deferred:
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
        if calibration.get("schemaVersion") != 1:
            raise ValueError("calibrationSchemaMismatch")
        # candidate 是离线评测产物，必须经过人工/部署流程批准后才能影响线上准入。
        if calibration.get("status") == "candidate":
            raise ValueError("calibrationCandidate")
        if calibration.get("status") not in (None, "approved"):
            raise ValueError("calibrationStatusInvalid")
        if calibration.get("modelRevision") != manifest["revision"]:
            raise ValueError("calibrationModelMismatch")
        if calibration.get("encodingVersion") != manifest["encodingVersion"]:
            raise ValueError("calibrationEncodingMismatch")
        if calibration.get("lexicalVersion") != LEXICAL_VERSION:
            raise ValueError("calibrationLexicalMismatch")
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
        reasonCode = str(e) if isinstance(e, ValueError) else type(e).__name__
        return {
            "semanticCurrent": None,
            "semanticAssisted": None,
            "lexical": None,
        }, reasonCode


def _rankQualified(scores: dict[int, float], threshold: float | None) -> dict[int, int]:
    """过滤未过阈值的条目，其余按分数排名（同分并列同名次）。

    名次（而非原始分数）作为 RRF 融合的输入——三通道分数量纲不同
    （余弦相似度 vs BM25 分），不可直接相加，统一转换为名次参与计算。
    """
    if threshold is None:
        return {}
    ordered = sorted(
        (
            (memoryID, score)
            for memoryID, score in scores.items()
            if isinstance(score, (int, float))
            and not isinstance(score, bool)
            and math.isfinite(score)
            and score >= threshold
        ),
        key=lambda item: (-item[1], item[0]),
    )
    ranks = {}
    previousScore = None
    currentRank = 0
    for position, (memoryID, score) in enumerate(ordered, start=1):
        if previousScore is None or score != previousScore:
            currentRank = position
            previousScore = score
        ranks[memoryID] = currentRank
    return ranks


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
) -> tuple[list[dict], dict]:
    """各通道独立筛过阈值者，经名次融合生成最终候选列表。

    每个通道先独立应用自己的阈值（两个语义 + 一个词面）；记忆在任一
    通道过关即入选，多通道同时过关则融合名次靠前。融合采用 RRF：
    每条记忆累加 1/(K+名次)，K=60——量纲无关，只比较相对名次。
    顺序为先过阈值再融合：某通道未过关的记忆，不会因另一通道分数
    较高而被重新纳入，反之亦然。

    排序兜底键依次为 融合分 > priority > scope 专属度 > 更新时间 > ID，
    保证相同输入始终得到相同顺序（评测可复现的前提）。
    """
    channelRanks = {
        name: _rankQualified(channelScores.get(name, {}), thresholds.get(name))
        for name in ("semanticCurrent", "semanticAssisted", "lexical")
    }
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
    return selected, diagnostics


def _deduplicateCandidates(memories: list[dict]) -> list[dict]:
    """按 scope 与规范化正文去重，并保留排序更靠前的第一条记录。"""
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
    的 500 字符保底预算，常驻记忆不会被情境条目全部挤出。
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

    评分、排序到实际使用之间有几十毫秒到两秒的间隔，期间 ops 可能
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
    按 priority 排序截前 10 条）或 hybrid（全量候选过三通道打分）。
    整个流程限时 2 秒：超时、并发已满（同时最多 2 个检索）、任一
    环节出错，均返回空结果并在 diagnostics 记录原因——记忆缺席只
    降低质量，抛异常打断回复生成才是事故。

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
    }
    if _activeRetrievals >= LLM_MEMORY_MAX_ACTIVE_RETRIEVALS:
        diagnostics["degradedReason"] = "retrievalCapacity"
        return MemoryRetrievalResult(diagnostics=diagnostics)

    _activeRetrievals += 1
    lease = _RetrievalLease()
    deadline = started + LLM_MEMORY_RETRIEVAL_TIMEOUT_SECONDS
    try:
        configSnapshot = llmConfig if llmConfig is not None else loadLLMConfig()
        mode = configSnapshot.get("memoryRetrievalMode", "legacy")
        if mode not in {"legacy", "hybrid"}:
            mode = "legacy"
        diagnostics["mode"] = mode

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
                deadline,
                lease=lease,
            )
            legacyItems = await _withDeadline(
                retrieveMemories(
                    chatID=chatID,
                    userID=userID,
                    sessionID=sessionID,
                    perScopeLimit=perScopeLimit,
                    totalLimit=totalLimit,
                ),
                deadline,
                lease=lease,
            )
            pinned = sortPinnedMemories([
                memory for memory in allCandidates
                if memory.get("mode") == MEMORY_MODE_PINNED
            ])
            contextual = [
                memory for memory in legacyItems
                if memory.get("mode", MEMORY_MODE_CONTEXTUAL) == MEMORY_MODE_CONTEXTUAL
            ]
            diagnostics["candidateCount"] = len(allCandidates)
        else:
            allCandidates = await _withDeadline(
                getMemoryCandidates(
                    chatID=chatID, userID=userID, sessionID=sessionID,
                ),
                deadline,
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

            currentText, assistedText, lexicalText = buildQueryTexts(query)
            thresholds, calibrationReason = loadCalibratedThresholds()
            if calibrationReason:
                diagnostics["degradedReason"] = calibrationReason

            channelScores = {
                "semanticCurrent": {},
                "semanticAssisted": {},
                "lexical": {},
            }
            if lexicalText and thresholds["lexical"] is not None:
                try:
                    channelScores["lexical"] = await _withDeadline(
                        asyncio.to_thread(
                            scoreLexicalCandidates,
                            lexicalText,
                            contextualCandidates,
                        ),
                        deadline,
                        lease=lease,
                    )
                except asyncio.TimeoutError:
                    diagnostics["degradedReason"] = "lexicalTimeout"
                except Exception as exc:
                    diagnostics["degradedReason"] = (
                        f"lexical:{type(exc).__name__}"
                    )

            semanticQueries = []
            semanticNames = []
            semanticCandidates = (
                ("semanticCurrent", currentText),
                ("semanticAssisted", assistedText),
            )
            for name, textValue in semanticCandidates:
                if not textValue or thresholds[name] is None:
                    continue
                # 当前消息是同一文本的 canonical 语义证据。若当前通道未启用，
                # 才允许辅助通道接管；相同文本不能因两个标签获得两份 RRF 贡献。
                if textValue == currentText and name == "semanticAssisted":
                    if currentText and thresholds["semanticCurrent"] is not None:
                        continue
                if textValue in semanticQueries:
                    continue
                semanticQueries.append(textValue)
                semanticNames.append(name)

            runtime = getStateManager().getMemoryRuntime()
            if semanticQueries and runtime is not None:
                try:
                    semanticResults = await runtime.scoreSemantic(
                        semanticQueries,
                        contextualCandidates,
                        deadline=deadline,
                    )
                    for name, scores in zip(semanticNames, semanticResults):
                        channelScores[name] = scores
                except asyncio.TimeoutError:
                    diagnostics["degradedReason"] = "semanticTimeout"
                except Exception as exc:
                    diagnostics["degradedReason"] = (
                        f"semantic:{type(exc).__name__}"
                    )
            elif semanticQueries:
                diagnostics["semanticReason"] = "runtimeUnavailable"

            # 只让 contextual memory 进入三通道筛选；pinned 不参与相关性竞争，
            # 但仍由后面的独立预算和快照复核保护。
            contextual, selectionDiagnostics = selectContextualCandidates(
                contextualCandidates,
                channelScores,
                thresholds,
            )
            diagnostics.update(selectionDiagnostics)

        combined = _deduplicateCandidates([*pinned, *contextual])
        pinned = [memory for memory in combined if memory.get("mode") == MEMORY_MODE_PINNED]
        contextual = [
            memory for memory in combined
            if memory.get("mode", MEMORY_MODE_CONTEXTUAL) == MEMORY_MODE_CONTEXTUAL
        ]
        diagnostics["pinnedCandidateCount"] = len(pinned)
        if diagnostics["contextualCandidateCount"] == 0:
            diagnostics["contextualCandidateCount"] = len(contextual)

        # 先按候选快照计算一次预算，再做数据库复核，最后重新渲染，避免复核
        # 删除条目后留下错误的预算判断或旧的 contextBlock。
        selected, _, budgetDiagnostics = renderMemoryContext(pinned, contextual)
        diagnostics.update(budgetDiagnostics)
        validIDs = await _validateSelected(
            selected,
            deadline=deadline,
            lease=lease,
        )
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
    except asyncio.TimeoutError:
        diagnostics["degradedReason"] = "retrievalTimeout"
    except Exception as exc:
        diagnostics["degradedReason"] = type(exc).__name__
    finally:
        lease.release()

    diagnostics["elapsedMs"] = round((time.monotonic() - started) * 1000, 2)
    return MemoryRetrievalResult(diagnostics=diagnostics)
