"""
tests/utils/llm/memory/test_action_parsing.py

测试 LLM 记忆操作的参数规范化（parseActionDict）、校验与执行门禁。
"""

import asyncio

import pytest
from unittest.mock import AsyncMock, patch

from config import LLM_MEMORY_MAX_CONTENT_LEN, LLM_MEMORY_MAX_TAGS

from utils.llm.memory.action import (
    MemoryAction,
    MemoryActionContext,
    executeAction,
    parseActionDict,
    validateAction,
)


def _addArgs(**overrides) -> dict:
    """模型给出的 add 操作参数（snake_case），默认是一条能通过校验的 global 记忆。"""
    args = {
        "action": "add",
        "scope_type": "global",
        "scope_id": "global",
        "content": "测试内容",
    }
    args.update(overrides)
    return args


# ===========================================================================
# parseActionDict — 模型参数规范化
# ===========================================================================

def test_parseActionDictNormalizesModelFields():
    """snake_case 参数转为内部字段：去首尾空白、枚举值转小写、整数字段转 int。"""
    action = parseActionDict({
        "action": " ADD ",
        "scope_type": " Global ",
        "scope_id": None,
        "content": "  记住这件事  ",
        "tags": ["偏好"],
        "priority": "2",
        "mode": " Contextual ",
        "reason": "  用户明确说过  ",
    })

    assert action.action == "add"
    assert action.scopeType == "global"
    assert action.scopeID == ""
    assert action.content == "记住这件事"
    assert action.tags == ["偏好"]
    assert action.priority == 2
    assert action.memoryID is None
    assert action.mode == "contextual"
    assert action.retrievalHint is None
    assert action.reason == "用户明确说过"


def test_parseActionDictKeepsMissingOptionalFieldsAsNone():
    """没给的可选字段保持 None：update 靠它区分「不改」和「改成空」。"""
    action = parseActionDict({
        "action": "update",
        "scope_type": "global",
        "scope_id": "global",
        "memory_id": 7,
    })

    assert action.memoryID == 7
    assert action.content is None
    assert action.tags is None
    assert action.priority is None
    assert action.mode is None
    assert action.retrievalHint is None


@pytest.mark.parametrize("data", ["add", ["add"], None, 1])
def test_parseActionDictRejectsNonObject(data):
    with pytest.raises(ValueError, match="^记忆操作必须是 JSON 对象$"):
        parseActionDict(data)


@pytest.mark.parametrize("overrides,field", [
    ({"priority": True}, "priority"),
    ({"action": "delete", "memory_id": True}, "memory_id"),
])
def test_booleanIntegerFieldsAreRejected(overrides, field):
    """JSON true 不能当作整数 1：priority 会被提级，memory_id 会误指 #1。"""
    with pytest.raises(ValueError, match=f"^{field} 必须是整数$"):
        parseActionDict(_addArgs(**overrides))


def test_nonNumericPriorityIsRejected():
    with pytest.raises(ValueError):
        parseActionDict(_addArgs(priority="not-an-int"))


def test_tagsAreDeduplicatedAndCapped():
    """tags 去空白、去重、丢弃空项，并按单条记忆的标签上限截断。"""
    action = parseActionDict(_addArgs(tags=[" 偏好 ", "偏好", "", 42]))
    assert action.tags == ["偏好", "42"]

    manyTags = [f"标签{i}" for i in range(LLM_MEMORY_MAX_TAGS + 3)]
    capped = parseActionDict(_addArgs(tags=manyTags))
    assert capped.tags == manyTags[:LLM_MEMORY_MAX_TAGS]


def test_emptyTagsListIsKeptDistinctFromMissing():
    """空数组表示清空标签，不能和「没给 tags」合并成 None。"""
    assert parseActionDict(_addArgs(tags=[])).tags == []


def test_nonListTagsAreRejected():
    with pytest.raises(ValueError, match="^tags 必须是数组$"):
        parseActionDict(_addArgs(tags="偏好"))


def test_modeAndRetrievalHintRoundTrip():
    action = parseActionDict(_addArgs(mode="pinned", retrieval_hint="讨论测试安排时"))
    restored = MemoryAction.fromDict(action.toDict())

    assert restored.mode == "pinned"
    assert restored.retrievalHint == "讨论测试安排时"


