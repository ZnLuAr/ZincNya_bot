"""
utils/llm/client/

多模型 LLM 客户端。
按模型名前缀路由至 Anthropic / Gemini / OpenAI / DeepSeek / 豆包等 provider。
对外导出 generateReply 和 requestReply。
"""

from ._router import getProvider
from ._guardrails import SYSTEM_GUARDRAILS, MEMORY_ACTION_INSTRUCTIONS
from ..config import getModel, loadPrompts
from ..contextBuilder import buildConversationContext




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




async def requestReply(
    *,
    systemBlocks: list[dict],
    userContent: str,
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
    return await provider.requestReply(
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
) -> str:
    """调用 LLM 生成回复。"""
    prompts = loadPrompts()
    systemMessages = _buildSystemMessages(prompts)
    maxTokens = prompts.get("max_tokens", 1024)
    temperature = prompts.get("temperature", 0.8)

    userContent = await buildConversationContext(
        userMessage=userMessage,
        chatID=chatID,
        userID=userID,
        sessionID=sessionID,
        includeContext=includeContext,
    )

    model = getModel()
    provider = getProvider(model)
    return await provider.requestReply(
        systemMessages=systemMessages,
        userContent=userContent,
        model=model,
        maxTokens=maxTokens,
        temperature=temperature,
    )
