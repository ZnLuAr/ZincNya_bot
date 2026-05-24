"""
utils/llm/knowledge/tokenizer.py

中英文混合分词与 BM25 评分实现。

分词规则：
    - 英文：按空格/标点切分为单词，转小写
    - 中文：按字符 2-gram 切分，同时保留 1-gram（单字）
    - 数字：按整体保留（"2026" 不切分）
    - emoji / 特殊符号：丢弃
"""

import re
from collections import Counter


def tokenize(text: str) -> list[str]:
    """
    中英文混合分词。

    返回 token 列表（已去重、转小写）。
    """
    if not text:
        return []

    text = text.lower()
    tokens = []

    # 提取英文单词（连续 ASCII 字母数字）
    english_words = re.findall(r'[a-z0-9]+', text)
    tokens.extend(english_words)

    # 提取数字（整体保留）
    numbers = re.findall(r'\d+', text)
    tokens.extend(numbers)

    # 提取中文字符
    chinese_chars = re.findall(r'[一-鿿]+', text)
    for segment in chinese_chars:
        # 1-gram（单字）
        tokens.extend(list(segment))
        # 2-gram
        if len(segment) >= 2:
            for i in range(len(segment) - 1):
                tokens.append(segment[i:i+2])

    # 去重但保留顺序（用 dict.fromkeys）
    return list(dict.fromkeys(tokens))


def _bm25(queryTokens: list[str], docTokens: list[str], avgDocLen: float = 50.0, k1: float = 1.5, b: float = 0.75) -> float:
    """
    BM25 评分（简化版，单文档评分）。

    参数：
        queryTokens: 查询 token 列表
        docTokens: 文档 token 列表
        avgDocLen: 全库平均文档长度（用于长度归一化）
        k1: BM25 参数（词频饱和度）
        b: BM25 参数（长度归一化强度）

    返回：
        BM25 分数（越高越相关）
    """
    if not queryTokens or not docTokens:
        return 0.0

    docLen = len(docTokens)
    docFreq = Counter(docTokens)
    score = 0.0

    for token in queryTokens:
        if token not in docFreq:
            continue

        tf = docFreq[token]
        # BM25 TF 部分（带饱和）
        numerator = tf * (k1 + 1)
        denominator = tf + k1 * (1 - b + b * (docLen / avgDocLen))
        score += numerator / denominator

    return score


def computeAvgDocLen(allDocTokens: list[list[str]]) -> float:
    """计算全库平均文档长度（用于 BM25 归一化）。"""
    if not allDocTokens:
        return 50.0
    totalLen = sum(len(tokens) for tokens in allDocTokens)
    return totalLen / len(allDocTokens)