@pytest.mark.parametrize("hint", ["", "   "])
def test_blankRetrievalHintMeansExplicitClear(hint):
    """空白 hint 是「清除旧说明」，保留为空字符串，不与未提供（None）混同。"""
    assert parseActionDict(_addArgs(retrieval_hint=hint)).retrievalHint == ""


@pytest.mark.asyncio
@patch("utils.llm.memory.action.logSystemEvent", new_callable=AsyncMock)
async def test_invalidHintDoesNotDiscardOtherwiseValidAdd(mockLog):
    """不合规的检索说明只丢弃说明本身，记忆操作仍然有效。"""
    action = parseActionDict(_addArgs(content="合法事实", retrieval_hint="第一行\n第二行"))

    assert action.retrievalHint is None
    assert await validateAction(action) is None


@pytest.mark.asyncio
@patch("utils.llm.memory.action.logSystemEvent", new_callable=AsyncMock)
async def test_discardedHintLogDoesNotContainMemoryBody(mockLog):
    """丢弃检索说明时只记原因码，不把记忆正文或说明原文写进日志。"""
    secret = "绝密事实 secret-hint-42"

    parseActionDict(_addArgs(content=secret, retrieval_hint=f"{secret}\n第二行"))
    await asyncio.sleep(0)

    loggedText = " ".join(
        str(argument)
        for call in mockLog.call_args_list
        for argument in call.args
    )
    assert secret not in loggedText
    assert "action=add" in loggedText
    assert "reason=hintMultiline" in loggedText


# ===========================================================================
# validateAction — 字段合法性
# ===========================================================================

@pytest.mark.asyncio
async def test_unsupportedActionIsRejected():
    """parseActionDict 不判断 action 是否受支持，这一步由 validateAction 拒绝。"""
    action = parseActionDict(_addArgs(action="frobnicate"))

    assert await validateAction(action) == "不支持的 action：frobnicate"


@pytest.mark.asyncio
async def test_unsupportedScopeTypeIsRejected():
    action = parseActionDict(_addArgs(scope_type="session"))

    assert await validateAction(action) == "不支持的 scope_type：session"


@pytest.mark.asyncio
async def test_addWithoutContentIsRejected():
    args = _addArgs()
    del args["content"]

    assert await validateAction(parseActionDict(args)) == "add 必须提供 content"


@pytest.mark.asyncio
async def test_contentLengthLimit():
    """content 恰好到上限可以通过，多一个字就拒绝。"""
    atLimit = parseActionDict(_addArgs(content="A" * LLM_MEMORY_MAX_CONTENT_LEN))
    overLimit = parseActionDict(_addArgs(content="A" * (LLM_MEMORY_MAX_CONTENT_LEN + 1)))

    assert await validateAction(atLimit) is None
    assert await validateAction(overLimit) == f"content 不能超过 {LLM_MEMORY_MAX_CONTENT_LEN} 字"


# ===========================================================================
# scope 授权与执行门禁
# ===========================================================================

@pytest.mark.asyncio
async def test_modelDeclaredPrivateScopeIsCheckedAgainstRequestIdentity():
    """模型参数里自报的 user scope 必须与请求身份一致，否则在入队前就被拒绝。"""
    context = MemoryActionContext(chatID="chat-1", userID="user-1")
    forged = parseActionDict(_addArgs(scope_type="user", scope_id="other-user"))
    matching = parseActionDict(_addArgs(scope_type="user", scope_id="user-1"))

    assert "不属于当前请求" in await validateAction(forged, actionContext=context)
    assert await validateAction(matching, actionContext=context) is None


@pytest.mark.asyncio
async def test_scopeAuthorizationUsesTrustedRequestIdentity():
    """chat/user scope 只能匹配调用链传入的身份，不能相信模型自报 ID。"""
    chatAction = MemoryAction(
        action="add",
        scopeType="chat",
        scopeID="other-chat",
        content="跨聊天事实",
    )
    userAction = MemoryAction(
        action="add",
        scopeType="user",
        scopeID="other-user",
        content="跨用户事实",
    )
    context = MemoryActionContext(chatID="current-chat", userID="current-user")

    assert "不属于当前请求" in await validateAction(
        chatAction,
        actionContext=context,
    )
    assert "不属于当前请求" in await validateAction(
        userAction,
        actionContext=context,
    )
    assert await validateAction(
        MemoryAction(
            action="add",
            scopeType="chat",
            scopeID="current-chat",
            content="当前聊天事实",
        ),
        actionContext=context,
    ) is None
    assert await validateAction(
        MemoryAction(
            action="add",
            scopeType="user",
            scopeID="current-user",
            content="当前用户事实",
        ),
        actionContext=context,
    ) is None


