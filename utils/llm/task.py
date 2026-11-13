"""
utils/llm/task.py

任务入口：把一个请求交给操作循环跑完。首次生成与审核重试共用。

    - runRequest：运行一个任务，发言和记忆操作怎么送出由调用方给的能力决定
    - collectOutput：发言和记忆操作只收集、不送出的封装，供审核重试使用
"""

import asyncio
import uuid
from dataclasses import replace

from utils.core.logger import logSystemEvent, LogLevel
from utils.llm.capability import makeCollectingMemoryCapability, makeCollectingMessageCapability
from utils.llm.client import prepareModelInput, requestModelOutput
from utils.llm.context.journal import ContextJournal, buildHistoryBlock, getContextStore
from utils.llm.context.runtime import executionSlot, registerTask, unregisterTask
from utils.llm.models import CollectedOutput, TaskRequest
from utils.llm.operation import (
    Capability,
    TaskInfo,
    TaskOutcome,
    TaskStatus,
    listCapabilities,
    runTask,
)




async def runRequest(
    request: TaskRequest,
    *,
    capabilities: list[Capability],
    feedback: str = "",
) -> TaskOutcome:
    """
    为一个请求运行操作循环。

    参数:
        capabilities: 调用方构造的能力（发言、记忆操作）；init 时登记的外部能力
                      （如 AFC 工具）由这里追加
        feedback: 本次生成的补充信息，拼入模型输入和记忆查询，原请求保持不变

    每个请求建立工作上下文并保存执行前后的操作与结束状态；includeContext=True
    时共享聊天的当前上下文，同上下文的任务串行执行，前序任务的操作记录注入
    模型输入。模型输入准备一次，循环内每次请求复用。输入、模型或存储失败记入
    TaskOutcome（status=FAILED，error 为原异常）。
    取消会等候数据库收尾再传播，返回结果携带 taskID 与 contextID。
    """
    feedback = feedback.strip()

    userMessage = request.userMessage
    memoryQuery = request.memoryQuery
    if feedback:
        userMessage = f"{userMessage}\n\n[背景信息补充：{feedback}]"
        if memoryQuery is not None:
            memoryQuery = replace(memoryQuery, feedbackText=feedback)

    # 身份来自入口校验过的请求，能力只认这里的 chatID / userID
    task = TaskInfo(
        taskID=str(uuid.uuid4()),
        chatID=str(request.chatID),
        userID=request.userID,
        triggerMessageID=request.triggerMessageID,
        includeContext=request.includeContext,
    )

    contextID = ""
    try:
        store = getContextStore()
        await store.initialize()
        await store.checkWritable()
        contextID = await store.createContext(task.chatID, shared=request.includeContext)
    except Exception as e:
        await logSystemEvent(
            "LLM 工作上下文创建失败", f"task={task.taskID}, error={type(e).__name__}", LogLevel.ERROR,
        )
        return TaskOutcome(
            status=TaskStatus.FAILED, detail="工作上下文创建失败", error=e,
            taskID=task.taskID, contextID=contextID,
        )

    # 执行权：同一共享上下文一次只跑一个任务；排队期间被停止的请求在此作废
    # 独立上下文一次性使用，不建门（否则每个请求都会留下永久门条目）
    async with executionSlot(contextID, gated=request.includeContext) as valid:
        if not valid:
            return TaskOutcome(
                status=TaskStatus.STOPPED, detail="排队期间同上下文的任务被停止",
                taskID=task.taskID, contextID=contextID,
            )

        try:
            await _createTaskRecord(store, contextID, task.taskID)
        except Exception as e:
            await logSystemEvent(
                "LLM 工作上下文创建失败", f"task={task.taskID}, error={type(e).__name__}", LogLevel.ERROR,
            )
            return TaskOutcome(
                status=TaskStatus.FAILED, detail="工作上下文创建失败", error=e,
                taskID=task.taskID, contextID=contextID,
            )

        registerTask(task, chatID=task.chatID, contextID=contextID, preview=request.displayOriginalMsg)
        try:
            modelCalls = 0
            try:
                historyBlock = await buildHistoryBlock(store, contextID)
                extraBlocks = list(request.extraBlocks)
                if historyBlock is not None:
                    extraBlocks.append(historyBlock)
                modelInput = await prepareModelInput(
                    userMessage,
                    request.chatID,
                    includeContext=request.includeContext,
                    userID=request.userID,
                    images=request.images or None,
                    memoryQuery=memoryQuery,
                    extraBlocks=extraBlocks,
                )
            except asyncio.CancelledError:
                await _finishInterrupted(store, task.taskID, modelCalls)
                raise
            except Exception as e:
                outcome = TaskOutcome(
                    status=TaskStatus.FAILED,
                    detail=f"准备模型输入失败：{type(e).__name__}",
                    error=e,
                )
            else:
                async def _callModel(*, instructions: list[str], appendix: str) -> str:
                    nonlocal modelCalls
                    modelCalls += 1
                    return await requestModelOutput(modelInput, instructions=instructions, appendix=appendix)

                try:
                    outcome = await runTask(
                        task=task,
                        capabilities=[*capabilities, *listCapabilities()],
                        callModel=_callModel,
                        journal=ContextJournal(store, contextID, task.taskID),
                    )
                except asyncio.CancelledError:
                    await _finishInterrupted(store, task.taskID, modelCalls)
                    raise
                except Exception as e:
                    outcome = TaskOutcome(
                        status=TaskStatus.FAILED, modelCalls=modelCalls,
                        detail=f"操作循环异常：{type(e).__name__}", error=e,
                    )
        finally:
            unregisterTask(task.taskID)

        outcome = replace(outcome, taskID=task.taskID, contextID=contextID)
        finalWrite = asyncio.create_task(store.finishTask(task.taskID, outcome.status.value, outcome.modelCalls))
        try:
            await asyncio.shield(finalWrite)
        except asyncio.CancelledError:
            try:
                await _completeCleanup(finalWrite)
            except Exception as e:
                await logSystemEvent(
                    "LLM 任务状态保存失败", f"task={task.taskID}, error={type(e).__name__}", LogLevel.ERROR,
                )
            raise
        except Exception as e:
            await logSystemEvent(
                "LLM 任务状态保存失败", f"task={task.taskID}, error={type(e).__name__}", LogLevel.ERROR,
            )
            return replace(outcome, status=TaskStatus.FAILED, detail="任务结束状态保存失败", error=e)
        if outcome.error is not None:
            await logSystemEvent(
                "LLM 任务失败", f"task={task.taskID}, {outcome.detail}", LogLevel.ERROR,
            )
        return outcome


