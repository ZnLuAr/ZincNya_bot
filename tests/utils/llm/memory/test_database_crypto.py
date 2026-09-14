"""
tests/utils/llm/memory/test_database_crypto.py

测试 utils/llm/memory/database.py 的 content 加密往返。

验证：
    - addMemory 写入后，库中 content 列是密文（非明文）。
    - 经 getMemoryByID / getMemories 读回时自动解密为明文。
    - updateMemory 更新 content 同样加密存储。
    - _decryptContent 对历史明文行有兜底（不崩）。

通过 monkeypatch 把密钥指向临时文件并重置缓存，避免触碰真实 .chatKey。
"""

import pytest
from unittest.mock import patch

import utils.core.crypto as crypto
from utils.core.crypto import decryptText
from utils.llm.memory.database import (
    addMemory,
    getEnabledContextualMemoryPage,
    getMemoryByID,
    getMemoryCounts,
    getMemories,
    updateMemory,
)


@pytest.fixture
def tmpKey(tmp_path, monkeypatch):
    """把密钥指向临时文件并清空缓存。"""
    monkeypatch.setattr(crypto, "KEY_PATH", str(tmp_path / ".chatKey"))
    monkeypatch.setattr(crypto, "_fernetCache", None)


@pytest.fixture
async def memoryDb(inMemoryDb):
    """初始化 memory_entries 表（content 为 BLOB）。"""
    conn = inMemoryDb
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS memory_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope_type TEXT NOT NULL,
            scope_id TEXT NOT NULL,
            content BLOB NOT NULL,
            tags_json TEXT NOT NULL DEFAULT '[]',
            enabled INTEGER NOT NULL DEFAULT 1,
            priority INTEGER NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT 'manual',
            mode TEXT NOT NULL DEFAULT 'contextual',
            retrieval_hint BLOB,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()
    yield conn


@pytest.fixture
def patchRun(memoryDb):
    """把 memoryDB.run 指向内存库。"""
    with patch("utils.llm.memory.database.memoryDB.run") as mock_run:
        async def _mock_run(func):
            return func(memoryDb)
        mock_run.side_effect = _mock_run
        yield memoryDb


@pytest.mark.asyncio
async def test_add_memory_stores_ciphertext(tmpKey, patchRun):
    """addMemory 写入的 content 在库中是密文。"""
    memID = await addMemory("global", None, "用户喜欢数学和编程")
    assert memID is not None

    cursor = patchRun.cursor()
    cursor.execute("SELECT content FROM memory_entries WHERE id = ?", (memID,))
    raw = cursor.fetchone()["content"]

    # 库里是密文，不等于明文；解密后还原
    assert raw != "用户喜欢数学和编程"
    assert decryptText(raw) == "用户喜欢数学和编程"


@pytest.mark.asyncio
async def test_get_memory_decrypts(tmpKey, patchRun):
    """getMemoryByID 读回时自动解密。"""
    memID = await addMemory("user", "u123", "记得提醒他喝水")
    mem = await getMemoryByID(memID)
    assert mem["content"] == "记得提醒他喝水"


@pytest.mark.asyncio
async def test_memory_counts_do_not_decrypt_rows(tmpKey, patchRun):
    await addMemory("global", None, "启用")
    await addMemory("global", None, "常驻", mode="pinned")
    await addMemory("global", None, "停用", enabled=False)

    with patch("utils.llm.memory.database._rowToMemoryDict") as mockDecode:
        counts = await getMemoryCounts()

    assert counts == {"total": 3, "enabled": 2, "contextualEnabled": 1}
    mockDecode.assert_not_called()


@pytest.mark.asyncio
async def test_contextual_index_page_excludes_pinned_and_disabled(tmpKey, patchRun):
    contextualID = await addMemory("global", None, "情境记忆")
    await addMemory("global", None, "常驻记忆", mode="pinned")
    await addMemory("global", None, "停用记忆", enabled=False)

    page = await getEnabledContextualMemoryPage()

    assert [memory["id"] for memory in page] == [contextualID]


@pytest.mark.asyncio
async def test_retrieval_hint_is_encrypted_and_decrypted(tmpKey, patchRun):
    memID = await addMemory(
        "global",
        None,
        "用户正在准备考试",
        retrievalHint="复习安排与临近考试压力",
    )

    raw = patchRun.execute(
        "SELECT retrieval_hint FROM memory_entries WHERE id = ?",
        (memID,),
    ).fetchone()["retrieval_hint"]
    assert raw != "复习安排与临近考试压力"
    assert decryptText(raw) == "复习安排与临近考试压力"
    assert (await getMemoryByID(memID))["retrievalHint"] == "复习安排与临近考试压力"


@pytest.mark.asyncio
async def test_update_memory_reencrypts(tmpKey, patchRun):
    """updateMemory 更新 content 后仍是加密存储且可解密。"""
    memID = await addMemory("global", None, "旧内容")
    ok = await updateMemory(memID, content="新内容")
    assert ok is True

    cursor = patchRun.cursor()
    cursor.execute("SELECT content FROM memory_entries WHERE id = ?", (memID,))
    raw = cursor.fetchone()["content"]
    assert raw != "新内容"
    assert decryptText(raw) == "新内容"

    mem = await getMemoryByID(memID)
    assert mem["content"] == "新内容"


@pytest.mark.asyncio
async def test_content_change_without_new_hint_clears_old_hint(tmpKey, patchRun):
    memID = await addMemory(
        "global",
        None,
        "旧内容",
        retrievalHint="旧检索说明",
    )

    assert await updateMemory(memID, content="新内容") is True
    assert (await getMemoryByID(memID))["retrievalHint"] is None


@pytest.mark.asyncio
async def test_metadata_change_preserves_hint(tmpKey, patchRun):
    memID = await addMemory(
        "global",
        None,
        "内容",
        retrievalHint="检索说明",
    )

    assert await updateMemory(memID, priority=2, mode="pinned") is True
    memory = await getMemoryByID(memID)
    assert memory["retrievalHint"] == "检索说明"
    assert memory["mode"] == "pinned"


@pytest.mark.asyncio
async def test_legacy_plaintext_fallback(tmpKey, patchRun):
    """历史明文行（未加密）读取时应兜底返回原文，不抛异常。"""
    # 直接插入明文，模拟迁移前的历史数据
    cursor = patchRun.cursor()
    cursor.execute(
        "INSERT INTO memory_entries (scope_type, scope_id, content) VALUES (?, ?, ?)",
        ("global", "global", "历史明文记忆"),
    )
    patchRun.commit()
    legacyID = cursor.lastrowid

    mem = await getMemoryByID(legacyID)
    assert mem["content"] == "历史明文记忆"
