"""
tests/utils/llm/capability/test_memory.py

测试记忆操作能力（capability/memory.py）：给模型的说明、参数规范化与校验、handle，
「只收集」与「真正送出」两种构造方式，以及 submitMemoryAction 的自动批准、
送审与无审核人三种去向。

本文件不打开 data/ 下的真实数据库、配置和日志：读目标记录、读自动批准开关、
执行记忆操作的入口都换成假的；Telegram 审核卡看 bot.send_message 与 botData，
控制台队列换成每个用例独立的空队列。
"""

import json
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import InlineKeyboardMarkup

from config import LLM_MEMORY_MAX_CONTENT_LEN, LLM_MEMORY_PRIORITY_CAP

from utils.core.logger import LogLevel
from utils.llm.capability.memory import (
    MEMORY_CAPABILITY_NAME,
    makeCollectingMemoryCapability,
    makeMemoryCapability,
    makeSubmittingMemoryCapability,
    submitMemoryAction,
)
from utils.llm.capability.message import makeMessageCapability
from utils.llm.memory.action import MemoryAction, MemoryActionContext, parseActionDict
from utils.llm.models import DisplayBlocks, MessageInput, MessageOrigin, TaskRequest
from utils.llm.operation.loop import runTask
from utils.llm.operation.types import (
    Operation,
    OperationResult,
    OperationStatus,
    TaskInfo,
    TaskStatus,
)
from utils.llm.review.items import editIndexKey, memoryReviewKey




_EMPTY_END = '<OPERATIONS>{"operations": [], "end": true}</OPERATIONS>'

# 送出用例共用的触发消息身份
_CHAT_ID = "100"
_USER_ID = 42
_TRIGGER_MSG_ID = 9

_EXECUTED_RESULT = OperationResult(status=OperationStatus.DONE, modelText="已执行")
_SUBMITTED_RESULT = OperationResult(
    status=OperationStatus.SUBMITTED,
    modelText="已交给管理员审核，批准前不会生效",
)
_NO_REVIEWER_RESULT = OperationResult(
    status=OperationStatus.FAILED,
    modelText="没有可用的审核人，操作未提交",
)




@pytest.fixture(autouse=True)
def memoryLookup(monkeypatch):
    """
    记忆校验读目标记录的入口换成假的（默认查不到），日志不落盘。

    本文件的用例因此不会打开 data/ 下的真实数据库和日志文件；需要目标记录的
    用例自己设置 return_value。
    """
    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr("utils.llm.memory.action.getMemoryByID", lookup)
    monkeypatch.setattr("utils.llm.memory.action.logSystemEvent", AsyncMock())
    monkeypatch.setattr("utils.llm.operation.loop.logSystemEvent", AsyncMock())
    return lookup


@pytest.fixture(autouse=True)
def submitEnv(monkeypatch):
    """
    submitMemoryAction 的外部依赖换成假的：自动批准开关（默认关闭）、执行入口
    （默认成功）和日志。用例按需改 return_value，并检查它们被怎样调用。
    """
    env = SimpleNamespace(
        autoApprove=MagicMock(return_value=False),
        execute=AsyncMock(return_value=True),
        logAction=AsyncMock(),
        logSystemEvent=AsyncMock(),
    )
    monkeypatch.setattr("utils.llm.capability.memory.getMemoryAutoApprove", env.autoApprove)
    monkeypatch.setattr("utils.llm.capability.memory.executeAction", env.execute)
    monkeypatch.setattr("utils.llm.capability.memory.logAction", env.logAction)
    monkeypatch.setattr("utils.llm.capability.memory.logSystemEvent", env.logSystemEvent)
    return env


@pytest.fixture(autouse=True)
def reviewQueue(monkeypatch):
    """控制台审核队列换成空队列，用例之间互不影响，也不碰模块级的那一条。"""
    queue = asyncio.Queue()
    monkeypatch.setattr("utils.llm.state._llmReviewQueue", queue)
    return queue


@pytest.fixture
def buildPayload(monkeypatch):
    """把构造审核快照的入口换成假的；用例设置 return_value 并检查调用。"""
    mock = AsyncMock(return_value={"action": "add", "content": "X"})
    monkeypatch.setattr("utils.llm.capability.memory.buildMemoryActionReviewPayload", mock)
    return mock




def _task(**overrides) -> TaskInfo:
    fields = {"taskID": "t1", "chatID": "100", "userID": 42, "includeContext": True}
    fields.update(overrides)
    return TaskInfo(**fields)


