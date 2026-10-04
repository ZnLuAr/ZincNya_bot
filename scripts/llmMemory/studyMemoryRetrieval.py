"""仅用 calibration 对照编码、候选覆盖与两阶段准入，不使用 holdout 标注。

实验复用正式编码器、评分、融合和预算；分组交叉验证用于发现校准集过拟合，
不是新的盲测，也不会生成可供线上加载的 calibration。两阶段重排在
预算超限时保留中止证据，不输出不完整的质量成绩。
"""

import argparse
import hashlib
import json
import math
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from config import LLM_MEMORY_CALIBRATION_PATH, LLM_MEMORY_MODEL_DIR, LLM_MEMORY_MODEL_MANIFEST_PATH
from scripts.llmMemory import evaluateMemory as evaluation
from utils.llm.memory.encoder import (
    MemoryEncoder, calculateFileSha256, resolveModelArtifactPath,
)
from utils.llm.memory.retrieval import buildQueryTexts


ENCODING_VARIANTS = ("baseline", "plain", "no-prefix", "separated", "plain-no-prefix")
ASSISTED_CURRENT_FLOORS = (None, 0.2, 0.3, 0.4)
FOLD_COUNT = 5
RERANKER_SHORTLIST_SIZE = 8
CANDIDATE_STRATEGIES = ("dense", "lexical", "union")
CANDIDATE_LIMITS = (8, 16, 32)
PAIR_STUDY_MEMORY_LIMIT = 512 * 1024 * 1024
RERANKER_RUNTIME_PROFILES = ("default", "low-memory")


class StudyEncoder(MemoryEncoder):
    """以离线子类隔离待验证的表示，不悄悄改变正式编码版本。"""

    variant = "baseline"

    def _formatMemoryText(self, memory: dict, *, includeHint: bool) -> str:
        """自然文本对照去除字段标题，仍严格限制 hint 只进入 enhanced。"""
        if self.variant not in {"plain", "plain-no-prefix"}:
            return super()._formatMemoryText(memory, includeHint=includeHint)
        sections = [str(memory["content"]).strip()]
        tags = [str(tag).strip() for tag in memory.get("tags") or [] if str(tag).strip()]
        if tags:
            sections.append("，".join(tags))
        if includeHint and memory.get("retrievalHint"):
            sections.append(str(memory["retrievalHint"]).strip())
        return "\n".join(sections)

    def _prepareQuery(self, text: str) -> list[int]:
        """分别比较检索指令前缀和段落边界，其他编码行为保持不变。"""
        if self.variant == "separated":
            parts = self._splitAssistedQuery(text)
            if parts is not None:
                # BERT tokenizer 通常不保留换行；明确句号边界避免不同消息
                # 的尾首字直接连接。仍走原有优先级和 token 预算，不放宽窗口。
                currentText, replyText, historyLines = parts
                text = currentText + "。"
                if replyText:
                    text += "\n\n引用：\n" + replyText + "。"
                if historyLines:
                    text += "\n\n近期对话：\n" + "\n".join(line + "。" for line in historyLines)
        prefix = self._queryPrefix
        try:
            if self.variant in {"no-prefix", "plain-no-prefix"}:
                self._queryPrefix = ""
            return super()._prepareQuery(text)
        finally:
            self._queryPrefix = prefix


