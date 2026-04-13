"""
utils/llm/client/

多模型 LLM 客户端。
按模型名前缀路由至 Anthropic / Gemini / OpenAI / DeepSeek / 豆包等 provider。
对外导出 generateReply 和 requestReply。
"""

import asyncio

from ._router import getProvider
from ._guardrails import SYSTEM_GUARDRAILS, MEMORY_ACTION_INSTRUCTIONS
from ..config import getModel, loadPrompts
from ..contextBuilder import buildConversationContext
from config import LLM_REQUEST_MAX_RETRIES, LLM_REQUEST_RETRY_DELAY
from utils.logger import logSystemEvent, LogLevel, LogChildType




def _buildSystemMessages(prompts: dict) -> list[str]:
    """将 prompts.json 的 system_prompt + guardrails 合并为 list[str]。"""
    raw = prompts.get("system_prompt", "")
    if isinstance(raw, list):
        parts = [s for s in raw if isinstance(s, str) and s.strip()]
    elif isinstance(raw, str) and raw.strip():
        parts = [raw]
    else:
        parts = [str(raw)]

    parts.extend(SYSTEM_GUARDRAILS)
    parts.extend(MEMORY_ACTION_INSTRUCTIONS)
    return parts




async def _requestWithRetry(
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

    向后兼容接口：systemBlocks 仍为 list[dict]（Anthropic 格式），
    内部合并为 list[str] 后委托给对应 provider。
    """
    # 将 Anthropic 格式的 systemBlocks 退化为 list[str]
    systemMessages = [
        block.get("text", "") if isinstance(block, dict) else str(block)
        for block in systemBlocks
    ]
    systemMessages = [s for s in systemMessages if s.strip()]

    model = getModel()
    provider = getProvider(model)
    return await _requestWithRetry(
        provider,
        systemMessages=systemMessages,
        userContent=userContent,
        model=model,
        maxTokens=maxTokens,
        temperature=temperature,
    )




async def generateReply(
    userMessage: str,
    chatID: str,
    includeContext: bool = False,
    *,
    userID: str | int | None = None,
    sessionID: str | int | None = None,
    images: list[dict] | None = None,
) -> str:
    """
    调用 LLM 生成回复。

    参数:
        images: 图片列表 [{"data": b64_str, "mimeType": "image/jpeg"}, ...]
                为 None 或空列表时表示纯文本。
    """
    prompts = loadPrompts()
    systemMessages = _buildSystemMessages(prompts)
    maxTokens = prompts.get("max_tokens", 1024)
    temperature = prompts.get("temperature", 0.8)

    # 有图片时在 system messages 末尾追加覆盖指令
    if images:
        from ._guardrails import IMAGE_SYSTEM_OVERRIDE
        systemMessages.append(IMAGE_SYSTEM_OVERRIDE)

    textContent = await buildConversationContext(
        userMessage=userMessage,
        chatID=chatID,
        userID=userID,
        sessionID=sessionID,
        includeContext=includeContext,
        hasImages=bool(images),
    )

    # 有图片时构造多模态 content list，否则保持纯文本
    if images:
        from ._guardrails import IMAGE_ANCHOR
        userContent: str | list = [
            {"type": "text", "text": IMAGE_ANCHOR},
            *[{"type": "image_base64", "data": img["data"], "mimeType": img["mimeType"]} for img in images],
            {"type": "text", "text": textContent},
        ]
    else:
        userContent = textContent

    model = getModel()
    provider = getProvider(model)
    return await _requestWithRetry(
        provider,
        systemMessages=systemMessages,
        userContent=userContent,
        model=model,
        maxTokens=maxTokens,
        temperature=temperature,
    )
