"""
utils/llm/knowledge/retriever.py

知识库检索模块

提供：
    - BM25 关键词检索（含 token 缓存）
    - Token 缓存管理
"""

import json

from .database import knowledgeDB
from .tokenizer import tokenize, _bm25, computeAvgDocLen


# Token 缓存：{entry_id: {"title": [...], "content": [...], "tags": [...]}}
_tokenCache: dict[int, dict[str, list[str]]] = {}
_avgDocLen: float = 50.0


# ========== Category 意图匹配触发词（5-8 个精准词）==========
_CATEGORY_TRIGGERS = {
    "identity": {
        "keywords": ["你是谁", "介绍", "自己", "你叫", "名字", "身份"],
        "bonus": 5.0,
    },
    "interests": {
        "keywords": ["喜欢", "爱好", "偏好", "兴趣", "讨厌"],
        "bonus": 3.0,
    },
    "style": {
        "keywords": ["说话", "风格", "口头禅", "语气", "习惯"],
        "bonus": 3.0,
    },
    "pitfalls": {
        "keywords": ["反例", "不要", "避免", "禁忌"],
        "bonus": 3.0,
    },
}




async def _loadAllEnabled() -> list[dict]:
    """加载所有启用的知识条目（含 tags 反序列化）"""
    def _query(conn):
        cursor = conn.execute(
            "SELECT id, category, title, content, tags_json, priority FROM knowledge_entries WHERE enabled = 1"
        )
        rows = cursor.fetchall()
        result = []
        for row in rows:
            entry = dict(row)
            entry["tags"] = json.loads(entry["tags_json"])
            result.append(entry)
        return result

    return await knowledgeDB.run(_query)




def _rebuildTokenCache(entries: list[dict]):
    """重建 token 缓存（启动 / reindex 时调用）"""
    global _tokenCache, _avgDocLen
    _tokenCache.clear()

    allDocTokens = []
    for entry in entries:
        entryID = entry["id"]
        titleTokens = tokenize(entry["title"])
        contentTokens = tokenize(entry["content"])
        tagsTokens = tokenize(" ".join(entry["tags"]))

        _tokenCache[entryID] = {
            "title": titleTokens,
            "content": contentTokens,
            "tags": tagsTokens,
        }

        # 用于计算平均文档长度（三个字段拼接）
        allDocTokens.append(titleTokens + contentTokens + tagsTokens)

    _avgDocLen = computeAvgDocLen(allDocTokens) if allDocTokens else 50.0




async def retrieveKnowledge(query: str, limit: int = 3, minScore: float = 0.5) -> list[dict]:
    """
    BM25 关键词检索 + 意图匹配优化（Phase 1.3）

    参数：
        query: 查询字符串
        limit: 最多返回条数
        minScore: 最低分数阈值

    返回：
        [{"id", "category", "title", "content", "tags", "priority", "score"}, ...]
    """
    if not query.strip():
        return []

    queryTokens = tokenize(query)
    if not queryTokens:
        return []

    entries = await _loadAllEnabled()

    # 如果缓存为空，重建
    if not _tokenCache:
        _rebuildTokenCache(entries)

    scored = []
    for entry in entries:
        entryID = entry["id"]
        if entryID not in _tokenCache:
            continue

        cached = _tokenCache[entryID]

        # 三个字段分别评分，权重：tags(2.0) > title(1.5) > content(1.0)
        score = (
            _bm25(queryTokens, cached["tags"], _avgDocLen) * 2.0
            + _bm25(queryTokens, cached["title"], _avgDocLen) * 1.5
            + _bm25(queryTokens, cached["content"], _avgDocLen) * 1.0
        )

        # 新增：category 意图匹配加成
        categoryBonus = 0.0
        category = entry["category"]

        if category in _CATEGORY_TRIGGERS:
            trigger = _CATEGORY_TRIGGERS[category]
            if any(kw in query for kw in trigger["keywords"]):
                categoryBonus = trigger["bonus"]

        score += categoryBonus
        score += entry["priority"] * 1.0  # 保持原有 priority 加成

        if score >= minScore:
            scored.append({**entry, "score": score})

    scored.sort(key=lambda x: -x["score"])
    return scored[:limit]




async def rebuildTokenCacheFromDB():
    """从数据库重建 token 缓存（reindex 后调用）"""
    entries = await _loadAllEnabled()
    _rebuildTokenCache(entries)