def readRerankerManifest(modelDir: str | Path, manifestPath: str | Path) -> dict:
    """分词与推理均先校验相同产物，不能以串行运行绕过完整性检查。"""
    manifest = json.loads(Path(manifestPath).read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schemaVersion") != 1 or manifest.get("task") != "text-pair-relevance":
        raise evaluation.EvaluationError("重排实验清单类型无效")
    artifacts = manifest["artifacts"]
    declared = {artifact["path"] for artifact in artifacts}
    if not {manifest["modelFile"], manifest["tokenizerFile"]} <= declared:
        raise evaluation.EvaluationError("重排模型和 tokenizer 必须在校验清单内")
    # 实验模型也不能跳过路径、大小和散列校验；不把下载成功当作可信。
    for artifact in artifacts:
        path = resolveModelArtifactPath(modelDir, artifact["path"])
        if path.stat().st_size != artifact["size"] or calculateFileSha256(path) != artifact["sha256"]:
            raise evaluation.EvaluationError("重排模型产物校验失败")
    return manifest


def loadRerankerTokenizer(modelDir: str | Path, manifest: dict):
    """只加载分词依赖；调用者必须先通过清单校验，不同时常驻推理库。"""
    tokenizers = MemoryEncoder._importOptionalDependency("tokenizers")
    tokenizer = tokenizers.Tokenizer.from_file(str(resolveModelArtifactPath(modelDir, manifest["tokenizerFile"])))
    tokenizer.enable_truncation(max_length=manifest["maxTokens"], strategy="longest_first")
    padToken = manifest["padToken"]
    padID = tokenizer.token_to_id(padToken)
    if padID is None:
        raise evaluation.EvaluationError("重排 tokenizer 缺少 pad token")
    tokenizer.enable_padding(pad_id=padID, pad_token=padToken)
    return tokenizer


def encodeRerankerPairs(tokenizer, pairs: list[tuple[str, str]]) -> list[dict]:
    """将原分词结果复制成可序列化的整型列表，不改变 token 或 padding。"""
    if not pairs:
        return []
    encoded = tokenizer.encode_batch(pairs)
    if len(encoded) != len(pairs):
        raise evaluation.EvaluationError("重排 tokenizer 必须为每对文本返回一条编码")
    return [{
        "input_ids": list(item.ids),
        "attention_mask": list(item.attention_mask),
        "token_type_ids": list(item.type_ids),
    } for item in encoded]




class StudyReranker:
    """仅供实验的本地成对相关性模型，不是可上线的 MemoryEncoder。

    只接收 query 与 content+tags，不接触 hint。256 token 是整对输入的
    上限；长文本按 tokenizer 的 longest_first 截断，不宣称覆盖完整长记忆。
    """

    def __init__(self, modelDir: str | Path, manifestPath: str | Path, *, runtimeProfile: str = "default",
                 stageObserver=None, loadTokenizer: bool = True):
        """先校验清单再加载；串行推理允许仅加载 session，不导入分词依赖。

        stageObserver 只通知加载边界，不能成为同步 native 分配的硬限额。
        默认配置保持旧实验行为，不能把优化开关变化解释成模型质量改进。
        """
        if runtimeProfile not in RERANKER_RUNTIME_PROFILES:
            raise evaluation.EvaluationError("未知重排运行时配置")
        self.runtimeProfile = runtimeProfile
        self.manifest = readRerankerManifest(modelDir, manifestPath)
        self.tokenizer = None
        if stageObserver:
            stageObserver("pair-imports-start")
        self.numpy = MemoryEncoder._importOptionalDependency("numpy")
        onnxruntime = MemoryEncoder._importOptionalDependency("onnxruntime")
        if stageObserver:
            stageObserver("pair-imports-complete")
        if loadTokenizer:
            if stageObserver:
                stageObserver("pair-tokenizer-start")
            self.tokenizer = loadRerankerTokenizer(modelDir, self.manifest)
            if stageObserver:
                stageObserver("pair-tokenizer-complete")
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        options.execution_mode = onnxruntime.ExecutionMode.ORT_SEQUENTIAL
        options.enable_cpu_mem_arena = False
        options.enable_mem_pattern = False
        # 禁止图重写/权重预打包可能减少初始化副本，也可能降低吞吐；只有
        # 实测能判断内存是否下降，不能用量化权重的文件大小代替资源验收。
        if runtimeProfile == "low-memory":
            options.graph_optimization_level = onnxruntime.GraphOptimizationLevel.ORT_DISABLE_ALL
            options.add_session_config_entry("session.disable_prepacking", "1")
        if stageObserver:
            stageObserver("pair-session-start")
        self.session = onnxruntime.InferenceSession(
            str(resolveModelArtifactPath(modelDir, self.manifest["modelFile"])),
            sess_options=options, providers=["CPUExecutionProvider"],
        )
        self.timings = []
        self.pairCount = 0
        if stageObserver:
            stageObserver("pair-session-complete")

    def scorePairs(self, pairs: list[tuple[str, str]]) -> list[float]:
        """保留原文本接口及编码加推理计时，串行 session 禁止偷偷加载分词器。"""
        if self.tokenizer is None:
            raise evaluation.EvaluationError("重排 session 未加载 tokenizer，必须使用预编码输入")
        if not pairs:
            return []
        started = time.perf_counter()
        scores = self.scoreEncodedPairs(encodeRerankerPairs(self.tokenizer, pairs))
        self.timings[-1] = time.perf_counter() - started
        return scores

    def scoreEncodedPairs(self, encoded: list[dict]) -> list[float]:
        """验证自有编码协议后推理；仅记录本阶段耗时，不把分词时间算作节省。"""
        fieldNames = ("input_ids", "attention_mask", "token_type_ids")
        if not isinstance(encoded, list):
            raise evaluation.EvaluationError("重排预编码输入必须为列表")
        if not encoded:
            return []
        started = time.perf_counter()
        sequenceLength = None
        for item in encoded:
            if not isinstance(item, dict) or set(item) != set(fieldNames):
                raise evaluation.EvaluationError("重排预编码输入字段无效")
            for name in fieldNames:
                values = item[name]
                if not isinstance(values, list) or not 0 < len(values) <= self.manifest["maxTokens"]:
                    raise evaluation.EvaluationError("重排预编码输入长度无效")
                if sequenceLength is None:
                    sequenceLength = len(values)
                if len(values) != sequenceLength:
                    raise evaluation.EvaluationError("重排预编码输入必须为等长 padding 批次")
                if any(type(value) is not int or value < 0 or (name == "attention_mask" and value not in (0, 1)) for value in values):
                    raise evaluation.EvaluationError("重排预编码输入必须为非负整数，mask 仅允许 0 或 1")
        inputs = {}
        for spec in self.session.get_inputs():
            if spec.name not in fieldNames or spec.type not in {"tensor(int64)", "tensor(int32)"}:
                raise evaluation.EvaluationError("重排 ONNX 输入不受支持")
            dtype = self.numpy.int64 if spec.type == "tensor(int64)" else self.numpy.int32
            try:
                inputs[spec.name] = self.numpy.asarray([item[spec.name] for item in encoded], dtype=dtype)
            except (OverflowError, TypeError, ValueError) as exc:
                raise evaluation.EvaluationError("重排预编码输入超出 ONNX 整型范围") from exc
        logits = self.numpy.asarray(self.session.run(None, inputs)[0])
        if logits.shape != (len(encoded), 1) or not self.numpy.isfinite(logits).all():
            raise evaluation.EvaluationError("重排 ONNX 必须返回每对文本一个有限 logit")
        self.timings.append(time.perf_counter() - started)
        self.pairCount += len(encoded)
        return [1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, float(value))))) for value in logits[:, 0]]

    def close(self) -> None:
        """实验退出后释放第二份 ONNX session 和 tokenizer。"""
        self.session = None
        self.tokenizer = None


