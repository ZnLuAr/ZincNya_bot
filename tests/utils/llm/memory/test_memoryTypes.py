"""测试 memory 共用数据契约与稳定指纹。"""

from utils.llm.memory.types import (
    MemoryQuery,
    MemoryRetrievalResult,
    MemoryTurn,
    MemoryWriteGuard,
    buildMemoryContentFingerprint,
    buildMemoryStateFingerprint,
)




def _memory(**overrides):
    memory = {
        "id": 7,
        "scope_type": "user",
        "scope_id": "42",
        "content": "用户喜欢清淡咖啡",
        "tags": ["饮食", "咖啡"],
        "retrievalHint": "讨论饮料口味时",
        "enabled": True,
        "priority": 2,
        "mode": "contextual",
        "source": "inferred",
    }
    memory.update(overrides)
    return memory


def test_memoryDataContractsHaveSafeDefaults():
    turn = MemoryTurn(currentText="现在喝什么")
    query = MemoryQuery(turns=(turn,))
    firstResult = MemoryRetrievalResult()
    secondResult = MemoryRetrievalResult()
    guard = MemoryWriteGuard(
        expectedState="abc",
        scopeType="user",
        scopeID="42",
    )

    firstResult.items.append({"id": 1})

    assert query.turns == (turn,)
    assert query.history == ()
    assert secondResult.items == []
    assert guard.allowPinned is False


def test_retrievalResultDoesNotExposeHint():
    result = MemoryRetrievalResult(items=[{
        "id": 1,
        "content": "事实",
        "tags": ["私有分类"],
        "retrievalHint": "私有检索说明",
        "updated_at": "2026-01-01",
        "enabled": True,
    }])

    assert result.items == [{"id": 1, "content": "事实"}]


def test_contentFingerprintOnlyTracksEncodedContentAndVersion():
    original = _memory()
    originalHash = buildMemoryContentFingerprint(
        original,
        modelRevision="revision-a",
        encodingVersion="encoding-a",
    )
    metadataOnly = _memory(priority=3, mode="pinned", enabled=False)

    assert buildMemoryContentFingerprint(
        metadataOnly,
        modelRevision="revision-a",
        encodingVersion="encoding-a",
    ) == originalHash
    assert buildMemoryContentFingerprint(
        _memory(content="用户喜欢浓咖啡"),
        modelRevision="revision-a",
        encodingVersion="encoding-a",
    ) != originalHash
    assert buildMemoryContentFingerprint(
        original,
        modelRevision="revision-b",
        encodingVersion="encoding-a",
    ) != originalHash


def test_stateFingerprintTracksReviewVisibleMetadata():
    originalHash = buildMemoryStateFingerprint(_memory())

    assert buildMemoryStateFingerprint(_memory(priority=3)) != originalHash
    assert buildMemoryStateFingerprint(_memory(mode="pinned")) != originalHash
    assert buildMemoryStateFingerprint(_memory(scope_id="99")) != originalHash


def test_fingerprintHintKeyIsCamelOnly():
    """retrievalHint 键统一为 camelCase：snake 形态不再被识别（值为空），
    指纹随之不同——键名契约钉死单一形态，防止双 key 兼容层回潮。"""
    camelMemory = _memory()
    snakeMemory = _memory()
    snakeMemory["retrieval_hint"] = snakeMemory.pop("retrievalHint")

    assert buildMemoryContentFingerprint(
        camelMemory,
        modelRevision="r",
        encodingVersion="e",
    ) != buildMemoryContentFingerprint(
        snakeMemory,
        modelRevision="r",
        encodingVersion="e",
    )
