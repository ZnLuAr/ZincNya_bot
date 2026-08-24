"""
tests/utils/llm/knowledge/test_database.py

knowledge_entries 存储层（utils/llm/knowledge/database.py）：upsert 幂等
（source_file + title 唯一键）、删除按源文件、分类过滤、统计、上下文块格式。
真实临时 DB，不 mock。
"""

import pytest

from utils.llm.knowledge import database as kdb
from utils.llm.knowledge.database import (
    buildKnowledgeContextBlock,
    deleteEntriesBySource,
    getKnowledgeEntries,
    getKnowledgeStats,
    upsertKnowledgeEntry,
)



@pytest.fixture
def db(tmp_path, monkeypatch):
    from utils.core.database import Database
    fake = Database(str(tmp_path / "know.db"), "TestKnowledge")
    monkeypatch.setattr(kdb, "knowledgeDB", fake)
    kdb.initDatabase()
    return fake



async def _seed(db):
    a = await upsertKnowledgeEntry("interests", "猫", "用户喜欢猫", ["宠物"], "a.md", "h1")
    b = await upsertKnowledgeEntry("style", "语气", "活泼", [], "a.md", "h1")
    c = await upsertKnowledgeEntry("interests", "编程", "Python 和 Rust", ["技术"], "b.md", "h2")
    return a, b, c



class TestUpsert:
    async def test_insert_returns_ids(self, db):
        a, b, c = await _seed(db)
        assert len({a, b, c}) == 3

    async def test_same_source_title_updates_not_duplicates(self, db):
        """source_file + title 唯一键：重跑 upsert 是更新不是新插入"""
        await _seed(db)
        newID = await upsertKnowledgeEntry("interests", "猫", "用户非常喜欢猫", ["宠物"], "a.md", "h3")
        entries = await getKnowledgeEntries()
        assert len(entries) == 3
        cat = next(e for e in entries if e["title"] == "猫")
        assert cat["content"] == "用户非常喜欢猫"
        assert cat["id"] == newID

    async def test_same_title_different_source_both_kept(self, db):
        await upsertKnowledgeEntry("x", "同名", "内容A", [], "a.md", "h")
        await upsertKnowledgeEntry("x", "同名", "内容B", [], "b.md", "h")
        assert len(await getKnowledgeEntries()) == 2



class TestDelete:
    async def test_delete_by_source(self, db):
        await _seed(db)
        removed = await deleteEntriesBySource("a.md")
        assert removed == 2
        remaining = await getKnowledgeEntries()
        assert [e["title"] for e in remaining] == ["编程"]

    async def test_delete_nonexistent_zero(self, db):
        assert await deleteEntriesBySource("nope.md") == 0



class TestGetEntries:
    async def test_category_filter(self, db):
        await _seed(db)
        interests = await getKnowledgeEntries(category="interests")
        assert {e["title"] for e in interests} == {"猫", "编程"}

    async def test_fields_shape(self, db):
        await _seed(db)
        e = (await getKnowledgeEntries())[0]
        for key in ("id", "category", "title", "content", "tags", "source_file"):
            assert key in e



class TestStats:
    async def test_stats_counts(self, db):
        await _seed(db)
        stats = await getKnowledgeStats()
        assert stats["total"] == 3
        assert stats["enabled"] == 3
        assert stats["byCategory"]["interests"] == 2
        assert stats["source_files"] == 2



class TestContextBlock:
    def test_block_format(self):
        block = buildKnowledgeContextBlock([
            {"category": "interests", "title": "猫", "content": "喜欢猫", "tags": ["宠物"]},
            {"category": "style", "title": "语气", "content": "活泼", "tags": []},
        ])
        assert "<TRUSTED_KNOWLEDGE>" in block
        assert "猫" in block and "活泼" in block

    def test_empty_entries_empty_block(self):
        assert buildKnowledgeContextBlock([]) == ""