class StudyPairScorer:
    """先按 base 取有限候选，再由 content+tags 的成对评分决定准入。"""

    def __init__(self, denseScorer, reranker, shortlistSize: int):
        """共享原有 embedding 索引；小池的大小不是最终只取一条的限制。"""
        self.denseScorer = denseScorer
        self.reranker = reranker
        self.shortlistSize = shortlistSize

    def score(self, queryTexts: list[str], candidates: list[dict]) -> list[dict]:
        """hint 不参与缩池或相关性判定，只保留原 enhanced 排序分。"""
        denseResults = self.denseScorer.score(queryTexts, candidates)
        byID = {int(memory["id"]): memory for memory in candidates}
        results = []
        for query, dense in zip(queryTexts, denseResults):
            # 这里取 top-k 是限制成对模型成本，不是把 top-1 直接塞进 prompt。
            # shortlist 仍须逐条经过重新校准的绝对阈值，可以同时留下多条事实。
            selected = sorted(dense["base"], key=lambda memoryID: (-dense["base"][memoryID], memoryID))[:self.shortlistSize]
            pairs = [(query, self.denseScorer.encoder._formatMemoryText(byID[memoryID], includeHint=False)) for memoryID in selected]
            baseScores = dict(zip(selected, self.reranker.scorePairs(pairs)))
            results.append({"base": baseScores, "enhanced": dense["enhanced"]})
        return results

    def clear(self) -> None:
        """复用原评分器的缓存清理，不关闭共享模型。"""
        self.denseScorer.clear()


def loadCalibrationCases(path: str | Path) -> list[dict]:
    """从混合 fixture 只选 calibration；holdout 不参与规范化、评分或输出。

    JSON 文件仍整体解析，因此这是计算隔离，不是禁止读取 holdout 字节。
    """
    rawData = json.loads(Path(path).read_text(encoding="utf-8"))
    rawCases = rawData if isinstance(rawData, list) else rawData["cases"]
    cases = evaluation.validateEvaluationCases([
        case for case in rawCases if case.get("split") == evaluation.CALIBRATION_SPLIT
    ])
    if not cases:
        raise evaluation.EvaluationError("实验需要 calibration 场景")
    if not any(not case["requiredIDs"] and not case["allowedIDs"] for case in cases):
        raise evaluation.EvaluationError("实验需要无答案场景")
    if not any(len(case["requiredIDs"]) > 1 for case in cases):
        raise evaluation.EvaluationError("实验需要多 required 场景")
    return cases


def collectScores(cases: list[dict], scorer) -> dict:
    """每条查询只编码一次；保留两种通道独立校准和相同文本接管的证据。"""
    records = {}
    for case in cases:
        current, assisted, lexical = buildQueryTexts(case["query"], now=case.get("queryNow"))
        candidates = evaluation._contextualCandidates(evaluation._scopeCandidates(case))
        texts = list(dict.fromkeys(text for text in (current, assisted) if text))
        semantic = dict(zip(texts, scorer.score(texts, candidates))) if texts else {}
        empty = {"base": {}, "enhanced": {}}
        records[case["caseID"]] = {
            "sameQuery": current == assisted,
            "scores": {
                "semanticCurrent": semantic.get(current, empty)["base"],
                "semanticAssisted": semantic.get(assisted, empty)["base"],
                "lexical": evaluation.scoreLexicalCandidates(lexical, candidates),
            },
            "ranking": {
                "semanticCurrent": semantic.get(current, empty)["enhanced"],
                "semanticAssisted": semantic.get(assisted, empty)["enhanced"],
            },
        }
    return records


def admissionScores(record: dict, currentFloor: float | None) -> dict:
    """辅助历史不能绕过当前相关性；仅过滤 base 准入，不修改排序表示。

    此门槛是待研究假设：能够拒绝换题后被历史抬高的记忆，也可能伤害真正
    的回指，因此必须同时报告回指与多事实损失，不能只挑 precision。
    """
    scores = {name: dict(values) for name, values in record["scores"].items()}
    if currentFloor is not None and not record["sameQuery"]:
        scores["semanticAssisted"] = {
            memoryID: score for memoryID, score in scores["semanticAssisted"].items()
            if scores["semanticCurrent"].get(memoryID, -math.inf) >= currentFloor
        }
    return scores


def shortlistCoverage(cases: list[dict], records: dict) -> dict:
    """计算进入成对评分前的必要事实覆盖上限，与最终正确召回分开。

    这里的标签只用于测量，不参与缩池；若必要事实没有进入任何语义小池，
    再强的成对模型也无法从这两个通道找回它。词面独立通道不计入此上限。
    """
    requiredCount = sum(len(case["requiredIDs"]) for case in cases)
    coveredCount = 0
    missed = []
    for case in cases:
        scores = records[case["caseID"]]["scores"]
        scoredIDs = set(scores["semanticCurrent"]) | set(scores["semanticAssisted"])
        coveredCount += len(case["requiredIDs"] & scoredIDs)
        missing = sorted(case["requiredIDs"] - scoredIDs)
        if missing:
            missed.append({"caseID": case["caseID"], "requiredNotScoredIDs": missing})
    return {"requiredCount": requiredCount, "coveredRequiredCount": coveredCount, "coverage": coveredCount / requiredCount if requiredCount else None, "missedCases": missed}