@pytest.mark.asyncio
async def test_nonGlobalScopeWithoutIdentityFailsClosed():
    """没有请求身份时，非 global action 不得退回模型自报的 scope。"""
    action = MemoryAction(
        action="add",
        scopeType="user",
        scopeID="123",
        content="无身份事实",
    )

    error = await validateAction(action)

    assert error is not None
    assert "缺少当前请求身份" in error


@pytest.mark.asyncio
@patch("utils.llm.memory.action.addMemory", new_callable=AsyncMock)
async def test_globalContextualAddKeepsProductAutoWritePolicy(mockAdd):
    """普通 global contextual 是明确的共享记忆自动写入例外。"""
    mockAdd.return_value = 123
    action = MemoryAction(
        action="add",
        scopeType="global",
        scopeID="任意模型值",
        content="共享事实",
        mode="contextual",
    )

    assert await executeAction(action) is True

    mockAdd.assert_awaited_once()
    assert mockAdd.await_args.args[:2] == ("global", "global")


@pytest.mark.asyncio
@patch("utils.llm.memory.action.logSystemEvent", new_callable=AsyncMock)
@patch("utils.llm.memory.action.addMemory", new_callable=AsyncMock)
async def test_privateContextualAddStillRequiresHumanApproval(mockAdd, mockLog):
    """匹配身份不等于自动授权：chat scope 仍必须经过人工审核。"""
    action = MemoryAction(
        action="add",
        scopeType="chat",
        scopeID="current-chat",
        content="私有事实",
        mode="contextual",
    )

    assert await executeAction(
        action,
        actionContext=MemoryActionContext(chatID="current-chat"),
    ) is False

    mockAdd.assert_not_awaited()
    assert any(
        "需要人工审核" in str(call.args[0])
        for call in mockLog.await_args_list
    )


@pytest.mark.asyncio
@patch("utils.llm.memory.action.addMemory", new_callable=AsyncMock)
async def test_crossScopeExecuteIsRejectedBeforeWrite(mockAdd):
    """执行层再次校验 scope，避免绕过校验层直接调用时跨 chat 写入。"""
    action = MemoryAction(
        action="add",
        scopeType="chat",
        scopeID="other-chat",
        content="不应写入",
    )

    assert await executeAction(
        action,
        actionContext=MemoryActionContext(chatID="current-chat"),
    ) is False

    mockAdd.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("targetMode", [None, "future-mode"])
async def test_invalidTargetModeFailsClosed(targetMode):
    """目标 mode 无法分类时，连人工批准入口也不能按 contextual 执行。"""
    action = MemoryAction(
        action="delete",
        scopeType="global",
        scopeID="global",
        memoryID=7,
    )
    target = {
        "id": 7,
        "scope_type": "global",
        "scope_id": "global",
        "source": "inferred",
        "mode": targetMode,
    }

    with (
        patch(
            "utils.llm.memory.action.getMemoryByID",
            new_callable=AsyncMock,
            return_value=target,
        ),
        patch(
            "utils.llm.memory.action.deleteMemory",
            new_callable=AsyncMock,
        ) as mockDelete,
    ):
        error = await validateAction(action)
        assert error is not None
        assert "mode 无效" in error
        assert await executeAction(
            action,
            humanApproved=True,
            expectedState="ignored",
        ) is False

    mockDelete.assert_not_awaited()


@pytest.mark.asyncio
@patch("utils.llm.memory.action.logSystemEvent", new_callable=AsyncMock)
@patch("utils.llm.memory.action.addMemory", new_callable=AsyncMock)
async def test_pinnedAddRequiresTrustedApproval(mockAdd, mockLog):
    action = MemoryAction(
        action="add",
        scopeType="global",
        scopeID="global",
        content="常驻事实",
        mode="pinned",
    )

    assert await executeAction(action) is False
    mockAdd.assert_not_awaited()
