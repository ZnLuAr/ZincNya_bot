"""
tests/utils/llm/knowledge/test_retriever.py

BM25 检索（utils/llm/knowledge/retriever.py）：关键词命中排序、limit/minScore
边界、空库。真实临时 DB + 每例重建 token cache（retriever 有模块级缓存）。
"""

import pytest

from utils.llm.knowledge import database as kdb
from utils.llm.knowledge import retriever
from utils.llm.knowledge.database import upsertKnowledgeEntry
from utils.llm.knowledge.retriever import rebuildTokenCacheFromDB, retrieveKnowledge



@pytest.fixture
def db(tmp_path, monkeypatch):
    from utils.core.database import Database
    fake = Database(str(tmp_path / "know.db"), "TestKnowledge")
    # retriever 用的是 from .database import knowledgeDB 的值副本——两处都要 patch
    monkeypatch.setattr(kdb, "knowledgeDB", fake)
    monkeypatch.setattr(retriever, "knowledgeDB", fake)
    kdb.initDatabase()
    yield fake



async def _seed(db):
    await upsertKnowledgeEntry("interests", "猫科", "用户对猫科动物非常感兴趣，尤其是大型猫科", ["动物"], "a.md", "h1")
    await upsertKnowledgeEntry("tech", "Python", "用户主要写 Python，也用 Rust", ["编程"], "a.md", "h1")
    await upsertKnowledgeEntry("tech", "服务器", "服务器是 Debian，跑 systemd", ["运维"], "b.md", "h2")
    await rebuildTokenCacheFromDB()



class TestRetrieve:
    async def test_keyword_match_returns_scored(self, db):
        await _seed(db)
        results = await retrieveKnowledge("猫科动物", limit=3, minScore=0.0)
        assert results
        assert "score" in results[0]
        # 分数降序
        scores = [r["score"] for r in results]
        assert scores == sorted(scores, reverse=True)
        # 查询词与命中条目相关（猫科 entry 必须在结果里）
        assert any(r["title"] == "猫科" for r in results)

    async def test_python_query_hits_python_entry(self, db):
        await _seed(db)
        results = await retrieveKnowledge("Python 编程", limit=3, minScore=0.0)
        assert any(r["title"] == "Python" for r in results)

    async def test_limit_bounds_results(self, db):
        await _seed(db)
        results = await retrieveKnowledge("的", limit=1, minScore=0.0)
        assert len(results) <= 1

    async def test_high_min_score_filters_all(self, db):
        await _seed(db)
        assert await retrieveKnowledge("猫科", limit=3, minScore=999) == []

    async def test_empty_db_returns_empty(self, db):
        await rebuildTokenCacheFromDB()
        assert await retrieveKnowledge("随便什么", limit=3, minScore=999) == []



class TestCache:
    async def test_rebuild_picks_up_new_entries(self, db):
        await rebuildTokenCacheFromDB()
        assert await retrieveKnowledge("Rust", limit=3, minScore=0.0) == []
        await upsertKnowledgeEntry("tech", "Rust", "偶尔写 Rust", [], "c.md", "h3")
        await rebuildTokenCacheFromDB()
        results = await retrieveKnowledge("Rust", limit=3, minScore=0.0)
        assert any(e["title"] == "Rust" for e in results)