def selectCandidateIDs(record: dict, strategy: str, limit: int) -> set[int]:
    """按每通道固定预算取候选，不读取标签、hint 或增强排序分。

    dense 合并 current/assisted 各自 top-k，union 再并入 BM25 正分 top-k；
    因此 k 是每通道预算，合并后最多 3k，不是全池上限。空查询不补候选。
    """
    if strategy not in CANDIDATE_STRATEGIES or limit <= 0:
        raise evaluation.EvaluationError("候选策略无效或预算不是正整数")
    channels = []
    if strategy in {"dense", "union"}:
        channels.append("semanticCurrent")
        if not record["sameQuery"]:
            channels.append("semanticAssisted")
    if strategy in {"lexical", "union"}:
        channels.append("lexical")
    selected = set()
    for channel in channels:
        scores = record["scores"][channel]
        # BM25 的零分表示没有词面证据，不能靠 ID 顺序填满候选预算。
        eligible = [memoryID for memoryID, score in scores.items()
                    if math.isfinite(score) and (channel != "lexical" or score > 0)]
        selected.update(sorted(eligible, key=lambda memoryID: (-scores[memoryID], memoryID))[:limit])
    return selected


def candidateCoverageMetrics(cases: list[dict], selectedByCase: dict) -> dict:
    """报告缩池前后的必要事实覆盖与负例负担，不把候选当最终注入。"""
    requiredCount = coveredCount = completeCount = multiCount = 0
    forbiddenCount = noAnswerCount = noAnswerWithCandidates = 0
    candidateCounts = []
    missedCases = []
    for case in cases:
        selected = selectedByCase[case["caseID"]]
        required = case["requiredIDs"]
        missing = required - selected
        requiredCount += len(required)
        coveredCount += len(required & selected)
        candidateCounts.append(len(selected))
        forbiddenCount += len(case["forbiddenIDs"] & selected)
        if len(required) > 1:
            multiCount += 1
            completeCount += not missing
        if not required and not case["allowedIDs"]:
            noAnswerCount += 1
            noAnswerWithCandidates += bool(selected)
        if missing:
            missedCases.append({"caseID": case["caseID"], "missingRequiredIDs": sorted(missing)})
    return {
        "caseCount": len(cases),
        "requiredCount": requiredCount,
        "coveredRequiredCount": coveredCount,
        "requiredCoverage": coveredCount / requiredCount if requiredCount else None,
        "multiRequiredCaseCount": multiCount,
        "multiRequiredCompleteCoverageCount": completeCount,
        "multiRequiredCompleteCoverageRate": completeCount / multiCount if multiCount else None,
        "forbiddenCandidateCount": forbiddenCount,
        "noAnswerCaseCount": noAnswerCount,
        "noAnswerWithCandidatesCount": noAnswerWithCandidates,
        "candidateCountTotal": sum(candidateCounts),
        "candidateCountMean": sum(candidateCounts) / len(cases) if cases else None,
        "candidateCountMaximum": max(candidateCounts, default=0),
        "missedCases": missedCases,
    }


def restrictToCandidateGate(records: dict, selectedByCase: dict) -> dict:
    """所有候选统一经过 dense 基础准入；词面只缩池，不能旁路放行。

    这是相似度准入对照，不是答案支持模型。hint 仍只影响通过准入后的排序。
    """
    gated = {}
    for caseID, selected in selectedByCase.items():
        record = records[caseID]
        gated[caseID] = {
            "sameQuery": record["sameQuery"],
            "scores": {
                channel: {memoryID: score for memoryID, score in scores.items()
                          if channel != "lexical" and memoryID in selected}
                for channel, scores in record["scores"].items()
            },
            "ranking": {channel: dict(scores) for channel, scores in record["ranking"].items()},
        }
    return gated


def studyCandidatePools(cases: list[dict], records: dict) -> list[dict]:
    """固定网格测候选覆盖，再按相同分组验证 dense 准入，不择优批准配置。"""
    cohorts = {
        "oldCalibration": [case for case in cases if "expandedCalibration" not in case["subsets"]],
        "expandedCalibration": [case for case in cases if "expandedCalibration" in case["subsets"]],
    }
    subsets = {name: [case for case in cases if name in case["subsets"]]
               for name in sorted({name for case in cases for name in case["subsets"]})}
    trials = []
    for strategy in CANDIDATE_STRATEGIES:
        for limit in CANDIDATE_LIMITS:
            # 候选选择完全不看标签；标签只在随后覆盖统计和训练折阈值选择中使用。
            selected = {case["caseID"]: selectCandidateIDs(records[case["caseID"]], strategy, limit)
                        for case in cases}
            coverage = candidateCoverageMetrics(cases, selected)
            coverage["cohorts"] = {name: candidateCoverageMetrics(items, selected) for name, items in cohorts.items()}
            coverage["subsets"] = {name: candidateCoverageMetrics(items, selected) for name, items in subsets.items()}
            trials.append({
                "strategy": strategy,
                "limitPerChannel": limit,
                "coverage": coverage,
                "candidateIDsByCase": {caseID: sorted(ids) for caseID, ids in selected.items()},
                "denseGate": studyScores(cases, restrictToCandidateGate(records, selected), None),
            })
    return trials


