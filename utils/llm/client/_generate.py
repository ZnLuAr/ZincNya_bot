"""
utils/llm/client/_generate.py

LLM 回复生成编排逻辑：
    - 构建 system messages（人设 + guardrails）
    - 双调用架构：图片描述 + 主调用
"""

from utils.logger import logSystemEvent, LogLevel, LogChildType

from ..config import getModel, getVisionModel, loadPrompts
from ..contextBuilder import buildConversationContext
from ._guardrails import SYSTEM_GUARDRAILS, MEMORY_ACTION_INSTRUCTIONS, VISION_DESCRIBE_PROMPT
from ._request import requestWithRetry
from ._router import getProvider


_VISION_MAX_TOKENS = 4096
_VISION_TEMPERATURE = 0.2




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




async def _describeImages(
    images: list[dict],
) -> str:
    """
    双调用架构 — 第一步：轻量视觉调用。

    使用无人设的 VISION_DESCRIBE_PROMPT，让 LLM 客观描述图片内容。
    不附带用户文本，避免模型根据文本编造图片内容。
    返回的描述文本将作为纯文本注入主调用的上下文中。
    失败时返回空字符串，不阻断主调用。
    """
    model = getVisionModel()
    provider = getProvider(model)

    await logSystemEvent(
        "LLM 图片描述调用",
        f"model={model}, provider={type(provider).__name__}, images={len(images)}",
        LogLevel.INFO,
        LogChildType.WITH_ONE_CHILD,
    )

    try:
        userContent: list = [
            *[{"type": "image_base64", "data": img["data"], "mimeType": img["mimeType"]} for img in images],
            {"type": "text", "text": "请详细描述以上图片中的所有内容。"},
        ]

        description = await requestWithRetry(
            provider,
            systemMessages=VISION_DESCRIBE_PROMPT,
            userContent=userContent,
            model=model,
            maxTokens=_VISION_MAX_TOKENS,
            temperature=_VISION_TEMPERATURE,
        )
    except Exception as e:
        await logSystemEvent(
            "LLM 图片描述失败",
            f"{type(e).__name__}: {e}",
            LogLevel.WARNING,
            LogChildType.WITH_ONE_CHILD,
        )
        return ""

    preview = description[:200] if description else "(空)"
    await logSystemEvent(
        "LLM 图片描述完成",
        f"生成描述: {len(description)} 字符 | {preview}",
        LogLevel.INFO,
        LogChildType.WITH_ONE_CHILD,
    )

    return description




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

    有图片时采用双调用架构：
        1. 轻量视觉调用：无人设 prompt，获取客观图片描述
        2. 主调用：完整人设 + 图片描述文本，生成角色化回复
    """
    prompts = loadPrompts()
    systemMessages = _buildSystemMessages(prompts)
    maxTokens = prompts.get("max_tokens", 1024)
    temperature = prompts.get("temperature", 0.8)

    # 双调用：先获取图片描述，再将描述作为纯文本注入主调用
    imageDescription: str | None = None
    if images:
        imageDescription = await _describeImages(images)

    textContent = await buildConversationContext(
        userMessage=userMessage,
        chatID=chatID,
        userID=userID,
        sessionID=sessionID,
        includeContext=includeContext,
    )

    # 有图片时，嵌入描述上下文；主调用始终是纯文本
    if images:
        if imageDescription:
            imageBlock = (
                "<IMAGE_DESCRIPTION>\n"
                "[用户发送了图片。以下是图片内容的文字描述，请基于此描述回答用户关于图片的提问。]\n"
                f"{imageDescription}\n"
                "</IMAGE_DESCRIPTION>"
            )
        else:
            imageBlock = (
                "<IMAGE_DESCRIPTION>\n"
                "[用户发了图片，但你尝试读取时画面没有浮现。请用锌酱的语气表达轻微困惑——像是你伸手去接却没接到，不必提技术原因。]\n"
                "</IMAGE_DESCRIPTION>"
            )
        textContent = f"{imageBlock}\n\n{textContent}"

    model = getModel()
    provider = getProvider(model)
    return await requestWithRetry(
        provider,
        systemMessages=systemMessages,
        userContent=textContent,
        model=model,
        maxTokens=maxTokens,
        temperature=temperature,
    )
