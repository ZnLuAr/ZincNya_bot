"""
utils/llm/knowledge/

知识库模块：
    - database: 知识条目 CRUD + BM25 检索
    - loader: Markdown 文件加载与索引刷新
    - tokenizer: 中英文分词与 BM25 评分
"""

from .database import (
    initDatabase,
    retrieveKnowledge,
    upsertKnowledgeEntry,
    deleteEntriesBySource,
    getKnowledgeEntries,
    getKnowledgeStats,
    buildKnowledgeContextBlock,
    rebuildTokenCacheFromDB,
)

from .loader import (
    reindexKnowledgeBase,
    reindexOnStartup,
)

__all__ = [
    "initDatabase",
    "retrieveKnowledge",
    "upsertKnowledgeEntry",
    "deleteEntriesBySource",
    "getKnowledgeEntries",
    "getKnowledgeStats",
    "buildKnowledgeContextBlock",
    "rebuildTokenCacheFromDB",
    "reindexKnowledgeBase",
    "reindexOnStartup",
]