class PairStudyMemoryError(evaluation.EvaluationError):
    """保留触发预算中止时的实际观测，避免失败实验丢失资源证据。"""

    def __init__(self, rssBytes: int, processPeakWorkingSet: int | None):
        """当前 RSS 与历史峰值分开保存，缺失的峰值不推断为已测量。"""
        self.rssBytes = rssBytes
        self.processPeakWorkingSet = processPeakWorkingSet
        self.limitBytes = PAIR_STUDY_MEMORY_LIMIT
        super().__init__("重排实验进程内存超过 512 MiB，停止评分")


def checkPairStudyMemory(process, samples: list) -> None:
    """采样进程 RSS 并检查历史工作集峰值，超过实验预算立即停止。

    Windows 的 peak_wset 可以捕捉两次采样之间的峰值；没有该字段的平台
    只能检查当前 RSS，因此这不是通用的硬内存隔离或瞬时峰值保证。
    """
    try:
        memoryInfo = process.memory_info()
        rss = memoryInfo.rss
        peak = getattr(memoryInfo, "peak_wset", None)
    except (AttributeError, OSError, evaluation.psutil.Error) as exc:
        raise evaluation.EvaluationError("无法读取进程内存，停止受限重排实验") from exc
    # 内存观测失败时不能把未知占用当成预算内；samples 仍只存实际 RSS，
    # 避免报告中的 observedMaximum 混入另一种历史峰值统计口径。
    if not isinstance(rss, (int, float)) or not math.isfinite(rss) or rss < 0:
        raise evaluation.EvaluationError("进程 RSS 无效，停止受限重排实验")
    if peak is not None and (
        not isinstance(peak, (int, float)) or not math.isfinite(peak) or peak < 0
    ):
        raise evaluation.EvaluationError("进程峰值工作集无效，停止受限重排实验")
    samples.append(rss)
    if max(rss, peak if peak is not None else rss) > PAIR_STUDY_MEMORY_LIMIT:
        raise PairStudyMemoryError(rss, peak)


def collectCandidatePairScores(
    cases: list[dict], records: dict, reranker, encoder, limit: int,
    *, process, rssSamples: list,
) -> tuple[dict, dict]:
    """对各通道候选并集逐对重排，返回隔离词面旁路的分数与覆盖统计。

    limit 是每通道的固定预算；同一候选会接受 current 与 assisted 各自
    的成对判定，相同查询只运行一次。标签仅用于末尾覆盖报告。
    """
    checkPairStudyMemory(process, rssSamples)
    pairRecords = {}
    selectedByCase = {}
    for case in cases:
        caseID = case["caseID"]
        selected = selectCandidateIDs(records[caseID], "union", limit)
        selectedByCase[caseID] = selected
        current, assisted, _ = buildQueryTexts(case["query"], now=case.get("queryNow"))
        texts = list(dict.fromkeys(text for text in (current, assisted) if text))
        candidates = evaluation._contextualCandidates(evaluation._scopeCandidates(case))
        byID = {int(memory["id"]): memory for memory in candidates}
        if selected - byID.keys():
            raise evaluation.EvaluationError("重排候选包含当前场景不可检索的记忆")
        # 缩池时不能读标签；第二阶段只使用正文与标签，不用 hint，也不让
        # BM25 独立注入。逐对 batch=1 限制激活占用，前后都检查进程预算。
        memoryTexts = {
            memoryID: encoder._formatMemoryText(byID[memoryID], includeHint=False)
            for memoryID in sorted(selected)
        }
        semantic = {}
        for queryText in texts:
            pairScores = {}
            for memoryID, memoryText in memoryTexts.items():
                checkPairStudyMemory(process, rssSamples)
                scores = reranker.scorePairs([(queryText, memoryText)])
                checkPairStudyMemory(process, rssSamples)
                if len(scores) != 1 or not math.isfinite(scores[0]):
                    raise evaluation.EvaluationError("逐对重排必须返回一个有限分数")
                pairScores[memoryID] = scores[0]
            semantic[queryText] = pairScores
        currentScores = semantic.get(current, {})
        assistedScores = semantic.get(assisted, {})
        pairRecords[caseID] = {
            "sameQuery": current == assisted,
            "scores": {
                "semanticCurrent": dict(currentScores),
                "semanticAssisted": dict(assistedScores),
                "lexical": {},
            },
            "ranking": {
                "semanticCurrent": dict(currentScores),
                "semanticAssisted": dict(assistedScores),
            },
        }
    coverage = candidateCoverageMetrics(cases, selectedByCase)
    coverage["cohorts"] = {
        "oldCalibration": candidateCoverageMetrics([
            case for case in cases if "expandedCalibration" not in case["subsets"]
        ], selectedByCase),
        "expandedCalibration": candidateCoverageMetrics([
            case for case in cases if "expandedCalibration" in case["subsets"]
        ], selectedByCase),
    }
    coverage["subsets"] = {
        name: candidateCoverageMetrics([
            case for case in cases if name in case["subsets"]
        ], selectedByCase)
        for name in sorted({name for case in cases for name in case["subsets"]})
    }
    checkPairStudyMemory(process, rssSamples)
    return pairRecords, coverage


def chooseThresholds(cases: list[dict], records: dict, currentFloor: float | None) -> dict:
    """每种实验重新选绝对阈值，继续要求 precision >= 0.95、forbidden=0。"""
    observations = {name: [] for name in evaluation.CHANNEL_NAMES}
    for case in cases:
        positiveIDs = case["requiredIDs"] | case["allowedIDs"]
        scores = admissionScores(records[case["caseID"]], currentFloor)
        for channel, values in scores.items():
            for memoryID, score in values.items():
                observations[channel].append({
                    "score": score,
                    "positive": memoryID in positiveIDs,
                    "forbidden": memoryID in case["forbiddenIDs"],
                })
    return {name: evaluation._chooseThreshold(values)[0] for name, values in observations.items()}


