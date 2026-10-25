"""统一处理首次生成与审核重试。"""

from dataclasses import replace

from config import LLM_REVIEW_FEEDBACK_MAX_LENGTH
from utils.llm.client import generateReply
from utils.llm.models import GenerationRequest, GenerationResult
from utils.llm.memory.action import extractValidatedMemoryActions




async def generateResponse(
    request: GenerationRequest,
    *,
    feedback: str = "",
    logLabel: str = "generate",
) -> GenerationResult:
    """根据可复用的请求生成回复，并校验其中的记忆操作。"""
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

    reply = await generateReply(
        userMessage,
        request.chatID,
        includeContext=request.includeContext,
        userID=request.userID,
        images=request.images or None,
        urlContexts=list(request.urlContexts) or None,
        memoryQuery=memoryQuery,
        extraBlocks=list(request.extraBlocks),
    )

    if not request.includeContext:
        return GenerationResult(reply=reply)

    reply, actions, failed = await extractValidatedMemoryActions(
        reply,
        logLabel=logLabel,
        chatID=request.chatID,
        userID=request.userID,
    )
    return GenerationResult(
        reply=reply,
        memoryActions=actions,
        memoryFailedCount=failed,
    )
