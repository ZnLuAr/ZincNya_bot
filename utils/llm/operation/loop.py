"""
utils/llm/operation/loop.py

操作循环：调用模型 → 整批解析校验 → 按序处理 → 记录实际结果 → 续跑或结束。

循环不知道任何具体能力，也不知道模型怎么调用：能力以 Capability 列表传入，
模型调用以 callModel 回调传入。它只负责顺序、预算和记录。
"""

import asyncio
from typing import Awaitable, Callable

from config import LLM_TASK_MAX_MODEL_CALLS

from utils.core.logger import logSystemEvent, LogLevel

from .parser import BatchFormatError, parseBatch
from .prompt import buildOperationInstructions, renderAppendix
from .types import (
    Capability,
    Operation,
    OperationJournal,
    OperationRecord,
    OperationResult,
    OperationStatus,
    TaskInfo,
    TaskOutcome,
    TaskStatus,
)




# 这两种结果算「这个操作成功了」，同批后续操作可以继续
_SUCCESS_STATUSES = (OperationStatus.DONE, OperationStatus.SUBMITTED)

_LOG_DETAIL_LEN = 200




async def runTask(
    *,
    task: TaskInfo,
    capabilities: list[Capability],
    callModel: Callable[..., Awaitable[str]],
    maxModelCalls: int = LLM_TASK_MAX_MODEL_CALLS,
    journal: OperationJournal | None = None,
) -> TaskOutcome:
    """
    运行一个任务直到结束。

    参数:
        capabilities: 本任务可用的能力；说明为空的能力不提供给模型，
                      同名的能力以排在前面的为准
        callModel: async (*, instructions: list[str], appendix: str) -> str
                   instructions 是操作说明（进 system 消息），appendix 是操作记录与
                   未执行原因（附在 user 内容末尾，首次调用为空串）
        journal: 执行前后保存操作与结果；保存失败结束任务并保留已发生的实际结果

    模型请求抛出的异常不向外抛，记入 TaskOutcome.error；CancelledError 照常传播。
    """
    # 能力说明在任务开始时取一次，中途不刷新
    active: dict[str, Capability] = {}
    descriptions = []
    for capability in capabilities:
        description = await _describeOne(capability, task)
        if not description:
            continue
        if capability.name in active:
            await logSystemEvent(
                "LLM 能力重名", f"task={task.taskID}, {capability.name} 已有同名能力，后一个不提供",
                LogLevel.WARNING,
            )
            continue
        active[capability.name] = capability
        descriptions.append(description)
    instructions = buildOperationInstructions(descriptions)

    records: list[OperationRecord] = []
    rejectReason = ""
    modelCalls = 0

    def _outcome(status: TaskStatus, detail: str = "", error: Exception | None = None) -> TaskOutcome:
        return TaskOutcome(
            status=status, records=tuple(records), modelCalls=modelCalls,
            detail=detail, error=error,
        )

    while modelCalls < maxModelCalls:
        if task.stopEvent.is_set():
            return _outcome(TaskStatus.STOPPED, "在调用模型前收到停止信号")

        if journal is not None:
            try:
                journal.checkReady()
            except Exception as e:
                return _outcome(TaskStatus.FAILED, "工作上下文存储不可用", e)

        # ── 调用模型 ──
        modelCalls += 1
        try:
            output = await callModel(
                instructions=instructions,
                appendix=renderAppendix(records, rejectReason),
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            return _outcome(TaskStatus.FAILED, f"模型请求失败：{type(e).__name__}", e)
        rejectReason = ""
        if task.stopEvent.is_set():
            return _outcome(TaskStatus.STOPPED, "模型请求返回后收到停止信号")

        # ── 整批解析与校验：任何一处不合法，整批零执行 ──
        try:
            batch = parseBatch(output)
        except BatchFormatError as e:
            rejectReason = str(e)
            await _logRejected(task, modelCalls, rejectReason)
            continue

        problems = []
        for position, (name, args) in enumerate(batch.operations, start=1):
            reason = await _validateOne(active, name, args, task)
            if reason:
                problems.append(f"第 {position} 个操作（{name}）：{reason}")
        if problems:
            rejectReason = "\n".join(problems)
            await _logRejected(task, modelCalls, rejectReason)
            continue

        # ── 按序处理：前一个没成功，后面的不再开始 ──
        batchSucceeded = True
        for name, args in batch.operations:
            operation = Operation(index=len(records) + 1, name=name, args=args)
            stoppedBeforeExecution = False

            if not batchSucceeded:
                result = OperationResult(status=OperationStatus.SKIPPED)
            else:
                if task.stopEvent.is_set():
                    return _outcome(TaskStatus.STOPPED, f"在操作 #{operation.index} 开始前收到停止信号")
                if journal is not None:
                    try:
                        await journal.beforeOperation(operation)
                    except Exception as e:
                        return _outcome(TaskStatus.FAILED, f"操作 #{operation.index} 执行前保存失败", e)
                if task.stopEvent.is_set():
                    stoppedBeforeExecution = True
                    result = OperationResult(status=OperationStatus.SKIPPED)
                else:
                    result = await _handleOne(active[name], operation, task)

            record = OperationRecord(operation=operation, result=result)
            records.append(record)
            if journal is not None:
                try:
                    await journal.afterOperation(record)
                except Exception as e:
                    # 实际执行结果先进入内存记录；保存失败作为任务故障另行报告。
                    return _outcome(TaskStatus.FAILED, f"操作 #{operation.index} 的结果保存失败", e)
            if stoppedBeforeExecution:
                return _outcome(TaskStatus.STOPPED, f"操作 #{operation.index} 执行前保存期间收到停止信号")
            if result.status not in _SUCCESS_STATUSES:
                batchSucceeded = False

        # 带结束信号的批次只有全部处理成功才算正常完成；否则把结果交还模型
        if batch.end and batchSucceeded:
            return _outcome(TaskStatus.COMPLETED)

    detail = f"已调用模型 {modelCalls} 次"
    if rejectReason:
        detail += f"；最后一次输出整批没有执行：{rejectReason[:_LOG_DETAIL_LEN]}"
    return _outcome(TaskStatus.LIMIT_REACHED, detail)




async def _describeOne(capability: Capability, task: TaskInfo) -> str:
    try:
        return capability.describe(task)
    except Exception as e:
        # 一个能力的说明出错不影响其他能力，本任务不提供它
        await logSystemEvent(
            "LLM 能力说明异常", f"task={task.taskID}, capability={capability.name}",
            LogLevel.ERROR, exception=e,
        )
        return ""


async def _validateOne(active: dict[str, Capability], name: str, args: dict, task: TaskInfo) -> str | None:
    capability = active.get(name)
    if capability is None:
        available = "、".join(active) or "无"
        return f"没有这个能力；可用的能力：{available}"

    try:
        return await capability.validate(args, task)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        # 校验没有副作用，出错时按不合法处理即可
        await logSystemEvent(
            "LLM 操作校验异常", f"task={task.taskID}, op={name}",
            LogLevel.ERROR, exception=e,
        )
        return f"校验时出错（{type(e).__name__}）"


async def _handleOne(capability: Capability, operation: Operation, task: TaskInfo) -> OperationResult:
    try:
        result = await capability.handle(operation, task)
        if not isinstance(result, OperationResult):
            raise TypeError(f"handle 应返回 OperationResult，实际是 {type(result).__name__}")
        return result
    except asyncio.CancelledError:
        raise
    except Exception as e:
        # 处理阶段可能已经产生副作用，异常时无法确认做到了哪一步
        await logSystemEvent(
            "LLM 操作处理异常", f"task={task.taskID}, #{operation.index} {operation.name}",
            LogLevel.ERROR, exception=e,
        )
        return OperationResult(
            status=OperationStatus.UNCERTAIN,
            modelText=f"处理时出错（{type(e).__name__}），无法确认是否已经生效",
        )


async def _logRejected(task: TaskInfo, modelCalls: int, reason: str) -> None:
    """为被拒绝的批次记下日志——通常是因为模型的输出格式错误而被拒绝。"""
    await logSystemEvent(
        "LLM 操作批次未执行",
        f"task={task.taskID}, 第 {modelCalls} 次调用：{reason[:_LOG_DETAIL_LEN]}",
        LogLevel.WARNING,
    )