def replayCases(cases: list[dict], records: dict, thresholds: dict, currentFloor: float | None) -> list[dict]:
    """复用线上等价选择与渲染，并保留相同 current/assisted 文本的去重规则。"""
    results = []
    for case in cases:
        record = records[case["caseID"]]
        scores = admissionScores(record, currentFloor)
        # 校准需要独立观察两个通道，回放却只能保留一份同文本证据。
        # current 关闭时 assisted 才能接管，否则会人为获得两次 RRF 贡献。
        if record["sameQuery"] and thresholds["semanticCurrent"] is not None:
            scores["semanticAssisted"] = {}
        result = evaluation._scoreCaseResult(case, evaluation._evaluateSingleCase(
            case, "hybrid+hint", thresholds,
            channelScores=scores, semanticRankingScores=record["ranking"],
        ))
        result.pop("diagnostics", None)
        results.append(result)
    return results


def summarizeResults(results: list[dict]) -> dict:
    """分开报告旧题与扩充题的事实召回和错误，不产生上线 qualityGate。"""
    metrics = evaluation._marginStudyMetrics(results)
    # 扩充题仍是 draft；分开统计，避免较容易的新题掩盖旧题退化。
    # 沿用同一指标口径，空 cohort 的 precision/recall 保持 None。
    metrics["cohorts"] = {
        "oldCalibration": evaluation._marginStudyMetrics([
            result for result in results if "expandedCalibration" not in result["subsets"]
        ]),
        "expandedCalibration": evaluation._marginStudyMetrics([
            result for result in results if "expandedCalibration" in result["subsets"]
        ]),
    }
    metrics["subsets"] = {
        subset: evaluation._marginStudyMetrics([
            result for result in results if subset in result["subsets"]
        ])
        for subset in sorted({subset for result in results for subset in result["subsets"]})
    }
    return metrics


def studyScores(cases: list[dict], records: dict, currentFloor: float | None) -> dict:
    """给出样本内与按 groupID 隔离的五折结果，并披露候选事实复用。

    分组分配只依赖 groupID 的散列，不读取标签或分数。同组问法不跨折，
    但背景事实可能跨组共享，因此不能视为事实独立或新的盲测。
    """
    groups = sorted({case["groupID"] for case in cases}, key=lambda value: hashlib.sha256(value.encode()).hexdigest())
    if len(groups) < FOLD_COUNT:
        raise evaluation.EvaluationError("实验至少需要五个独立 groupID")
    folds = {group: index % FOLD_COUNT for index, group in enumerate(groups)}
    thresholds = chooseThresholds(cases, records, currentFloor)
    trainingResults = replayCases(cases, records, thresholds, currentFloor)
    validationResults = []
    foldReports = []
    # ID 在旧 fixture 中可能跨场景复用；连同正文识别同一候选，另报正文
    # 重叠以暴露换 ID 复用的事实。这里只统计实际可检索的 contextual 候选。
    candidatesByCase = {
        case["caseID"]: {
            (memory["id"], memory["content"].strip())
            for memory in evaluation._contextualCandidates(evaluation._scopeCandidates(case))
        }
        for case in cases
    }
    for fold in range(FOLD_COUNT):
        train = [case for case in cases if folds[case["groupID"]] != fold]
        test = [case for case in cases if folds[case["groupID"]] == fold]
        foldThresholds = chooseThresholds(train, records, currentFloor)
        validationResults.extend(replayCases(test, records, foldThresholds, currentFloor))
        trainCandidates = set().union(*(candidatesByCase[case["caseID"]] for case in train))
        testCandidates = set().union(*(candidatesByCase[case["caseID"]] for case in test))
        foldReports.append({
            "fold": fold,
            "trainCaseCount": len(train),
            "testCaseCount": len(test),
            "trainGroupIDs": sorted({case["groupID"] for case in train}),
            "testGroupIDs": sorted({case["groupID"] for case in test}),
            "sharedCandidateMemoryCount": len(trainCandidates & testCandidates),
            "sharedCandidateContentCount": len(
                {content for memoryID, content in trainCandidates}
                & {content for memoryID, content in testCandidates}
            ),
            "thresholds": foldThresholds,
        })
    return {
        "assistedCurrentFloor": currentFloor,
        "thresholds": thresholds,
        "inSample": summarizeResults(trainingResults),
        "groupValidation": summarizeResults(validationResults),
        "folds": foldReports,
        "validationNote": (
            "按 groupID 隔离问法，同组场景不代表独立样本；候选背景可能跨组复用。"
            "sharedCandidateMemoryCount 按 ID+正文计数，sharedCandidateContentCount 按正文计数，"
            "均只含可检索的 contextual 候选；重叠计数不参与阈值选择。"
            "候选池实验的重叠计数仍针对缩池前候选库，不代表 top-k 之后的交集。"
            "新增 groupID 会改变轮转分折，跨数据版本的验证差异也可能来自分折变化。"
            "这是开发集分组验证，不是事实独立验证或新的盲测。"
        ),
        "cases": trainingResults,
        "validationCases": validationResults,
    }