def _submitted(modelText="已交给管理员审核") -> OperationResult:
    return OperationResult(status=OperationStatus.SUBMITTED, modelText=modelText)


def _batch(*operations, end=True) -> str:
    payload = json.dumps({"operations": list(operations), "end": end}, ensure_ascii=False)
    return f"<OPERATIONS>{payload}</OPERATIONS>"


def _scriptedModel(*outputs):
    """按顺序返回预设输出的模型回调，连同它每次收到的 appendix 列表一起返回。"""
    appendices = []

    async def _callModel(*, instructions, appendix):
        appendices.append(appendix)
        return outputs[len(appendices) - 1]

    return _callModel, appendices


def _addArgs(**overrides) -> dict:
    """一个合法的 global add；覆盖值为 None 表示模型没给这个字段。"""
    args = {
        "action": "add",
        "scope_type": "global",
        "scope_id": "global",
        "content": "记忆内容",
        "mode": "contextual",
    }
    args.update(overrides)
    return args


def _targetArgs(action="delete", **overrides) -> dict:
    """一个指向 #7 的 global update / delete。"""
    args = {"action": action, "scope_type": "global", "scope_id": "global", "memory_id": 7}
    args.update(overrides)
    return args


def _storedMemory(**overrides) -> dict:
    """数据库里 #7 的样子：模型自己推断出来的 global contextual 记忆。"""
    memory = {
        "id": 7,
        "scope_type": "global",
        "scope_id": "global",
        "content": "旧内容",
        "source": "inferred",
        "mode": "contextual",
    }
    memory.update(overrides)
    return memory


def _memoryOperation(**overrides) -> Operation:
    return Operation(index=1, name=MEMORY_CAPABILITY_NAME, args=_addArgs(**overrides))


def _globalAdd(content: str = "A", **fields) -> MemoryAction:
    return MemoryAction(action="add", scopeType="global", scopeID="global", content=content, **fields)


def _request(**messageFields) -> TaskRequest:
    """一条触发消息组成的、进入聊天上下文的请求；messageFields 覆盖 MessageInput 的字段。"""
    fields = {"text": "我喜欢乌龙茶", "includeContext": True}
    fields.update(messageFields)
    return TaskRequest(
        messages=(MessageInput(
            origin=MessageOrigin(
                chatID=_CHAT_ID, userID=_USER_ID, messageID=_TRIGGER_MSG_ID, sender="alice",
            ),
            **fields,
        ),),
        includeContext=True,
    )


def _cardBot(reviewMsgID: int = 601) -> AsyncMock:
    """Telegram Bot 替身：发出的审核卡 message_id 是 reviewMsgID。"""
    bot = AsyncMock()
    bot.send_message.return_value = SimpleNamespace(message_id=reviewMsgID)
    return bot


def _memoryReviewKeys(botData: dict) -> list[str]:
    return [key for key in botData if str(key).startswith("llm_memreview_")]


async def _submit(action: MemoryAction, **overrides) -> OperationResult:
    """调用 submitMemoryAction；没给的参数用一组默认值（on 模式、一位 ops）。"""
    kwargs = {
        "bot": _cardBot(),
        "botData": {},
        "autoMode": "on",
        "opsList": ["1"],
        "chatID": 1,
        "userID": 1,
        "originalMsg": "msg",
    }
    kwargs.update(overrides)
    return await submitMemoryAction(action, **kwargs)




# ===========================================================================
# 记忆能力：说明
# ===========================================================================

def test_memoryCapabilityIsOfferedOnlyWithContext():
    capability = makeMemoryCapability(AsyncMock())

    assert capability.name == MEMORY_CAPABILITY_NAME == "memory_action"
    assert '"op": "memory_action"' in capability.describe(_task(includeContext=True))
    assert capability.describe(_task(includeContext=False)) == ""


async def test_memoryOperationIsUnknownWithoutContextInLoop():
    deliver = AsyncMock(return_value=OperationResult(status=OperationStatus.DONE))
    submit = AsyncMock(return_value=_submitted())
    callModel, appendices = _scriptedModel(
        _batch({"op": "message", "text": "记下了"}, {"op": "memory_action", **_addArgs()}),
        _EMPTY_END,
    )

    outcome = await runTask(
        task=_task(includeContext=False),
        capabilities=[makeMessageCapability(deliver), makeMemoryCapability(submit)],
        callModel=callModel,
    )

    # 没进聊天上下文的任务不提供记忆能力；同批的发言也不执行
    assert "第 2 个操作（memory_action）：没有这个能力；可用的能力：message" in appendices[1]
    submit.assert_not_awaited()
    deliver.assert_not_awaited()
    assert outcome.records == ()