async def _createTaskRecord(store, contextID: str, taskID: str) -> None:
    """保存任务的运行初始状态；取消时等创建结束，再将已创建的任务标为中断。"""
    async def create():
        await store.createTask(contextID, taskID)

    worker = asyncio.create_task(create())
    try:
        await asyncio.shield(worker)
    except asyncio.CancelledError:
        try:
            await _completeCleanup(worker)
        except Exception:
            # 创建失败已由存储层报告；成功创建的任务才需要结束状态。
            pass
        else:
            await _finishInterrupted(store, taskID, 0)
        raise


async def _completeCleanup(worker):
    """等候收尾协程完成，连续取消也保持数据库状态收尾的执行顺序。"""
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            continue
    return worker.result()


async def _finishInterrupted(store, taskID: str, modelCalls: int):
    """取消任务时保存中断状态和调用次数；写入故障留待启动恢复。"""
    try:
        await _completeCleanup(asyncio.create_task(store.finishTask(taskID, "interrupted", modelCalls)))
    except Exception as e:
        await logSystemEvent(
            "LLM 中断状态保存失败", f"task={taskID}, error={type(e).__name__}", LogLevel.ERROR,
        )


async def collectOutput(
    request: TaskRequest,
    *,
    feedback: str = "",
    logLabel: str = "collect",
) -> CollectedOutput:
    """
    运行一个任务，但发言和记忆操作只收集、不送出；审核重试用它拿到新的回复。

    多条发言用空行合并成一条回复。工具调用等外部能力照常执行。
    准备输入或请求模型失败时抛出原异常；任务以其他方式非正常结束时（达到调用
    上限等）交回已收集到的内容，并记一条 WARNING。
    """
    texts: list[str] = []
    actions: list = []

    outcome = await runRequest(
        request,
        capabilities=[
            makeCollectingMessageCapability(texts),
            makeCollectingMemoryCapability(actions),
        ],
        feedback=feedback,
    )

    if outcome.status == TaskStatus.FAILED and outcome.error is not None:
        raise outcome.error
    if outcome.status != TaskStatus.COMPLETED:
        await logSystemEvent(
            f"LLM {logLabel} 任务未正常完成",
            f"chatID={request.chatID}, status={outcome.status.value}, {outcome.detail}",
            LogLevel.WARNING,
        )

    return CollectedOutput(reply="\n\n".join(texts), memoryActions=actions)