def main() -> int:
    """运行少量预先声明的对照；输入与正式配置均不可被报告覆盖。"""
    parser = argparse.ArgumentParser(description="Memory calibration-only 检索改进对照")
    parser.add_argument("--cases", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--model-dir", default=LLM_MEMORY_MODEL_DIR)
    parser.add_argument("--manifest", default=LLM_MEMORY_MODEL_MANIFEST_PATH)
    parser.add_argument("--reranker-manifest")
    parser.add_argument("--reranker-dir")
    parser.add_argument("--reranker-runtime-profile", choices=RERANKER_RUNTIME_PROFILES, default="default")
    parser.add_argument("--shortlist", type=int, default=RERANKER_SHORTLIST_SIZE)
    parser.add_argument("--candidate-study", action="store_true", help="固定 top-8/16/32 候选覆盖与 dense 准入对照，仅 baseline")
    parser.add_argument("--two-stage-reranker", action="store_true", help="union 候选统一成对准入，逐对推理并检查 512 MiB 预算")
    parser.add_argument("--variants", nargs="+", choices=ENCODING_VARIANTS, default=list(ENCODING_VARIANTS))
    args = parser.parse_args()
    encoder = None
    reranker = None
    process = evaluation.psutil.Process()
    rssBefore = evaluation._safeRSS(process)
    rssSamples = [rssBefore]
    phase = "inputs"
    try:
        if args.reranker_runtime_profile != "default" and not args.two_stage_reranker:
            raise evaluation.EvaluationError("低内存重排配置仅用于受预算限制的两阶段实验")
        if args.candidate_study and (args.variants != ["baseline"] or args.reranker_manifest):
            raise evaluation.EvaluationError("候选研究需要 baseline，不能同时启用旧重排实验")
        if args.two_stage_reranker and (not args.reranker_manifest or args.candidate_study):
            raise evaluation.EvaluationError("两阶段实验需要成对模型，且与候选网格分开运行")
        # 原子写入先打开 .tmp；临时路径同样不能碰输入或模型，否则最终
        # 路径虽安全，写报告仍可能截断输入并在替换时将其移走。
        outputFile = Path(args.output)
        outputPaths = {
            outputFile.resolve(),
            outputFile.with_suffix(outputFile.suffix + ".tmp").resolve(),
        }
        protectedPaths = {Path(path).resolve() for path in (args.cases, args.manifest, LLM_MEMORY_CALIBRATION_PATH, LLM_MEMORY_MODEL_MANIFEST_PATH)}
        if outputPaths & protectedPaths:
            raise evaluation.EvaluationError("实验报告不能覆盖输入或正式配置")
        modelRoot = Path(args.model_dir).resolve()
        if any(path == modelRoot or modelRoot in path.parents for path in outputPaths):
            raise evaluation.EvaluationError("实验报告不能写入模型产物目录")
        if bool(args.reranker_manifest) != bool(args.reranker_dir):
            raise evaluation.EvaluationError("重排 manifest 与模型目录必须同时指定")
        if args.reranker_manifest:
            if args.variants != ["baseline"] or args.shortlist <= 0:
                raise evaluation.EvaluationError("重排对照只支持 baseline 和正整数 shortlist")
            rerankerRoot = Path(args.reranker_dir).resolve()
            if (Path(args.reranker_manifest).resolve() in outputPaths
                    or any(path == rerankerRoot or rerankerRoot in path.parents for path in outputPaths)):
                raise evaluation.EvaluationError("实验报告不能覆盖重排模型或清单")
        cases = loadCalibrationCases(args.cases)
        # 输出路径已校验才允许写中止报告。先绑定输入摘要，后续超预算时
        # 直接保留证据，不加载第二个模型或将部分结果伪装成完成的评估。
        abortInputs = {
            "calibrationSha256": evaluation._casesDigest(cases),
            "modelManifest": str(args.manifest),
            "modelManifestSha256": hashlib.sha256(Path(args.manifest).read_bytes()).hexdigest(),
            "rerankerManifest": args.reranker_manifest,
            "rerankerRuntimeProfile": args.reranker_runtime_profile,
            "rerankerManifestSha256": (
                hashlib.sha256(Path(args.reranker_manifest).read_bytes()).hexdigest()
                if args.reranker_manifest else None
            ),
        }
        rerankerOptions = {} if args.reranker_runtime_profile == "default" else {"runtimeProfile": args.reranker_runtime_profile}
        phase = "dense-load"
        if args.two_stage_reranker:
            checkPairStudyMemory(process, rssSamples)
        encoder = StudyEncoder(modelDir=args.model_dir, manifestPath=args.manifest)
        if args.two_stage_reranker:
            checkPairStudyMemory(process, rssSamples)
        if args.reranker_manifest and not args.two_stage_reranker:
            reranker = StudyReranker(args.reranker_dir, args.reranker_manifest, **rerankerOptions)
        rssSamples.append(evaluation._safeRSS(process))
        trials = []
        for variant in dict.fromkeys(args.variants):
            started = time.perf_counter()
            encoder.variant = variant
            # 表示变化后不得复用上一组向量；一组内的多种门槛共享相同分数，
            # 避免额外编码给实验引入耗时与数值差异。
            scorer = evaluation.EncoderSemanticScorer(encoder)
            if reranker is not None:
                scorer = StudyPairScorer(scorer, reranker, args.shortlist)
            phase = "dense-scoring"
            records = collectScores(cases, scorer)
            if args.two_stage_reranker:
                checkPairStudyMemory(process, rssSamples)
            candidateTrials = studyCandidatePools(cases, records) if args.candidate_study else None
            coverage = shortlistCoverage(cases, records) if reranker is not None else None
            rssSamples.append(evaluation._safeRSS(process))
            scorer.clear()
            if args.two_stage_reranker:
                # dense 先释放，再加载 pair；本次离线峰值不能替代线上双模型
                # 常驻验收。统一成对准入关闭词面的独立放行路径。
                encoder.close()
                phase = "pair-load"
                checkPairStudyMemory(process, rssSamples)
                reranker = StudyReranker(args.reranker_dir, args.reranker_manifest, **rerankerOptions)
                checkPairStudyMemory(process, rssSamples)
                phase = "pair-scoring"
                records, coverage = collectCandidatePairScores(
                    cases, records, reranker, encoder, args.shortlist,
                    process=process, rssSamples=rssSamples,
                )
            # 成对模型分数不是余弦相似度，不挪用余弦 floor 网格。此次只比较
            # 一套预先确定的 shortlist 规则，避免同时改变多个因素。
            phase = "validation"
            for currentFloor in ((None,) if reranker is not None else ASSISTED_CURRENT_FLOORS):
                trials.append({"encoding": variant, **studyScores(cases, records, currentFloor)})
            if args.two_stage_reranker:
                checkPairStudyMemory(process, rssSamples)
            print(f"完成 {variant}: {time.perf_counter() - started:.2f}s", flush=True)
        report = {
            "schemaVersion": 1,
            "reportType": "retrieval-improvement-study",
            "status": "experimental",
            "productionEligible": False,
            "split": "calibration",
            "calibrationSha256": evaluation._casesDigest(cases),
            "modelRevision": encoder._manifest["revision"],
            "modelManifest": str(args.manifest),
            "modelManifestSha256": hashlib.sha256(Path(args.manifest).read_bytes()).hexdigest(),
            "baselineEncodingVersion": encoder._manifest["encodingVersion"],
            "rssBytes": {
                "before": rssBefore,
                "observedMaximum": max((value for value in rssSamples if value is not None), default=None),
                "processPeakWorkingSet": getattr(process.memory_info(), "peak_wset", None),
                "note": "本次进程的全量 RSS，不只模型权重；observedMaximum 为阶段采样，可能漏掉瞬时峰值。开发机数据不代表目标机验收。",
            },
            "reranker": None if reranker is None else {
                "repository": reranker.manifest["repository"],
                "runtimeProfile": args.reranker_runtime_profile,
                "revision": reranker.manifest["revision"],
                "manifestSha256": hashlib.sha256(Path(args.reranker_manifest).read_bytes()).hexdigest(),
                "shortlistSizePerQuery": args.shortlist,
                "candidateStrategy": "union-per-channel" if args.two_stage_reranker else "dense-per-query",
                "lexicalIndependentAdmission": not args.two_stage_reranker,
                "inferenceBatchSize": 1 if args.two_stage_reranker else args.shortlist,
                "memoryLimitBytes": PAIR_STUDY_MEMORY_LIMIT if args.two_stage_reranker else None,
                "encoderReleasedBeforePairLoad": args.two_stage_reranker,
                "pairCount": reranker.pairCount,
                "shortlistCoverage": coverage,
                "queryBatchSecondsP95": evaluation._percentile(reranker.timings, 95),
                "timingUnit": "single-pair" if args.two_stage_reranker else "query-shortlist-batch",
                "note": "重排耗时不含 embedding、SQLite、解密；RSS 来自开发机进程，不是目标机验收。",
            },
            "trials": trials,
            "candidateStudy": None if not args.candidate_study else {
                "gate": "dense-base-absolute-thresholds",
                "note": "k 为每通道预算；候选覆盖是准入前上限，不是最终召回。BM25 不能绕过第二阶段，第二阶段仍是相似度基线而非答案支持模型。所有网格共享同一分组；重复比较不是独立证据，也不批准上线。",
                "trials": candidateTrials,
            },
            "note": "仅校准集与分组交叉验证，不是新盲测，不批准 calibration。",
        }
        evaluation._writeReport(report, args.output)
        return 0
    except PairStudyMemoryError as exc:
        # 中止报告没有质量成绩或阈值；Windows 峰值可追溯到本进程更早阶段，
        # phase 是发现超限的位置，不保证就是产生峰值的那一次分配。
        failure = {
            "schemaVersion": 1,
            "reportType": "retrieval-improvement-study",
            "status": "aborted",
            "reason": "memory-budget-exceeded",
            "productionEligible": False,
            "split": "calibration",
            **abortInputs,
            "phase": phase,
            "candidateStrategy": "union-per-channel",
            "shortlistSizePerChannel": args.shortlist,
            "completedPairCount": 0 if reranker is None else reranker.pairCount,
            "rssBytes": {
                "atAbort": exc.rssBytes,
                "observedMaximum": max(value for value in rssSamples if value is not None),
                "processPeakWorkingSet": exc.processPeakWorkingSet,
                "limit": exc.limitBytes,
            },
            "trials": [],
            "note": "资源超限后停止；没有完整质量结果。phase 仅为发现阶段，内存检查不是操作系统硬限额。",
        }
        try:
            evaluation._writeReport(failure, args.output)
        except OSError as reportError:
            print(f"中止报告写入失败: {reportError}", file=sys.stderr)
        print(f"检索改进实验失败: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        print(f"检索改进实验失败: {exc}", file=sys.stderr)
        return 1
    finally:
        if reranker is not None:
            reranker.close()
        if encoder is not None:
            encoder.close()


if __name__ == "__main__":
    raise SystemExit(main())
