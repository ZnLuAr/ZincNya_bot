"""
tests/utils/llm/memory/test_retrieval.py

测试 utils/llm/memory/database.py 的检索与呈现逻辑。

验证：
    ① buildMemoryContextBlock 输出含 w=（不含 p=）
    ② 输出仍含 id= / src=（inferred 可操作性不破坏）
    ③ 块头含相关性门控措辞
    ④ retrieveMemories 同 scope 溢出（放宽后低优先级入池）
    ⑤ retrieveMemories scope 专属度兜底（priority 打平时 session>global）
    ⑥ 异常路径降级（mock 抛异常返 []，脏数据不拖垮 sort）
"""

import pytest
from datetime import datetime
from unittest.mock import patch, AsyncMock

from utils.llm.memory.database import (
    buildMemoryContextBlock,
    retrieveMemories,
)


# ============================================================================
# buildMemoryContextBlock() 呈现层测试
# ============================================================================

def test_build_memory_context_block_output_format():
    """① 输出含 w=（不含 p=）、② 含 id=/src=、③ 块头有门控措辞"""
    memories = [
        {
            "id": 42,
            "scope_type": "global",
            "scope_id": "global",
            "content": "用户偏好简体中文",
            "tags": ["偏好"],
            "priority": 10,
            "source": "manual",
        },
        {
            "id": 57,
            "scope_type": "chat",
            "scope_id": "123456",
            "content": "这个群主要讨论编程",
            "tags": [],
            "priority": 0,
            "source": "inferred",
        },
    ]

    result = buildMemoryContextBlock(memories)

    # ① 不含 p=，含 w=
    assert "p=" not in result
    assert "w=10" in result
    assert "w=0" in result

    # ② 含 id= / src=
    assert "id=42" in result
    assert "id=57" in result
    assert "src=manual" in result
    assert "src=inferred" in result

    # ③ 块头含相关性门控关键词
    assert "仅在与当前对话直接相关时才引用" in result
    assert "不要为了提及而提及" in result
    assert "w= 是内部召回权重" in result


def test_build_memory_context_block_empty():
    """空列表返回空字符串"""
    assert buildMemoryContextBlock([]) == ""


# ============================================================================
# retrieveMemories() 检索逻辑测试
# ============================================================================

@pytest.mark.asyncio
async def test_retrieve_memories_same_scope_overflow():
    """④ 同 scope 溢出判别：global 有 4 条 [p3, p3, p3, p2]，
    perScopeLimit=20 全量入池，p2 入选（旧逻辑 limit=3 会砍掉 p2）"""
    with patch("utils.llm.memory.database.getMemories", new_callable=AsyncMock) as mock_get:
        # global scope 返 4 条，3×p3 + 1×p2
        mock_get.return_value = [
            {"id": 1, "priority": 3, "scope_type": "global", "scope_id": "global", "updated_at": datetime(2024, 1, 1)},
            {"id": 2, "priority": 3, "scope_type": "global", "scope_id": "global", "updated_at": datetime(2024, 1, 2)},
            {"id": 3, "priority": 3, "scope_type": "global", "scope_id": "global", "updated_at": datetime(2024, 1, 3)},
            {"id": 4, "priority": 2, "scope_type": "global", "scope_id": "global", "updated_at": datetime(2024, 1, 4)},
        ]

        result = await retrieveMemories(totalLimit=10)

        # 新逻辑：4 条全入池，p2 也入选
        assert len(result) == 4
        ids = [m["id"] for m in result]
        assert 4 in ids  # p2 的那条


@pytest.mark.asyncio
async def test_retrieve_memories_scope_rank_tiebreaker():
    """⑤ scope 专属度兜底：同 priority=2 时，chat 排在 global 之前"""
    with patch("utils.llm.memory.database.getMemories", new_callable=AsyncMock) as mock_get:
        async def _mock_get(scopeType, scopeID, enabledOnly, limit):
            if scopeType == "global":
                return [{"id": 10, "priority": 2, "scope_type": "global", "scope_id": "global", "updated_at": datetime(2024, 1, 1)}]
            if scopeType == "chat":
                return [{"id": 20, "priority": 2, "scope_type": "chat", "scope_id": "123", "updated_at": datetime(2024, 1, 1)}]
            return []

        mock_get.side_effect = _mock_get

        result = await retrieveMemories(chatID="123", totalLimit=10)

        # 同 p2，chat(rank=1) > global(rank=0)，chat 条目排前
        assert len(result) == 2
        assert result[0]["id"] == 20  # chat
        assert result[1]["id"] == 10  # global


@pytest.mark.asyncio
async def test_retrieve_memories_exception_fallback():
    """⑥ 异常路径降级：getMemories 抛异常，返回 []（不冒泡）"""
    with patch("utils.llm.memory.database.getMemories", new_callable=AsyncMock) as mock_get:
        with patch("utils.llm.memory.database.logSystemEvent", new_callable=AsyncMock):
            mock_get.side_effect = RuntimeError("DB explosion")

            result = await retrieveMemories(totalLimit=10)

            # 降级返空，不抛异常
            assert result == []


@pytest.mark.asyncio
async def test_retrieve_memories_dirty_data_does_not_crash_sort():
    """⑥ 脏数据兜底：updated_at=None 的记忆混在正常记忆中，sort 不抛 TypeError"""
    with patch("utils.llm.memory.database.getMemories", new_callable=AsyncMock) as mock_get:
        # 1 条脏数据（updated_at=None）+ 1 条正常
        mock_get.return_value = [
            {"id": 1, "priority": 1, "scope_type": "global", "scope_id": "global", "updated_at": None},
            {"id": 2, "priority": 1, "scope_type": "global", "scope_id": "global", "updated_at": datetime(2024, 1, 1)},
        ]

        result = await retrieveMemories(totalLimit=10)

        # 不抛异常，正常返回 2 条
        assert len(result) == 2
