"""
tests/utils/llm/test_task.py

测试任务入口（utils/llm/task.py）与任务请求（utils/llm/models.py）：
    - TaskRequest：批次消息渲染为提示词与审核展示（displayPairs / displayBlocks）
    - runRequest：反馈处理、任务身份、模型输入准备与复用、外部能力追加
    - collectOutput：发言与记忆操作只收集不送出

模型输入准备和模型请求在 utils.llm.task 内被替换；假的模型输出是完整的
<OPERATIONS> 块，解析、校验和循环走真实代码。
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from config import LLM_TASK_MAX_MODEL_CALLS

from utils.core.logger import LogLevel
from utils.llm import task as taskModule
from utils.llm.memory.action import MemoryAction
from utils.llm.memory.types import MemoryTurn
from utils.llm.models import DisplayBlocks, MessageInput, MessageOrigin, TaskRequest
from utils.llm.operation import (
    Capability,
    OperationResult,
    OperationStatus,
    TaskStatus,
)
from utils.llm.task import collectOutput, runRequest


def _request(includeContext=False, *, chatID="100", memoryTurn=True, images=True):
    return TaskRequest(
        messages=(MessageInput(
            origin=MessageOrigin(chatID=chatID, userID=42, messageID=7, sender="alice"),
            text="当前问题",
            replyText="引用原文 https://example.com",
            replySender="bob",
            images=({"data": "image-data", "mimeType": "image/jpeg"},) if images else (),
            imageNotes=("[图片下载失败]",),
            extraBlocks=((1, "扩展", "扩展内容"),),
            memoryTurn=MemoryTurn(currentText="当前问题", replyText="引用原文") if memoryTurn else None,
        ),),
        includeContext=includeContext,
    )


def _batch(*operations, end=True) -> str:
    """一次合法的模型输出。"""
    payload = json.dumps({"operations": list(operations), "end": end}, ensure_ascii=False)
    return f"<OPERATIONS>\n{payload}\n</OPERATIONS>"


def _message(text: str) -> dict:
    return {"op": "message", "text": text}


def _memoryAdd(content: str, **fields) -> dict:
    return {
        "op": "memory_action",
        "action": "add",
        "scope_type": "global",
        "scope_id": "global",
        "content": content,
        **fields,
    }


def _probeCapability(name: str, handled: list) -> Capability:
    """一种测试用能力：任何参数都合法，处理时记下操作和任务信息。"""

    async def _validate(args, task):
        return None

    async def _handle(operation, task):
        handled.append((operation, task))
        return OperationResult(status=OperationStatus.DONE, modelText="ok")

    return Capability(
        name=name,
        describe=lambda task: f"【{name}】测试能力",
        validate=_validate,
        handle=_handle,
    )


@pytest.fixture
def modelEnv(monkeypatch, contextStore):
    """
    替换 task 模块里的模型输入准备与模型请求。

    env.outputs 是按调用顺序给出的模型输出；用到最后一项后一直重复它。
    默认只有一项：不做任何操作、直接结束。
    项是异常实例时，该次请求抛出它。env.requests 记录每次请求收到的参数。
    """
    env = SimpleNamespace()
    env.modelInput = object()
    env.prepare = AsyncMock(return_value=env.modelInput)
    env.outputs = [_batch()]
    env.requests = []
    env.log = AsyncMock()

    async def _requestModelOutput(modelInput, *, instructions, appendix=""):
        env.requests.append(SimpleNamespace(
            modelInput=modelInput, instructions=instructions, appendix=appendix,
        ))
        output = env.outputs.pop(0) if len(env.outputs) > 1 else env.outputs[0]
        if isinstance(output, Exception):
            raise output
        return output

    monkeypatch.setattr(taskModule, "prepareModelInput", env.prepare)
    monkeypatch.setattr(taskModule, "requestModelOutput", _requestModelOutput)
    monkeypatch.setattr(taskModule, "listCapabilities", lambda: [])
    monkeypatch.setattr(taskModule, "logSystemEvent", env.log)
    monkeypatch.setattr("utils.llm.operation.loop.logSystemEvent", AsyncMock())
    return env




# ============================================================================
# TaskRequest
# ============================================================================

@pytest.mark.parametrize("replySender, expected", [
    ("bob", "<@bob>"), ("张三", "<@张三>"), ("未知用户", "<未知用户>"),
])
def test_request_renders_reply_without_losing_original(replySender, expected):
    message = MessageInput(
        origin=MessageOrigin(chatID="100", userID=42, messageID=7, sender="alice"), text="当前",
        replyText="原文" * 400, replySender=replySender,
    )
    request = TaskRequest(messages=(message,))
    truncated = "原文" * 400
    truncated = truncated[:300] + "……"
    assert f"[引用的消息]\n{expected} {truncated}" in request.userMessage
    assert "[当前用户消息]\n<@alice> 当前" in request.userMessage
    assert "原文" * 151 not in request.userMessage
    assert message.replyText == "原文" * 400
    assert request.displayPairs == [{"replyLine": f"{expected} {truncated}", "currentText": "当前"}]


def test_request_keeps_message_pairs_in_order():
    request = TaskRequest(messages=(
        MessageInput(
            origin=MessageOrigin(chatID="100", userID=42, messageID=7, sender="alice"),
            text="看看 https://current.example", replyText="帮我总结 https://reply.example", replySender="bob",
            imageNotes=("[图片下载失败]",),
        ),
        MessageInput(origin=MessageOrigin(chatID="100", userID=42, messageID=8, sender="alice"), text="再问一句"),
    ))
    assert request.displayPairs == [
        {"replyLine": "<@bob> 帮我总结 https://reply.example", "currentText": "[图片下载失败]\n看看 https://current.example"},
        {"replyLine": "", "currentText": "再问一句"},
    ]
    assert request.userMessage.endswith("再问一句")
    assert request.origin is request.messages[-1].origin
    assert request.triggerMessageID == 8


def test_request_display_heading_and_images():
    request = TaskRequest(messages=(MessageInput(
        origin=MessageOrigin(chatID="100", userID=42, messageID=7, sender="alice"), text="文本",
        images=({"data": "x"},),
    ),))
    assert request.displayOriginalMsg == "@alice：[附带 1 张图片]\n文本"


def test_request_display_blocks_prefix_counts_images_of_whole_batch():
    """前缀只放图片标注，张数按整批消息合计；下载失败的说明留在各自的正文里"""
    request = TaskRequest(messages=(
        MessageInput(
            origin=MessageOrigin(chatID="100", userID=42, messageID=7, sender="alice"),
            text="看图", replyText="原文", replySender="bob",
            images=({"data": "x"}, {"data": "y"}),
            imageNotes=("[图片下载失败]",),
        ),
        MessageInput(
            origin=MessageOrigin(chatID="100", userID=42, messageID=8, sender="alice"),
            text="再问一句",
            images=({"data": "z"},),
        ),
    ))

    blocks = request.displayBlocks

    assert isinstance(blocks, DisplayBlocks)
    assert blocks.prefix == "[附带 3 张图片]"
    assert blocks.pairs == [
        {"replyLine": "<@bob> 原文", "currentText": "[图片下载失败]\n看图"},
        {"replyLine": "", "currentText": "再问一句"},
    ]
    assert blocks.pairs == request.displayPairs


def test_request_display_blocks_without_images_has_empty_prefix():
    """没有下载到图片时前缀为空，即使消息带着图片下载失败的说明"""
    request = TaskRequest(messages=(MessageInput(
        origin=MessageOrigin(chatID="100", userID=42, messageID=7, sender="alice"),
        text="文本", replyText="原文", replySender="bob",
        imageNotes=("[图片下载失败]",),
    ),))

    blocks = request.displayBlocks

    assert blocks.prefix == ""
    assert blocks.pairs == [{"replyLine": "<@bob> 原文", "currentText": "[图片下载失败]\n文本"}]
    assert blocks.pairs == request.displayPairs




# ============================================================================
# runRequest
# ============================================================================

async def test_run_task_prepares_model_input_from_request(modelEnv):
    """没有反馈时，请求里的消息、身份、图片、记忆查询和扩展块原样用于准备模型输入"""
    request = _request(includeContext=True)

    outcome = await runRequest(request, capabilities=[])

    assert outcome.status == TaskStatus.COMPLETED
    modelEnv.prepare.assert_awaited_once_with(
        request.userMessage,
        "100",
        includeContext=True,
        userID=42,
        images=[{"data": "image-data", "mimeType": "image/jpeg"}],
        memoryQuery=request.memoryQuery,
        extraBlocks=[(1, "扩展", "扩展内容")],
    )


async def test_run_task_passes_none_when_request_has_no_images_or_memory_turn(modelEnv):
    request = _request(memoryTurn=False, images=False)

    await runRequest(request, capabilities=[], feedback="补充条件")

    kwargs = modelEnv.prepare.await_args.kwargs
    assert kwargs["images"] is None
    assert kwargs["memoryQuery"] is None


async def test_run_task_feedback_is_used_for_this_task_only(modelEnv):
    """反馈接在用户消息之后并写进记忆查询；保存的请求不变"""
    request = _request(includeContext=True)
    originalMessage = request.userMessage

    await runRequest(request, capabilities=[], feedback="  补充条件  ")

    args, kwargs = modelEnv.prepare.await_args
    assert args[0] == originalMessage + "\n\n[背景信息补充：补充条件]"
    assert "审核" not in args[0]
    assert kwargs["memoryQuery"].feedbackText == "补充条件"
    assert kwargs["memoryQuery"].turns == request.memoryQuery.turns
    assert request.userMessage == originalMessage
    assert request.memoryQuery.feedbackText == ""


async def test_run_task_identity_comes_from_request(modelEnv):
    """能力拿到的 chatID / userID / 触发消息 ID 来自请求，chatID 统一为字符串"""
    handled = []
    modelEnv.outputs = [_batch({"op": "probe", "chat_id": "999", "user_id": "999"})]
    request = _request(includeContext=True, chatID=-100123)

    outcome = await runRequest(request, capabilities=[_probeCapability("probe", handled)])

    assert outcome.status == TaskStatus.COMPLETED
    [(operation, task)] = handled
    assert operation.args == {"chat_id": "999", "user_id": "999"}
    assert task.chatID == "-100123"
    assert task.userID == 42
    assert task.triggerMessageID == 7
    assert task.includeContext is True
    assert str(UUID(task.taskID)) == task.taskID
    assert outcome.taskID == task.taskID
    assert str(UUID(outcome.contextID)) == outcome.contextID


async def test_run_task_prepare_failure_is_reported_in_outcome(modelEnv):
    """准备模型输入抛异常时不向外抛：任务失败，error 是原异常，没有请求过模型"""
    error = RuntimeError("context unavailable")
    modelEnv.prepare.side_effect = error

    outcome = await runRequest(_request(), capabilities=[])

    assert outcome.status == TaskStatus.FAILED
    assert outcome.error is error
    assert outcome.detail == "准备模型输入失败：RuntimeError"
    assert outcome.records == ()
    assert outcome.modelCalls == 0
    assert modelEnv.requests == []


async def test_run_task_model_request_failure_is_reported_in_outcome(modelEnv):
    error = TimeoutError("provider timeout")
    modelEnv.outputs = [error]

    outcome = await runRequest(_request(), capabilities=[])

    assert outcome.status == TaskStatus.FAILED
    assert outcome.error is error
    assert outcome.modelCalls == 1


async def test_run_task_appends_registered_capabilities(modelEnv, monkeypatch):
    """登记的外部能力追加在调用方能力之后，模型可以直接使用"""
    handled = []
    callerCapability = _probeCapability("caller", handled)
    externalCapability = _probeCapability("external", handled)
    monkeypatch.setattr(taskModule, "listCapabilities", lambda: [externalCapability])
    modelEnv.outputs = [_batch({"op": "external", "query": "x"}, {"op": "caller"})]

    outcome = await runRequest(_request(), capabilities=[callerCapability])

    assert outcome.status == TaskStatus.COMPLETED
    assert [operation.name for operation, _ in handled] == ["external", "caller"]
    instructionText = "\n".join(modelEnv.requests[0].instructions)
    assert instructionText.index("【caller】测试能力") < instructionText.index("【external】测试能力")


async def test_run_task_reads_registered_capabilities_per_task(modelEnv, monkeypatch):
    """外部能力在每个任务开始时读取，而不是模块加载时固定下来"""
    handled = []
    registered = []
    monkeypatch.setattr(taskModule, "listCapabilities", lambda: list(registered))
    modelEnv.outputs = [_batch({"op": "external"}), _batch()]

    first = await runRequest(_request(), capabilities=[])
    registered.append(_probeCapability("external", handled))
    modelEnv.outputs = [_batch({"op": "external"})]
    second = await runRequest(_request(), capabilities=[])

    assert first.status == TaskStatus.COMPLETED
    assert first.modelCalls == 2
    assert second.status == TaskStatus.COMPLETED
    assert second.modelCalls == 1
    assert len(handled) == 1


async def test_run_task_prepares_model_input_once_and_reuses_it(modelEnv):
    """多轮任务只准备一次模型输入；后续请求复用它，只有操作记录在变"""
    handled = []
    modelEnv.outputs = [
        _batch({"op": "probe", "step": 1}, end=False),
        _batch({"op": "probe", "step": 2}, end=False),
        _batch(),
    ]

    outcome = await runRequest(_request(), capabilities=[_probeCapability("probe", handled)])

    assert outcome.status == TaskStatus.COMPLETED
    assert outcome.modelCalls == 3
    assert modelEnv.prepare.await_count == 1
    assert all(request.modelInput is modelEnv.modelInput for request in modelEnv.requests)
    assert modelEnv.requests[0].appendix == ""
    assert "<OPERATION_LOG>" in modelEnv.requests[1].appendix
    assert '"step": 1' in modelEnv.requests[1].appendix
    assert '"step": 2' in modelEnv.requests[2].appendix
    assert modelEnv.requests[0].instructions == modelEnv.requests[2].instructions




async def test_runRequestPersistsResultsAndFinalState(modelEnv, contextStore):
    handled = []
    modelEnv.outputs = [_batch({"op": "probe", "value": "参数"})]

    outcome = await runRequest(_request(), capabilities=[_probeCapability("probe", handled)])

    records = await contextStore.readRecords(outcome.contextID)
    assert records[0]["status"] == "done"
    assert records[0]["content"]["args"] == {"value": "参数"}
    rows = await contextStore._db.run(lambda conn: [dict(row) for row in conn.execute("SELECT * FROM tasks")])
    assert [(row["taskID"], row["status"], row["modelCalls"]) for row in rows] == [
        (outcome.taskID, "completed", 1),
    ]


@pytest.mark.parametrize("stage", ["initialize", "createContext", "createTask"])
async def test_runRequestStorageFailureBlocksModel(modelEnv, contextStore, monkeypatch, stage):
    error = OSError("storage unavailable")
    monkeypatch.setattr(contextStore, stage, AsyncMock(side_effect=error))

    outcome = await runRequest(_request(), capabilities=[])

    assert outcome.status == TaskStatus.FAILED
    assert outcome.error is error
    modelEnv.prepare.assert_not_awaited()
    assert modelEnv.requests == []


async def test_runRequestFinalWriteFailurePreservesExecutedResult(modelEnv, contextStore, monkeypatch):
    handled = []
    modelEnv.outputs = [_batch({"op": "probe"})]
    monkeypatch.setattr(contextStore, "finishTask", AsyncMock(side_effect=OSError("disk full")))

    outcome = await runRequest(_request(), capabilities=[_probeCapability("probe", handled)])

    assert outcome.status == TaskStatus.FAILED
    assert len(handled) == 1
    assert outcome.records[0].result.status == OperationStatus.DONE
    assert (await contextStore.readRecords(outcome.contextID))[0]["status"] == "done"


@pytest.mark.parametrize("stage", ["createTask", "prepare", "operation", "finishTask"])
async def test_runRequestCancellationFinishesStoredTask(modelEnv, contextStore, monkeypatch, stage):
    """在创建、准备、执行和结束写入处取消，任务终态与实际模型调用次数保持一致。"""
    entered = asyncio.Event()
    release = asyncio.Event()
    handled = []
    capability = _probeCapability("probe", handled)

    if stage in {"createTask", "finishTask"}:
        original = getattr(contextStore, stage)

        async def delayed(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)

        monkeypatch.setattr(contextStore, stage, delayed)
    elif stage == "prepare":
        async def prepare(*args, **kwargs):
            entered.set()
            await release.wait()
        modelEnv.prepare.side_effect = prepare
    else:
        async def handle(operation, task):
            entered.set()
            await release.wait()
            return OperationResult(status=OperationStatus.DONE)
        capability = Capability(name="probe", describe=capability.describe, validate=capability.validate, handle=handle)
        modelEnv.outputs = [_batch({"op": "probe"})]

    pending = asyncio.create_task(runRequest(_request(), capabilities=[capability]))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(pending, 2)

    rows = await contextStore._db.run(lambda conn: [dict(row) for row in conn.execute("SELECT * FROM tasks")])
    assert len(rows) == 1
    assert rows[0]["status"] == ("completed" if stage == "finishTask" else "interrupted")
    assert rows[0]["modelCalls"] == (1 if stage in {"operation", "finishTask"} else 0)
    if stage == "operation":
        assert (await contextStore.readRecords(rows[0]["contextID"]))[0]["status"] == "uncertain"


async def test_runRequestSharedContextReusedForIncludeContext(modelEnv, contextStore):
    """includeContext=True 的请求共享聊天的当前上下文；False 的请求各自新建。"""
    sharedFirst = await runRequest(_request(includeContext=True), capabilities=[])
    sharedSecond = await runRequest(_request(includeContext=True), capabilities=[])
    alone = await runRequest(_request(includeContext=False), capabilities=[])

    assert sharedFirst.contextID == sharedSecond.contextID
    assert alone.contextID != sharedFirst.contextID
    rows = await contextStore._db.run(lambda conn: [dict(row) for row in conn.execute("SELECT * FROM currentContexts")])
    assert [row["chatID"] for row in rows] == ["100"]
    assert rows[0]["contextID"] == sharedFirst.contextID

    # 独立上下文不建执行门，只有共享上下文留下门条目
    from utils.llm.context import runtime
    assert set(runtime._contextGates) == {sharedFirst.contextID}


async def test_runRequestSerializesSharedContext(modelEnv, contextStore):
    """同一上下文的任务串行：第二个任务的输入准备在第一个任务结束写入之后才开始。"""
    events = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def handle(operation, task):
        events.append("first-handle")
        entered.set()
        await release.wait()
        return OperationResult(status=OperationStatus.DONE)

    capability = Capability(
        name="probe", describe=lambda task: "【probe】测试能力",
        validate=_probeCapability("probe", []).validate, handle=handle,
    )
    modelEnv.outputs = [_batch({"op": "probe"})]

    # 直接返回模型输入而不是回敲原 mock：给 side_effect 设包装会让原 mock 再触发包装，无限递归
    async def secondPrepare(*args, **kwargs):
        events.append("second-prepare")
        return modelEnv.modelInput

    first = asyncio.create_task(runRequest(_request(includeContext=True), capabilities=[capability]))
    await asyncio.wait_for(entered.wait(), 2)
    # 第一个任务的模型输出已消费（卡在处理中）；排队的第二个任务改用直接结束的输出
    modelEnv.outputs = [_batch()]
    modelEnv.prepare.side_effect = secondPrepare
    second = asyncio.create_task(runRequest(_request(includeContext=True), capabilities=[]))

    await asyncio.sleep(0.05)
    assert events == ["first-handle"], "第一个任务未结束时，排队任务不应开始准备输入"

    release.set()
    firstOutcome, secondOutcome = await asyncio.gather(first, second)
    assert firstOutcome.status == TaskStatus.COMPLETED
    assert secondOutcome.status == TaskStatus.COMPLETED
    assert events == ["first-handle", "second-prepare"]


async def test_runRequestStopInvalidatesQueuedRequest(modelEnv, contextStore):
    """停止运行中的任务时，同上下文排队未开始的请求作废：返回 STOPPED 且不留任务记录。"""
    from utils.llm.context.runtime import listRunningTasks, stopTask

    entered = asyncio.Event()
    release = asyncio.Event()

    async def handle(operation, task):
        entered.set()
        await release.wait()
        return OperationResult(status=OperationStatus.DONE)

    capability = Capability(
        name="probe", describe=lambda task: "【probe】测试能力",
        validate=_probeCapability("probe", []).validate, handle=handle,
    )
    # end=False：操作在停止信号到来时进行中（不打断），返回后续跑在循环顶部停下
    modelEnv.outputs = [_batch({"op": "probe"}, end=False)]

    first = asyncio.create_task(runRequest(_request(includeContext=True), capabilities=[capability]))
    await asyncio.wait_for(entered.wait(), 2)
    second = asyncio.create_task(runRequest(_request(includeContext=True), capabilities=[]))
    await asyncio.sleep(0.05)

    stoppedID = stopTask(listRunningTasks()[0]["taskID"])
    release.set()
    firstOutcome, secondOutcome = await asyncio.gather(first, second)

    assert stoppedID == firstOutcome.taskID
    assert firstOutcome.status == TaskStatus.STOPPED
    assert secondOutcome.status == TaskStatus.STOPPED
    assert "排队" in secondOutcome.detail
    modelEnv.prepare.assert_awaited_once(), "排队的请求作废后不应准备模型输入"

    rows = await contextStore._db.run(lambda conn: [dict(row) for row in conn.execute("SELECT * FROM tasks")])
    assert [row["taskID"] for row in rows] == [firstOutcome.taskID]
    assert rows[0]["status"] == "stopped"


async def test_runRequestInjectsPersistedHistoryIntoModelInput(modelEnv, contextStore):
    """共享上下文的后续任务把前序任务的操作记录作为扩展块注入模型输入。"""
    from utils.llm.config import ContextTier

    handled = []
    modelEnv.outputs = [_batch({"op": "probe", "value": "第一次"})]
    firstOutcome = await runRequest(
        _request(includeContext=True), capabilities=[_probeCapability("probe", handled)],
    )
    assert firstOutcome.status == TaskStatus.COMPLETED

    modelEnv.prepare.reset_mock()
    modelEnv.outputs = [_batch()]
    await runRequest(_request(includeContext=True), capabilities=[])

    kwargs = modelEnv.prepare.await_args.kwargs
    assert kwargs["extraBlocks"][0] == (1, "扩展", "扩展内容")
    tier, source, text = kwargs["extraBlocks"][1]
    assert (tier, source) == (ContextTier.LOW_TRUST, "工作上下文")
    assert "<WORK_CONTEXT_HISTORY>" in text
    assert "#1 probe → 已完成" in text
    assert "第一次" in text


async def test_runRequestHistoryReadFailureDegradesToNoBlock(modelEnv, contextStore, monkeypatch):
    """历史读取失败不阻断任务：按无历史继续，扩展块保持请求原样并记 WARNING。"""
    from utils.llm.context import journal as journalModule

    historyLog = AsyncMock()
    monkeypatch.setattr(journalModule, "logSystemEvent", historyLog)
    monkeypatch.setattr(contextStore, "readRecords", AsyncMock(side_effect=OSError("unreadable")))

    outcome = await runRequest(_request(includeContext=True), capabilities=[])

    assert outcome.status == TaskStatus.COMPLETED
    assert modelEnv.prepare.await_args.kwargs["extraBlocks"] == [(1, "扩展", "扩展内容")]
    historyLog.assert_awaited_once()




# ============================================================================
# collectOutput
# ============================================================================

async def test_collect_output_collects_messages_and_memory_actions(modelEnv):
    """发言合并为一条回复，记忆操作作为 MemoryAction 交回；两者都没有被送出或执行"""
    modelEnv.outputs = [_batch(
        _message("  第一句  "),
        _memoryAdd("记住这件事", priority=2),
        _message("第二句"),
    )]

    result = await collectOutput(_request(includeContext=True))

    assert result.reply == "第一句\n\n第二句"
    assert len(result.memoryActions) == 1
    action = result.memoryActions[0]
    assert isinstance(action, MemoryAction)
    assert (action.action, action.scopeType, action.content, action.priority) == (
        "add", "global", "记住这件事", 2,
    )
    modelEnv.log.assert_not_awaited()


async def test_collect_output_reports_collected_operations_as_submitted(modelEnv):
    """收集到的发言在操作记录里是「已送审」，模型续跑时不会当作已经送达"""
    modelEnv.outputs = [_batch(_message("先说一句"), end=False), _batch()]

    result = await collectOutput(_request())

    assert result.reply == "先说一句"
    assert "#1 message → 已送审" in modelEnv.requests[1].appendix
    assert "已交给管理员审核" in modelEnv.requests[1].appendix


async def test_collect_output_collects_across_rounds(modelEnv):
    modelEnv.outputs = [
        _batch(_message("第一轮"), end=False),
        _batch(_memoryAdd("第二轮的记忆"), _message("第二轮")),
    ]

    result = await collectOutput(_request(includeContext=True))

    assert result.reply == "第一轮\n\n第二轮"
    assert [action.content for action in result.memoryActions] == ["第二轮的记忆"]


async def test_collect_output_without_message_returns_empty_reply(modelEnv):
    modelEnv.outputs = [_batch()]

    result = await collectOutput(_request(includeContext=True))

    assert result.reply == ""
    assert result.memoryActions == []


async def test_collect_output_context_off_offers_no_memory_capability(modelEnv):
    """不进聊天上下文的任务没有记忆操作能力：带记忆操作的整批不执行，也收集不到记忆操作"""
    modelEnv.outputs = [
        _batch(_message("不该被收集"), _memoryAdd("不该出现")),
        _batch(_message("回复")),
    ]

    result = await collectOutput(_request(includeContext=False))

    assert result.reply == "回复"
    assert result.memoryActions == []
    assert "memory_action" not in "\n".join(modelEnv.requests[0].instructions)
    assert "第 2 个操作（memory_action）：没有这个能力" in modelEnv.requests[1].appendix


async def test_collect_output_invalid_memory_action_rejects_whole_batch(modelEnv):
    """同批里有不合法的记忆操作时，这一批的发言也不收集；模型重新输出后才生效"""
    modelEnv.outputs = [
        _batch(_message("不该被收集"), _memoryAdd("伪造归属", scope_type="user", scope_id="999")),
        _batch(_message("回复"), _memoryAdd("用户事实", scope_type="user", scope_id="42")),
    ]

    result = await collectOutput(_request(includeContext=True))

    assert result.reply == "回复"
    assert [(action.scopeType, action.scopeID) for action in result.memoryActions] == [("user", "42")]
    assert "不属于当前请求" in modelEnv.requests[1].appendix


async def test_collect_output_passes_feedback_to_the_task(modelEnv):
    modelEnv.outputs = [_batch(_message("回复"))]
    request = _request(includeContext=True)

    result = await collectOutput(request, feedback="补充条件")

    assert result.reply == "回复"
    args, kwargs = modelEnv.prepare.await_args
    assert args[0] == request.userMessage + "\n\n[背景信息补充：补充条件]"
    assert kwargs["memoryQuery"].feedbackText == "补充条件"


async def test_collect_output_raises_original_error_when_prepare_fails(modelEnv):
    error = RuntimeError("context unavailable")
    modelEnv.prepare.side_effect = error

    with pytest.raises(RuntimeError) as excInfo:
        await collectOutput(_request())

    assert excInfo.value is error


async def test_collect_output_raises_original_error_when_model_request_fails(modelEnv):
    """请求模型失败时抛出原异常，之前轮次收集到的内容不交回"""
    error = TimeoutError("provider timeout")
    modelEnv.outputs = [_batch(_message("第一轮"), end=False), error]

    with pytest.raises(TimeoutError) as excInfo:
        await collectOutput(_request())

    assert excInfo.value is error


async def test_collect_output_returns_collected_content_when_limit_reached(modelEnv):
    """模型一直不给出结束信号：到调用上限后交回已收集的内容，并记一条 WARNING"""
    modelEnv.outputs = [
        _batch(_message("先说一句"), _memoryAdd("记住这件事"), end=False),
        "这段文字没有操作块",
    ]

    result = await collectOutput(_request(includeContext=True), logLabel="feedback retry")

    assert result.reply == "先说一句"
    assert [action.content for action in result.memoryActions] == ["记住这件事"]
    assert len(modelEnv.requests) == LLM_TASK_MAX_MODEL_CALLS
    modelEnv.log.assert_awaited_once()
    event, detail, level = modelEnv.log.await_args.args
    assert event == "LLM feedback retry 任务未正常完成"
    assert "chatID=100" in detail
    assert "status=limitReached" in detail
    assert f"已调用模型 {LLM_TASK_MAX_MODEL_CALLS} 次" in detail
    assert level == LogLevel.WARNING
