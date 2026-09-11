"""测试 memory 专属分词与字段化 BM25。"""

from collections import Counter

from utils.llm.memory.lexical import scoreLexicalCandidates, tokenizeMemoryText




def test_tokenizerUsesChineseBigramsWithoutSingles():
    assert tokenizeMemoryText("今天 好") == ["今天"]
    assert tokenizeMemoryText("天") == []


def test_tokenizerNormalizesAsciiWords():
    assert tokenizeMemoryText("Python 3.13 PYTHON") == ["python", "3", "13", "python"]


def test_repeatedTermKeepsRealTermFrequency():
    candidates = [
        {"id": 1, "content": "咖啡咖啡咖啡", "tags": []},
        {"id": 2, "content": "咖啡", "tags": []},
    ]

    scores = scoreLexicalCandidates("咖啡", candidates)

    assert Counter(tokenizeMemoryText(candidates[0]["content"]))["咖啡"] == 3
    assert scores[1] > 0 and scores[2] > 0


def test_tagsReceiveWeightButHintCannotQualify():
    candidates = [
        {"id": 1, "content": "无关正文", "tags": ["研究生备考"]},
        {
            "id": 2,
            "content": "无关正文",
            "tags": [],
            "retrievalHint": "研究生备考",
        },
    ]

    scores = scoreLexicalCandidates("研究生备考", candidates)

    assert scores[1] > 0
    assert 2 not in scores


def test_singleChineseCharacterCannotTriggerCandidate():
    candidates = [{"id": 1, "content": "天气很好", "tags": []}]

    assert scoreLexicalCandidates("天", candidates) == {}


def test_emptyFieldsDoNotProduceNanOrErrors():
    candidates = [
        {"id": 1, "content": "", "tags": ["咖啡偏好"]},
        {"id": 2, "content": "", "tags": []},
    ]

    scores = scoreLexicalCandidates("咖啡", candidates)

    assert scores[1] > 0
    assert 2 not in scores