# ===========================================================================
# 记忆能力：参数规范化
# ===========================================================================

def test_parseActionDictIgnoresOpKey():
    args = _addArgs(tags=["偏好"], priority=1, reason="用户明确说过")

    withOp = parseActionDict({"op": MEMORY_CAPABILITY_NAME, **args})

    assert withOp == parseActionDict(args)
    assert withOp.action == "add"


async def test_memoryCapabilityToleratesOpKeyInArgs():
    """解析器会先去掉 op；这里确认即使参数里还留着 op，校验和提交也不受影响。"""
    submit = AsyncMock(return_value=_submitted())
    capability = makeMemoryCapability(submit)
    args = {"op": MEMORY_CAPABILITY_NAME, **_addArgs()}
    operation = Operation(index=1, name=MEMORY_CAPABILITY_NAME, args=args)
    task = _task()

    assert await capability.validate(args, task) is None
    await capability.handle(operation, task)

    assert submit.await_args.args[0] == parseActionDict(_addArgs())


async def test_memoryHandleSubmitsNormalizedAction():
    submitted = _submitted()
    submit = AsyncMock(return_value=submitted)
    capability = makeMemoryCapability(submit)
    args = {
        "action": " ADD ",
        "scope_type": "Global",
        "scope_id": "global",
        "content": "  记忆内容  ",
        "tags": ["偏好", "偏好", " 饮食 ", ""],
        "priority": "2",
        "mode": "PINNED",
        "retrieval_hint": " 聊到吃什么时 ",
        "reason": " 用户明确说过 ",
    }
    operation = Operation(index=3, name=MEMORY_CAPABILITY_NAME, args=args)
    task = _task()

    assert await capability.validate(args, task) is None
    result = await capability.handle(operation, task)

    # 模型的 snake_case 字段转成内部表示：去空白、统一小写、数字转整数、标签去重
    assert result is submitted
    submit.assert_awaited_once_with(
        MemoryAction(
            action="add",
            scopeType="global",
            scopeID="global",
            content="记忆内容",
            tags=["偏好", "饮食"],
            priority=2,
            memoryID=None,
            mode="pinned",
            retrievalHint="聊到吃什么时",
            reason="用户明确说过",
        ),
        operation,
        task,
    )


async def test_memoryValidateDoesNotMutateArgs():
    capability = makeMemoryCapability(AsyncMock())
    args = _addArgs(mode=" Pinned ", tags=["偏好", "偏好"])
    original = json.loads(json.dumps(args))

    assert await capability.validate(args, _task()) is None

    assert args == original


async def test_memoryValidateHasNoSideEffect():
    submit = AsyncMock()
    capability = makeMemoryCapability(submit)

    await capability.validate(_addArgs(), _task())

    submit.assert_not_awaited()




# ===========================================================================
# 记忆能力：校验失败的原因
# ===========================================================================

@pytest.mark.parametrize("args, expected", [
    (_addArgs(priority=True), "priority 必须是整数"),
    (_targetArgs(memory_id=True), "memory_id 必须是整数"),
    (_addArgs(tags="偏好"), "tags 必须是数组"),
])
async def test_memoryValidateReturnsNormalizationError(args, expected):
    capability = makeMemoryCapability(AsyncMock())

    assert await capability.validate(args, _task()) == expected


@pytest.mark.parametrize("args, expected", [
    ({}, "不支持的 action：?"),
    (_addArgs(action="frobnicate"), "不支持的 action：frobnicate"),
    (_addArgs(scope_type="src"), "不支持的 scope_type：src"),
    (_addArgs(scope_type="chat", scope_id=""), "非 global scope 必须提供 scope_id"),
    (_addArgs(content=None), "add 必须提供 content"),
    (_addArgs(content="   "), "content 不能为空"),
    (
        _addArgs(content="字" * (LLM_MEMORY_MAX_CONTENT_LEN + 1)),
        f"content 不能超过 {LLM_MEMORY_MAX_CONTENT_LEN} 字",
    ),
    (_addArgs(priority=-1), "priority 不能小于 0"),
    (
        _addArgs(priority=LLM_MEMORY_PRIORITY_CAP + 1),
        f"priority 不能超过 {LLM_MEMORY_PRIORITY_CAP}",
    ),
    (_addArgs(mode="sticky"), "mode 必须是"),
    (_targetArgs("delete", memory_id=None), "delete 必须提供 memory_id"),
    (_targetArgs("update", memory_id=None, content="新内容"), "update 必须提供 memory_id"),
])
async def test_memoryValidateReturnsValidationReason(args, expected):
    capability = makeMemoryCapability(AsyncMock())

    reason = await capability.validate(args, _task())

    assert expected in reason


