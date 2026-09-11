"""
utils/llm/memory/lexical.py

LLM Memory 专用的中文 2-gram 与字段化 BM25 评分。

该层是 hybrid 检索的词面通道评分器，不持有任何长期状态：每次调用对
`retrieval.py` 传入的完整 contextual 候选集现算 BM25，无缓存、无落盘。
查询侧与文档侧共用同一分词器（`tokenizeMemoryText`），保证 token 可对上。

与 knowledge 检索的分词/评分刻意不复用——memory 面对的是情境应答而非
话题查找，行为契约由 `tests/utils/llm/memory/test_memoryLexical.py` 单独锁定。
"""

import math
import re
import unicodedata
from collections import Counter

from config import LLM_MEMORY_BM25_B, LLM_MEMORY_BM25_K1


# 可索引字符/词 的限定，包含生僻字、常用汉字、CJK 兼容区及 ASCII
_TOKEN_PATTERN = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+|[a-z0-9]+"
)




def tokenizeMemoryText(text: str) -> list[str]:
    """
    生成中文相邻 2-gram 与完整 ASCII 英数词，不产生中文单字。

    流程：NFKC 归一化 + 转小写 → 按 `_TOKEN_PATTERN` 切片段 →
    汉字片段切相邻 2-gram、英数片段整词保留。token 保留重复出现
    （文档侧 Counter 依赖真实词频算 TF），查询侧去重由调用方负责。

    不产生单字：单汉字歧义过大（「机」同时命中手机/飞机/机会），
    2-gram 是中文 BM25 的标准做法。因此，单字查询不能得到信息，
    这是预期行为而非缺陷。
    """
    normalized = unicodedata.normalize("NFKC", str(text or "")).lower()
    tokens = []
    for match in _TOKEN_PATTERN.finditer(normalized):
        segment = match.group(0)
        if re.fullmatch(r"[a-z0-9]+", segment):
            tokens.append(segment)
            continue
        tokens.extend(
            segment[index:index + 2]
            for index in range(len(segment) - 1)
        )
    return tokens


def _fieldScores(
    queryTokens: set[str],
    documents: list[Counter],
    *,
    k1: float,
    b: float,
) -> list[float]:
    """
    对一个字段（content 或 tags）集合计算 BM25；返回顺序与 `documents` 一致。

    IDF 用当前传入候选集的文档频率——不维护全局词表，因为词面通道
    每次都对完整 contextual 候选重算，语料即候选集本身。
    """
    if not queryTokens or not documents:
        return [0.0] * len(documents)

    documentCount = len(documents)
    fieldLengths = [sum(counter.values()) for counter in documents]
    averageLength = sum(fieldLengths) / documentCount
    # 全空字段（如候选记忆全都没有 tags）时平均长度为 0，按 1 兜底防除零
    if averageLength <= 0:
        averageLength = 1.0

    documentFrequencies = {
        token: sum(1 for counter in documents if counter.get(token, 0) > 0)
        for token in queryTokens
    }
    idf = {
        token: math.log(
            1 + (documentCount - frequency + 0.5) / (frequency + 0.5)
        )
        for token, frequency in documentFrequencies.items()
    }

    scores = []
    for counter, fieldLength in zip(documents, fieldLengths):
        score = 0.0
        for token in queryTokens:
            frequency = counter.get(token, 0)
            if frequency <= 0:
                continue
            numerator = frequency * (k1 + 1)
            denominator = frequency + k1 * (
                1 - b + b * fieldLength / averageLength
            )
            score += idf[token] * numerator / denominator
        scores.append(score)
    return scores




def scoreLexicalCandidates(
    queryText: str,
    candidates: list[dict],
    *,
    k1: float = LLM_MEMORY_BM25_K1,
    b: float = LLM_MEMORY_BM25_B,
) -> dict[int, float]:
    """
    按 content + 2 × tags 计算完整候选集的 BM25 分数，返回 {memoryID: 分数}。

    字段加权：tags 是人工/模型给的分类词，比正文更接近「这条记忆讲什么」，
    命中时信号更强，故权重 2 倍。两字段各自独立算 BM25（独立平均长度）
    后线性合并。retrievalHint 刻意不参与——hint 是宽泛的扩展词
    （话题/别名），给它词面资格会让「顺嘴一提」也触发误召回；
    它只去增强语义索引。零分候选不进返回值（调用方按缺席处理）。

    返回为空 dict 等价于「词面通道本轮无合格证据」，由上层降级/弃权，
    不在这里猜测阈值（校准阈值在 `retrieval.py` 的 calibration 侧）。
    """
    queryTokens = set(tokenizeMemoryText(queryText))
    if not queryTokens or not candidates:
        return {}

    contentDocuments = [
        Counter(tokenizeMemoryText(memory.get("content", "")))
        for memory in candidates
    ]
    tagDocuments = [
        Counter(tokenizeMemoryText(" ".join(memory.get("tags") or [])))
        for memory in candidates
    ]
    contentScores = _fieldScores(queryTokens, contentDocuments, k1=k1, b=b)
    tagScores = _fieldScores(queryTokens, tagDocuments, k1=k1, b=b)

    scores = {}
    for memory, contentScore, tagScore in zip(
        candidates, contentScores, tagScores,
    ):
        score = contentScore + 2.0 * tagScore
        if score > 0:
            scores[int(memory["id"])] = score
    return scores
