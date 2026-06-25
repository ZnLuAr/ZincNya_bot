"""
utils/llm/knowledge/

知识库模块：
    - database: 知识条目 CRUD
    - retriever: BM25 检索
    - loader: Markdown 文件加载与索引刷新
    - tokenizer: 中英文分词与 BM25 评分
"""

from .database import (
    initDatabase,
    upsertKnowledgeEntry,
    deleteEntriesBySource,
    getKnowledgeEntries,
    getKnowledgeStats,
    buildKnowledgeContextBlock,
)

from .retriever import (
    retrieveKnowledge,
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