@pytest.mark.parametrize("field, value", [
    ("priority", [2]),
    ("memory_id", {"id": 7}),
    ("priority", float("inf")),
    ("priority", "高"),
    ("memory_id", "abc"),
    ("memory_id", 7.9),
])
async def test_memoryValidateNamesFieldForNonIntegerValue(field, value):
    capability = makeMemoryCapability(AsyncMock())
    args = _targetArgs("update", content="新内容")
    args[field] = value

    reason = await capability.validate(args, _task())

    assert reason == f"{field} 必须是整数"




# ===========================================================================
# 记忆能力：update / delete 要核对目标记录
# ===========================================================================

async def test_memoryValidateRejectsMissingTarget(memoryLookup):
    capability = makeMemoryCapability(AsyncMock())

    reason = await capability.validate(_targetArgs("delete"), _task())

    assert reason == "memory #7 不存在"
    memoryLookup.assert_awaited_once_with(7)


async def test_memoryValidateRejectsManualTarget(memoryLookup):
    memoryLookup.return_value = _storedMemory(source="manual")
    capability = makeMemoryCapability(AsyncMock())

    reason = await capability.validate(_targetArgs("delete"), _task())

    assert reason == "memory #7 不是 inferred，禁止修改"


async def test_memoryValidateRejectsTargetInOtherScope(memoryLookup):
    memoryLookup.return_value = _storedMemory(scope_type="user", scope_id="42")
    capability = makeMemoryCapability(AsyncMock())

    reason = await capability.validate(_targetArgs("delete"), _task())

    assert reason == "memory #7 的 scope 不匹配"


async def test_memoryValidateRejectsUpdateWithoutChanges(memoryLookup):
    memoryLookup.return_value = _storedMemory()
    capability = makeMemoryCapability(AsyncMock())

    reason = await capability.validate(_targetArgs("update"), _task())

    assert reason.startswith("update 至少要包含")


@pytest.mark.parametrize("args", [
    _targetArgs("delete"),
    _targetArgs("update", content="新内容"),
    _targetArgs("update", tags=[]),
])
async def test_memoryValidateAcceptsOperationOnInferredTarget(memoryLookup, args):
    memoryLookup.return_value = _storedMemory()
    capability = makeMemoryCapability(AsyncMock())

    assert await capability.validate(args, _task()) is None


async def test_memoryValidateDoesNotReadTargetForAdd(memoryLookup):
    capability = makeMemoryCapability(AsyncMock())

    assert await capability.validate(_addArgs(), _task()) is None

    memoryLookup.assert_not_awaited()




# ===========================================================================
# 记忆能力：scope 归属只认任务的可信身份
# ===========================================================================

async def test_memoryValidatePassesTaskIdentityAsActionContext(monkeypatch):
    validate = AsyncMock(return_value=None)
    monkeypatch.setattr("utils.llm.capability.memory.validateAction", validate)
    capability = makeMemoryCapability(AsyncMock())
    args = _addArgs(scope_type="chat", scope_id="999", chat_id="999", chatID="999", user_id="999")

    await capability.validate(args, _task(chatID="100", userID=42))

    # 身份取自 task；模型在参数里写的各种 ID 不进入授权上下文
    assert validate.await_args.kwargs == {
        "actionContext": MemoryActionContext(chatID="100", userID=42),
    }


@pytest.mark.parametrize("scopeType, scopeID", [("chat", "100"), ("user", "42")])
async def test_memoryValidateAcceptsScopeOfCurrentTask(scopeType, scopeID):
    capability = makeMemoryCapability(AsyncMock())
    args = _addArgs(scope_type=scopeType, scope_id=scopeID)

    # userID 在 task 里是整数，模型给的是字符串，按文本比较
    assert await capability.validate(args, _task(chatID="100", userID=42)) is None


@pytest.mark.parametrize("scopeType, scopeID", [("chat", "999"), ("user", "999"), ("user", "100")])
async def test_memoryValidateRejectsScopeOfOtherChatOrUser(scopeType, scopeID):
    capability = makeMemoryCapability(AsyncMock())
    args = _addArgs(scope_type=scopeType, scope_id=scopeID, chat_id=scopeID, user_id=scopeID)

    reason = await capability.validate(args, _task(chatID="100", userID=42))

    assert reason == f"{scopeType} memory scope 不属于当前请求"


