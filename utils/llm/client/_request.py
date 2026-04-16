"""
utils/llm/client/_request.py

LLM 请求发送与重试逻辑。
"""

import asyncio

from config import LLM_REQUEST_MAX_RETRIES, LLM_REQUEST_RETRY_DELAY

from utils.logger import logSystemEvent, LogLevel, LogChildType

from ..config import getModel
from ._router import getProvider




async def requestWithRetry(
    provider,
    *,
    systemMessages: list[str],
    userContent: str | list,
    model: str,
    maxTokens: int,
    temperature: float,
) -> str:
    """
    带重试的 LLM 请求。

    遇到网络超时等瞬时错误时自动重试，最多 LLM_REQUEST_MAX_RETRIES 次。
    非瞬时错误（如 API key 无效、模型不存在）直接抛出。
    """
    lastErr: Exception | None = None
    for attempt in range(1 + LLM_REQUEST_MAX_RETRIES):
        try:
            return await provider.requestReply(
                systemMessages=systemMessages,
                userContent=userContent,
                model=model,
                maxTokens=maxTokens,
                temperature=temperature,
            )
        except Exception as e:
            lastErr = e
            # 判断是否为可重试的瞬时错误
            if not _isRetryable(e):
                raise
            if attempt < LLM_REQUEST_MAX_RETRIES:
                await logSystemEvent(
                    "LLM 请求失败，准备重试",
                    f"第 {attempt + 1} 次失败: {type(e).__name__}: {e}",
                    LogLevel.WARNING,
                    LogChildType.WITH_ONE_CHILD,
                )
                await asyncio.sleep(LLM_REQUEST_RETRY_DELAY)
    raise lastErr  # type: ignore[misc]


def _isRetryable(e: Exception) -> bool:
    """判断异常是否为可重试的瞬时错误（超时、连接断开等）。"""
    errName = type(e).__name__
    errStr = str(e).lower()

    # 超时类
    if "timeout" in errName.lower() or "timeout" in errStr:
        return True
    # 连接类
    if any(kw in errStr for kw in ("connection", "disconnected", "reset", "broken pipe", "524", "502", "503")):
        return True
    # httpx / aiohttp 网络错误
    if any(kw in errName for kw in ("RemoteProtocolError", "ConnectError", "ReadError", "NetworkError")):
        return True
    # Anthropic / OpenAI 特定可重试错误
    if "overloaded" in errStr or "rate_limit" in errStr:
        return True

    return False


async def requestReply(
    *,
    systemBlocks: list[dict],
    userContent: str | list,
    maxTokens: int,
    temperature: float,
) -> str:
    """
    发送底层 LLM 请求并返回文本回复。

    接受 Anthropic 格式的 systemBlocks (list[dict])，
    内部合并为 list[str] 后委托给对应 provider。
    供 chatScreen / console 等外部调用方使用。
    """
    # 将 Anthropic 格式的 systemBlocks 退化为 list[str]
    systemMessages = [
        block.get("text", "") if isinstance(block, dict) else str(block)
        for block in systemBlocks
    ]
    systemMessages = [s for s in systemMessages if s.strip()]

    model = getModel()
    provider = getProvider(model)
    return await requestWithRetry(
        provider,
        systemMessages=systemMessages,
        userContent=userContent,
        model=model,
        maxTokens=maxTokens,
        temperature=temperature,
    )
