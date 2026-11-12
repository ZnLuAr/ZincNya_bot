"""
utils/llm/task.py

任务入口：把一个请求交给操作循环跑完。首次生成与审核重试共用。

    - runRequest：运行一个任务，发言和记忆操作怎么送出由调用方给的能力决定
    - collectOutput：发言和记忆操作只收集、不送出的封装，供审核重试使用
"""

import uuid
from dataclasses import replace

from config import LLM_REVIEW_FEEDBACK_MAX_LENGTH

from utils.core.logger import logSystemEvent, LogLevel
from utils.llm.capability import makeCollectingMemoryCapability, makeCollectingMessageCapability
from utils.llm.client import prepareModelInput, requestModelOutput
from utils.llm.models import CollectedOutput, TaskRequest
from utils.llm.operation import (
    Capability,
    TaskInfo,
    TaskOutcome,
    TaskStatus,
    listCapabilities,
    runTask,
)


_TASK_ID_LEN = 8




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
        feedback: 审核人的补充反馈，只用于本次任务，不写回请求

    模型输入只准备一次，循环内每次请求复用。准备输入或请求模型失败时不抛出，
    记入 TaskOutcome（status=FAILED，error 为原异常）；feedback 超长抛 ValueError。
    """
    feedback = feedback.strip()
    if len(feedback) > LLM_REVIEW_FEEDBACK_MAX_LENGTH:
        raise ValueError(
            f"反馈过长喵（{len(feedback)}/{LLM_REVIEW_FEEDBACK_MAX_LENGTH} 字），请精简后重试"
        )

    userMessage = request.userMessage
    memoryQuery = request.memoryQuery
    if feedback:
        userMessage = f"{userMessage}\n\n[背景信息补充：{feedback}]"
        if memoryQuery is not None:
            memoryQuery = replace(memoryQuery, feedbackText=feedback)

    # 身份来自入口校验过的请求，能力只认这里的 chatID / userID
    task = TaskInfo(
        taskID=uuid.uuid4().hex[:_TASK_ID_LEN],
        chatID=str(request.chatID),
        userID=request.userID,
        triggerMessageID=request.triggerMessageID,
        includeContext=request.includeContext,
    )

    try:
        modelInput = await prepareModelInput(
            userMessage,
            request.chatID,
            includeContext=request.includeContext,
            userID=request.userID,
            images=request.images or None,
            memoryQuery=memoryQuery,
            extraBlocks=list(request.extraBlocks),
        )
    except Exception as e:
        return TaskOutcome(
            status=TaskStatus.FAILED,
            detail=f"准备模型输入失败：{type(e).__name__}",
            error=e,
        )

    async def _callModel(*, instructions: list[str], appendix: str) -> str:
        return await requestModelOutput(modelInput, instructions=instructions, appendix=appendix)

    return await runTask(
        task=task,
        capabilities=[*capabilities, *listCapabilities()],
        callModel=_callModel,
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