async def test_memoryValidateRejectsUserScopeWhenTaskHasNoUser():
    capability = makeMemoryCapability(AsyncMock())
    args = _addArgs(scope_type="user", scope_id="42")

    reason = await capability.validate(args, _task(userID=None))

    assert reason == "user memory 缺少当前请求身份，拒绝执行"


async def test_memoryValidateAcceptsGlobalScopeWithoutUser():
    capability = makeMemoryCapability(AsyncMock())

    assert await capability.validate(_addArgs(), _task(userID=None)) is None




# ===========================================================================
# 记忆能力：经过循环
# ===========================================================================

async def test_memoryOperationRunsThroughLoop():
    submit = AsyncMock(return_value=_submitted())
    callModel, _ = _scriptedModel(_batch({"op": "memory_action", **_addArgs(priority=2)}))
    task = _task()

    outcome = await runTask(
        task=task, capabilities=[makeMemoryCapability(submit)], callModel=callModel,
    )

    assert outcome.status == TaskStatus.COMPLETED
    assert outcome.records[0].result.status == OperationStatus.SUBMITTED

    action, operation, submittedTask = submit.await_args.args
    assert action == parseActionDict(_addArgs(priority=2))
    assert operation == Operation(index=1, name="memory_action", args=_addArgs(priority=2))
    assert submittedTask is task


async def test_invalidMemoryOperationBlocksWholeBatchInLoop():
    deliver = AsyncMock(return_value=OperationResult(status=OperationStatus.DONE))
    submit = AsyncMock(return_value=_submitted())
    callModel, appendices = _scriptedModel(
        _batch(
            {"op": "message", "text": "记下了"},
            {"op": "memory_action", **_addArgs(scope_type="chat", scope_id="999")},
        ),
        _EMPTY_END,
    )

    outcome = await runTask(
        task=_task(chatID="100"),
        capabilities=[makeMessageCapability(deliver), makeMemoryCapability(submit)],
        callModel=callModel,
    )

    # 记忆操作越权：同批的发言也不送出，原因退回模型
    deliver.assert_not_awaited()
    submit.assert_not_awaited()
    assert outcome.records == ()
    assert "第 2 个操作（memory_action）：chat memory scope 不属于当前请求" in appendices[1]




# ===========================================================================
# 只收集的记忆能力
# ===========================================================================

async def test_collectingMemoryCapabilityAppendsParsedAction(submitEnv, reviewQueue):
    actions = []
    capability = makeCollectingMemoryCapability(actions)

    result = await capability.handle(_memoryOperation(priority="2", tags=["偏好", "偏好"]), _task())

    assert result == OperationResult(status=OperationStatus.SUBMITTED, modelText="已交给管理员审核")
    assert actions == [
        MemoryAction(
            action="add", scopeType="global", scopeID="global", content="记忆内容",
            tags=["偏好"], priority=2, mode="contextual",
        ),
    ]
    # 只收集：不读自动批准开关、不执行、不进审核队列
    submitEnv.autoApprove.assert_not_called()
    submitEnv.execute.assert_not_awaited()
    assert reviewQueue.empty()


async def test_collectingMemoryCapabilityKeepsOrderAndValidatesInLoop(submitEnv):
    actions = []
    callModel, appendices = _scriptedModel(
        # 第一批里有越权的操作，整批退回，一个都不收集
        _batch(
            {"op": "memory_action", **_addArgs(content="A")},
            {"op": "memory_action", **_addArgs(scope_type="chat", scope_id="999")},
            end=False,
        ),
        _batch(
            {"op": "memory_action", **_addArgs(content="A")},
            {"op": "memory_action", **_addArgs(content="B")},
        ),
    )

    outcome = await runTask(
        task=_task(chatID="100"),
        capabilities=[makeCollectingMemoryCapability(actions)],
        callModel=callModel,
    )

    assert "chat memory scope 不属于当前请求" in appendices[1]
    assert [action.content for action in actions] == ["A", "B"]
    assert [record.result.status for record in outcome.records] == [OperationStatus.SUBMITTED] * 2
    submitEnv.execute.assert_not_awaited()




# ===========================================================================
# 真正送出的记忆能力
# ===========================================================================

