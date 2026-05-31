"""
tests/utils/llm/knowledge/test_tokenizer.py

测试 utils/llm/knowledge/tokenizer.py
"""

import pytest
from utils.llm.knowledge.tokenizer import tokenize, _bm25, computeAvgDocLen


# ============================================================================
# tokenize() 测试
# ============================================================================

def test_tokenize_english():
    """英文分词：按空格/标点切分，转小写"""
    result = tokenize("Hello World Python")
    assert result == ["hello", "world", "python"]


def test_tokenize_english_with_punctuation():
    """英文分词：标点符号被移除"""
    result = tokenize("Hello, World! Python?")
    assert result == ["hello", "world", "python"]


def test_tokenize_chinese():
    """中文分词：1-gram + 2-gram"""
    result = tokenize("你好世界")
    # 1-gram: 你, 好, 世, 界
    # 2-gram: 你好, 好世, 世界
    assert "你" in result
    assert "好" in result
    assert "世" in result
    assert "界" in result
    assert "你好" in result
    assert "好世" in result
    assert "世界" in result


def test_tokenize_chinese_single_char():
    """中文分词：单字只有 1-gram"""
    result = tokenize("好")
    assert result == ["好"]


def test_tokenize_mixed():
    """中英混合分词"""
    result = tokenize("Python 编程语言")
    assert "python" in result
    assert "编" in result
    assert "程" in result
    assert "语" in result
    assert "言" in result
    assert "编程" in result
    assert "程语" in result
    assert "语言" in result


def test_tokenize_numbers():
    """数字保留为整体"""
    result = tokenize("2026年5月26日")
    assert "2026" in result
    assert "5" in result
    assert "26" in result
    assert "年" in result
    assert "月" in result
    assert "日" in result


def test_tokenize_alphanumeric():
    """字母数字混合（如版本号）"""
    result = tokenize("Python3.11")
    assert "python3" in result
    assert "11" in result


def test_tokenize_empty():
    """空字符串边界"""
    assert tokenize("") == []


def test_tokenize_whitespace_only():
    """纯空格字符串"""
    assert tokenize("   \n\t  ") == []


def test_tokenize_special_chars_only():
    """纯特殊符号（应被丢弃）"""
    result = tokenize("!@#$%^&*()")
    assert result == []


def test_tokenize_emoji():
    """emoji 被丢弃"""
    result = tokenize("Hello 😊 World")
    assert result == ["hello", "world"]


def test_tokenize_deduplication():
    """去重但保留顺序"""
    result = tokenize("hello hello world")
    assert result == ["hello", "world"]


def test_tokenize_case_insensitive():
    """大小写不敏感"""
    result = tokenize("HELLO Hello hello")
    assert result == ["hello"]


# ============================================================================
# _bm25() 测试
# ============================================================================

def test_bm25_exact_match():
    """BM25：完全匹配"""
    query = ["python"]
    doc = ["python", "programming", "language"]
    score = _bm25(query, doc)
    assert score > 0


def test_bm25_partial_match():
    """BM25：部分匹配"""
    query = ["python", "java"]
    doc = ["python", "programming"]
    score = _bm25(query, doc)
    assert score > 0


def test_bm25_no_match():
    """BM25：无匹配"""
    query = ["rust"]
    doc = ["python", "java"]
    score = _bm25(query, doc)
    assert score == 0.0


def test_bm25_empty_query():
    """BM25：空查询"""
    query = []
    doc = ["python", "java"]
    score = _bm25(query, doc)
    assert score == 0.0


def test_bm25_empty_doc():
    """BM25：空文档"""
    query = ["python"]
    doc = []
    score = _bm25(query, doc)
    assert score == 0.0


def test_bm25_frequency_boost():
    """BM25：词频越高分数越高"""
    query = ["python"]
    doc1 = ["python", "java"]
    doc2 = ["python", "python", "java"]
    score1 = _bm25(query, doc1)
    score2 = _bm25(query, doc2)
    assert score2 > score1


def test_bm25_length_normalization():
    """BM25：长度归一化（长文档惩罚）"""
    query = ["python"]
    doc_short = ["python", "java"]
    doc_long = ["python"] + ["filler"] * 100

    # 短文档应该得分更高（avgDocLen=50）
    score_short = _bm25(query, doc_short, avgDocLen=50.0)
    score_long = _bm25(query, doc_long, avgDocLen=50.0)
    assert score_short > score_long


def test_bm25_multiple_query_terms():
    """BM25：多个查询词累加"""
    query = ["python", "java"]
    doc = ["python", "java", "rust"]
    score_both = _bm25(query, doc)

    score_python = _bm25(["python"], doc)
    score_java = _bm25(["java"], doc)

    # 两个词的分数应该接近单独分数之和
    assert abs(score_both - (score_python + score_java)) < 0.01


# ============================================================================
# computeAvgDocLen() 测试
# ============================================================================

def test_compute_avg_doc_len():
    """计算平均文档长度"""
    docs = [
        ["a", "b", "c"],           # len=3
        ["d", "e"],                # len=2
        ["f", "g", "h", "i", "j"]  # len=5
    ]
    avg = computeAvgDocLen(docs)
    assert avg == (3 + 2 + 5) / 3


def test_compute_avg_doc_len_empty():
    """空文档列表返回默认值"""
    assert computeAvgDocLen([]) == 50.0


def test_compute_avg_doc_len_single_doc():
    """单文档"""
    docs = [["a", "b", "c"]]
    assert computeAvgDocLen(docs) == 3.0