@pytest.mark.parametrize("submitResult", [_EXECUTED_RESULT, _SUBMITTED_RESULT, _NO_REVIEWER_RESULT])
async def test_submittingMemoryCapabilityHandsRequestToSubmitMemoryAction(monkeypatch, submitResult):
    submit = AsyncMock(return_value=submitResult)
    monkeypatch.setattr("utils.llm.capability.memory.submitMemoryAction", submit)
    request = _request()
    bot = AsyncMock()
    botData = {}
    opsList = ["111", "222"]
    capability = makeSubmittingMemoryCapability(
        request, bot=bot, botData=botData, autoMode="off", opsList=opsList,
    )
    operation = _memoryOperation(content="喜欢乌龙茶", chat_id="999", user_id="999")

    # 模型参数里的各种 ID、任务信息里的身份都不参与：身份、原文、展示块取自构造时的请求
    result = await capability.handle(operation, _task(chatID="555", userID=555))

    assert result is submitResult
    submit.assert_awaited_once()
    action = submit.await_args.args[0]
    assert isinstance(action, MemoryAction)
    assert (action.action, action.scopeType, action.content) == ("add", "global", "喜欢乌龙茶")

    kwargs = dict(submit.await_args.kwargs)
    # 首次生成要读自动批准开关
    assert kwargs.pop("respectAutoApprove", True) is True
    assert kwargs == {
        "bot": bot,
        "botData": botData,
        "autoMode": "off",
        "opsList": opsList,
        "chatID": _CHAT_ID,
        "userID": _USER_ID,
        "originalMsg": request.displayOriginalMsg,
        "displayBlocks": request.displayBlocks,
    }
    assert kwargs["botData"] is botData


async def test_submittingMemoryCapabilityReviewCardGoesToFirstOps():
    """走真实的 submitMemoryAction：审核卡发给第一位 ops，审核项里的身份、原文、展示块来自请求。"""
    request = _request(text="我喜欢乌龙茶", replyText="你爱喝什么", replySender="bob")
    bot = _cardBot(601)
    botData = {}
    capability = makeSubmittingMemoryCapability(
        request, bot=bot, botData=botData, autoMode="off", opsList=["111", "222"],
    )

    result = await capability.handle(_memoryOperation(content="喜欢乌龙茶"), _task())

    assert result == _SUBMITTED_RESULT
    bot.send_message.assert_awaited_once()
    sent = bot.send_message.await_args.kwargs
    assert sent["chat_id"] == 111
    assert "喜欢乌龙茶" in sent["text"]
    assert "你爱喝什么" in sent["text"]

    entry = botData[memoryReviewKey(100, 601)]
    assert entry["chatID"] == 100
    assert entry["opsID"] == 111
    assert entry["userID"] == _USER_ID
    assert entry["originalMsg"] == request.displayOriginalMsg
    assert entry["displayBlocks"] == request.displayBlocks
    assert (entry["action"]["action"], entry["action"]["scopeType"], entry["action"]["content"]) == (
        "add", "global", "喜欢乌龙茶",
    )




# ===========================================================================
# submitMemoryAction：自动批准、送审、无审核人
# ===========================================================================

class TestSubmitMemoryAction:
    """submitMemoryAction：处理一个记忆操作，返回的结果说明它的实际去向"""

    # --- 自动批准 ---

    async def test_autoapprove_success_returns_executed(self, submitEnv, buildPayload, reviewQueue):
        """自动批准开启且执行成功：记已完成，不构造审核快照、不送审"""
        submitEnv.autoApprove.return_value = True
        bot = _cardBot()
        botData = {}
        action = _globalAdd("共享事实", mode="contextual")

        result = await _submit(action, bot=bot, botData=botData, chatID=1, userID=7)

        assert result == _EXECUTED_RESULT
        submitEnv.execute.assert_awaited_once_with(
            action,
            humanApproved=False,
            actionContext=MemoryActionContext(chatID=1, userID=7),
        )
        buildPayload.assert_not_awaited()
        bot.send_message.assert_not_awaited()
        assert botData == {}
        assert reviewQueue.empty()
        assert submitEnv.logAction.await_args.args[1] == "LLM 记忆操作自动执行 (成功)"

    async def test_autoapprove_success_needs_no_reviewer(self, submitEnv):
        """自动执行成功不需要审核人：opsList 为空也记已完成"""
        submitEnv.autoApprove.return_value = True

        result = await _submit(_globalAdd(), opsList=[])

        assert result == _EXECUTED_RESULT
        submitEnv.logSystemEvent.assert_not_awaited()

    async def test_autoapprove_failure_falls_back_to_review(self, submitEnv, buildPayload):
        """自动执行失败：操作落入人工审核，记已送审"""
        submitEnv.autoApprove.return_value = True
        submitEnv.execute.return_value = False
        buildPayload.return_value = {"action": "add", "content": "待重试"}
        bot = _cardBot(601)
        botData = {}
        action = _globalAdd("待重试", mode="contextual")

        result = await _submit(action, bot=bot, botData=botData)

        assert result == _SUBMITTED_RESULT
        submitEnv.execute.assert_awaited_once()
        buildPayload.assert_awaited_once_with(action)
        bot.send_message.assert_awaited_once()
        assert botData[memoryReviewKey(1, 601)]["action"] is buildPayload.return_value
        assert submitEnv.logAction.await_args.args[1] == "LLM 记忆操作自动执行 (失败)"

    async def test_autoapprove_failure_without_reviewer_is_dropped(self, submitEnv):
        """自动执行失败且没有审核人：记失败，并留下告警"""
        submitEnv.autoApprove.return_value = True
        submitEnv.execute.return_value = False

        result = await _submit(_globalAdd(), opsList=[])

        assert result == _NO_REVIEWER_RESULT
        submitEnv.logSystemEvent.assert_awaited_once()

    @pytest.mark.parametrize("action", [
        MemoryAction(action="add", scopeType="chat", scopeID="1", content="私有事实", mode="contextual"),
        MemoryAction(action="add", scopeType="user", scopeID="1", content="用户事实", mode="contextual"),
        MemoryAction(action="add", scopeType="global", scopeID="global", content="常驻事实", mode="pinned"),
    ])
    async def test_autoapprove_does_not_cover_private_or_pinned(self, submitEnv, buildPayload, action):
        """自动批准只放行普通 global contextual；chat/user scope 与 pinned 不执行，直接送审"""
        submitEnv.autoApprove.return_value = True
        buildPayload.return_value = {"action": "add"}
        bot = _cardBot(601)
        botData = {}

        result = await _submit(action, bot=bot, botData=botData, chatID="1", userID="1")

        assert result == _SUBMITTED_RESULT
        submitEnv.execute.assert_not_awaited()
        buildPayload.assert_awaited_once_with(action)
        bot.send_message.assert_awaited_once()
        assert botData[memoryReviewKey(1, 601)]["action"] == {"action": "add"}

    async def test_autoapprove_routes_successive_actions_independently(self, submitEnv):
        """同一批里逐个提交：普通 global contextual 直接执行，chat scope 与 pinned 各自送审。"""
        submitEnv.autoApprove.return_value = True
        bot = AsyncMock()
        bot.send_message.side_effect = [SimpleNamespace(message_id=601), SimpleNamespace(message_id=602)]
        botData = {}
        actions = [
            _globalAdd("共享事实", mode="contextual"),
            MemoryAction(action="add", scopeType="chat", scopeID="1", content="私有事实", mode="contextual"),
            _globalAdd("常驻事实", mode="pinned"),
        ]

        results = [
            await _submit(action, bot=bot, botData=botData, chatID="1", userID=1)
            for action in actions
        ]

        assert results == [_EXECUTED_RESULT, _SUBMITTED_RESULT, _SUBMITTED_RESULT]
        submitEnv.execute.assert_awaited_once()
        assert submitEnv.execute.await_args.args[0] is actions[0]
        assert submitEnv.execute.await_args.kwargs == {
            "humanApproved": False,
            "actionContext": MemoryActionContext(chatID="1", userID=1),
        }
        assert [botData[memoryReviewKey(1, msgID)]["action"]["content"] for msgID in (601, 602)] == [
            "私有事实", "常驻事实",
        ]

    async def test_respect_autoapprove_false_does_not_read_setting(self, submitEnv, buildPayload):
        """respectAutoApprove=False：不读自动批准配置，可自动执行的操作也送审"""
        submitEnv.autoApprove.return_value = True
        bot = _cardBot(601)
        botData = {}

        result = await _submit(
            _globalAdd(mode="contextual"), bot=bot, botData=botData, respectAutoApprove=False,
        )

        assert result == _SUBMITTED_RESULT
        submitEnv.autoApprove.assert_not_called()
        submitEnv.execute.assert_not_awaited()
        bot.send_message.assert_awaited_once()
        assert botData[memoryReviewKey(1, 601)]["action"] == {"action": "add", "content": "X"}

    # --- 没有审核人 ---

    @patch("utils.llm.capability.memory.addMemoryReviewItem")
    @pytest.mark.parametrize("autoMode", ["on", "off", "console"])
    async def test_no_ops_returns_dropped_and_logs_warning(self, mockAdd, submitEnv, buildPayload, autoMode):
        """没有审核人：记失败并记 WARNING；不入队、不发审核卡，日志不含记忆正文"""
        bot = _cardBot()
        botData = {}
        action = _globalAdd("绝密事实 secret-42")

        result = await _submit(action, bot=bot, botData=botData, autoMode=autoMode, opsList=[])

        assert result == _NO_REVIEWER_RESULT
        submitEnv.logSystemEvent.assert_awaited_once()
        event, detail, level = submitEnv.logSystemEvent.await_args.args
        assert event == "LLM 记忆操作无审核人"
        assert detail == "操作未提交（无 LLM ops）：add | scope=global:global"
        assert level == LogLevel.WARNING
        buildPayload.assert_not_awaited()
        mockAdd.assert_not_called()
        bot.send_message.assert_not_awaited()
        assert botData == {}

    # --- console：加入控制台审核队列 ---

    @patch("utils.llm.capability.memory.addMemoryReviewItem")
    async def test_console_mode_queues_item_and_returns_submitted(self, mockAdd, buildPayload):
        """autoMode=console：审核快照入 console 队列，交给第一个 ops，ID 统一为字符串"""
        bot = _cardBot()
        botData = {}

        result = await _submit(
            _globalAdd(), bot=bot, botData=botData, autoMode="console", opsList=[42, 43],
            chatID=99, userID=7, originalMsg="触发消息",
            displayBlocks=DisplayBlocks(pairs=[{"replyLine": "", "currentText": "触发消息"}]),
        )

        assert result == _SUBMITTED_RESULT
        # 控制台队列只用 originalMsg，不带展示块
        mockAdd.assert_called_once_with(
            action={"action": "add", "content": "X"},
            chatID="99",
            originalMsg="触发消息",
            opsID="42",
            userID=7,
        )
        bot.send_message.assert_not_awaited()
        assert botData == {}

    async def test_console_queue_keeps_action_order(self, reviewQueue):
        """逐个提交时，console 队列里的审核项顺序与提交顺序一致（走真实的快照构造与入队）"""
        for content in ("A", "B", "C"):
            result = await _submit(_globalAdd(content), autoMode="console", opsList=["1"])
            assert result == _SUBMITTED_RESULT

        queued = [reviewQueue.get_nowait() for _ in range(reviewQueue.qsize())]
        assert [item["action"]["content"] for item in queued] == ["A", "B", "C"]
        assert {(item["kind"], item["chatID"], item["opsID"]) for item in queued} == {("memory", "1", "1")}

    # --- 其余模式：发 Telegram 审核卡 ---

    @patch("utils.llm.capability.memory.addMemoryReviewItem")
    @pytest.mark.parametrize("autoMode", ["on", "off"])
    async def test_tg_mode_sends_card_to_first_ops_and_returns_submitted(self, mockAdd, buildPayload, autoMode):
        """autoMode 非 console：审核卡发给第一位 ops，审核项存进 bot_data，不进 console 队列"""
        payload = {"action": "add", "scopeType": "global", "scopeID": "global", "content": "喜欢乌龙茶"}
        buildPayload.return_value = payload
        blocks = DisplayBlocks(pairs=[{"replyLine": "", "currentText": "结构化的触发消息"}])
        bot = _cardBot(601)
        botData = {}

        result = await _submit(
            _globalAdd("喜欢乌龙茶"), bot=bot, botData=botData, autoMode=autoMode, opsList=["111", "222"],
            chatID="100", userID=7, originalMsg="触发消息", displayBlocks=blocks,
        )

        assert result == _SUBMITTED_RESULT
        bot.send_message.assert_awaited_once()
        sent = bot.send_message.await_args.kwargs
        assert sent["chat_id"] == 111
        assert "[记忆操作待审核 · ADD]" in sent["text"]
        assert "喜欢乌龙茶" in sent["text"]
        assert "结构化的触发消息" in sent["text"]
        assert sent["parse_mode"] == "HTML"
        assert isinstance(sent["reply_markup"], InlineKeyboardMarkup)

        key = memoryReviewKey(100, 601)
        assert _memoryReviewKeys(botData) == [key]
        entry = dict(botData[key])
        assert isinstance(entry.pop("createdAt"), float)
        assert entry == {
            "action": payload,
            "originalMsg": "触发消息",
            "chatID": 100,
            "opsID": 111,
            "userID": 7,
            "displayBlocks": blocks,
        }
        assert botData[key]["action"] is payload
        assert botData[editIndexKey(601)] == key
        mockAdd.assert_not_called()
