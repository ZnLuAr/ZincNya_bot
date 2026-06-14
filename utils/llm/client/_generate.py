"""
utils/llm/client/_generate.py

LLM 回复生成编排逻辑：
    - 构建 system messages（人设 + guardrails，按 includeContext 决定是否追加记忆操作指令）
    - 可选双调用架构：visionModel != model 时分离描述与回复，否则单次调用
    - 通过 urlContexts 透传低信任 URL 内容到 contextBuilder
"""

from utils.core.logger import logSystemEvent, LogLevel, LogChildType

from ..config import getForceFallbackPrompt, loadPrompts, loadLLMConfig, _FALLBACK_PROMPTS
from ..contextBuilder import buildConversationContext
from ..promptSafety import neutralizePromptDelimiters
from ._guardrails import SYSTEM_GUARDRAILS, MEMORY_ACTION_INSTRUCTIONS, VISION_DESCRIBE_PROMPT, OPS_FEEDBACK_INSTRUCTIONS
from ._request import requestWithRetry
from ._router import getProvider


_VISION_MAX_TOKENS = 4096
_VISION_TEMPERATURE = 0.2
_VISION_MIN_DESCRIPTION_LEN = 150  # 低于此长度视为描述异常（正常描述 200-800+ 字符）




def _buildSystemMessages(prompts: dict, *, includeContext: bool = False) -> list[str]:
    """
    将 prompts.json 的 system_prompt + guardrails 合并为 list[str]
    如果 includeContext=True，再拼上 MEMORY_ACTION_INSTRUCTIONS 和 OPS_FEEDBACK_INSTRUCTIONS
    """
    source = _FALLBACK_PROMPTS if getForceFallbackPrompt() else prompts
    raw = source.get("system_prompt", "")
    if isinstance(raw, list):
        parts = [s for s in raw if isinstance(s, str) and s.strip()]
    elif isinstance(raw, str) and raw.strip():
        parts = [raw]
    else:
        parts = [str(raw)]

    parts.extend(SYSTEM_GUARDRAILS)
    if includeContext:
        parts.extend(MEMORY_ACTION_INSTRUCTIONS)
        parts.extend(OPS_FEEDBACK_INSTRUCTIONS)
    return parts




async def _describeImages(
    images: list[dict],
    *,
    model: str,
) -> str:
    """
    双调用架构 — 第一步：轻量视觉调用

    使用无人设的 VISION_DESCRIBE_PROMPT，让 LLM 客观描述图片内容
    不附带用户文本，避免模型根据文本编造图片内容
    返回的描述文本将作为纯文本注入主调用的上下文中

    描述长度低于阈值时自动重试一次（有些中转服务偶尔会丢图片数据）
    失败时返回空字符串，不阻断主调用

    参数:
        model: 视觉模型名。由调用方从请求级配置快照传入（generateReply 已读
               过一次 llmConfig），避免此处再 getVisionModel() 重复读盘。
    """
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
            {"type": "text", "text": "请详细描述以上图片中的所有内容"},
        ]

        description = await requestWithRetry(
            provider,
            systemMessages=VISION_DESCRIBE_PROMPT,
            userContent=userContent,
            model=model,
            maxTokens=_VISION_MAX_TOKENS,
            temperature=_VISION_TEMPERATURE,
        )

        # 正常图片描述通常 200-800+ 字符，过短大概率是服务异常（如中转丢图片数据）
        if len(description) < _VISION_MIN_DESCRIPTION_LEN:
            await logSystemEvent(
                "LLM 图片描述异常短",
                f"仅 {len(description)} 字符，重试 | {description[:80]}",
                LogLevel.WARNING,
                LogChildType.WITH_ONE_CHILD,
            )
            description = await requestWithRetry(
                provider,
                systemMessages=VISION_DESCRIBE_PROMPT,
                userContent=userContent,
                model=model,
                maxTokens=_VISION_MAX_TOKENS,
                temperature=_VISION_TEMPERATURE,
            )
            if len(description) < _VISION_MIN_DESCRIPTION_LEN:
                await logSystemEvent(
                    "LLM 图片描述重试仍异常",
                    f"仅 {len(description)} 字符，放弃",
                    LogLevel.WARNING,
                    LogChildType.WITH_ONE_CHILD,
                )
                return ""

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




_IMAGE_FAILED_BLOCK = (
    "<IMAGE_DESCRIPTION>\n"
    "[用户发了图片，但图片读取失败了请告诉用户图片没能看到，建议再发一次试试语气自然，不必提技术原因]\n"
    "</IMAGE_DESCRIPTION>"
)

async def generateReply(
    userMessage: str,
    chatID: str,
    includeContext: bool = False,
    *,
    userID: str | int | None = None,
    sessionID: str | int | None = None,
    images: list[dict] | None = None,
    urlContexts: list[dict] | None = None,
) -> str:
    """
    调用 LLM 生成回复

    参数:
        images: 图片列表 [{"data": b64_str, "mimeType": "image/jpeg"}, ...]
                为 None 或空列表时表示纯文本
        urlContexts: URL 抓取结果列表

    图片处理策略（由 visionModel 配置决定）：
        - visionModel == model → 单调用：图片直接传给主模型
        - visionModel != model → 双调用：轻量模型描述 + 主模型回复
    """
    prompts = loadPrompts()
    systemMessages = _buildSystemMessages(prompts, includeContext=includeContext)
    maxTokens = prompts.get("max_tokens", 1024)
    temperature = prompts.get("temperature", 0.8)

    # 使用请求级配置快照，在每次回复生成的全过程只读一次 llmConfig.json，
    # 把 dict 沿调用链往下传，避免每个 getter（getModel / getVisionModel / getKnowledge* 等）
    # 各自重复 open()、json.load() 读盘。
    #
    # 这里不使用模块级缓存，是考虑到 LLM 回复在 debounce + create_task 下多协程并发，
    # 运维若在生成途中改配置文件，模块级缓存的 mtime 检查可能读到半新半旧的
    # 混合状态。请求级快照保证"一条用户消息的完整处理过程，使用的配置一致"。
    #
    # 本快照覆盖的读盘调用点（原先每处独立 loadLLMConfig）：
    #   - 此处 model / visionModel（原 getModel + getVisionModel）
    #   - buildConversationContext → buildKnowledgeContext 的
    #     knowledgeEnabled / knowledgeMaxResults / knowledgeMinScore
    # 未覆盖（有意保持独立读取，见各自说明）：
    #   - _request.requestWithRetry 内的 getModel()：仅 fallback 重决策时用，
    #     此刻重读反而能拿到运维刚改的救场配置，故不传快照。
    #   - handlers/llm.py _dispatchGeneratedOutput 的 getAutoMode()：属回复
    #     已生成后的分发阶段，与生成逻辑解耦，单独读一次即可。
    cfg = loadLLMConfig()
    model = cfg["model"]
    visionModel = cfg["visionModel"]

    textContent = await buildConversationContext(
        userMessage=userMessage,
        chatID=chatID,
        userID=userID,
        sessionID=sessionID,
        includeContext=includeContext,
        urlContexts=urlContexts,
        llmConfig=cfg,
    )

    # ── 构建 userContent ──
    if images and visionModel != model:
        # 双调用：分离描述与回复（省主模型 token 并或能改善模型文本读取表现）
        imageDescription = await _describeImages(images, model=visionModel)
        if imageDescription:
            # 视觉模型输出不可信。中和结构分隔符，防止其内容伪造 <IMAGE_DESCRIPTION>
            # 等结构标记，越权影响主模型的上下文边界。
            # imageBlock 在 buildConversationContext 之后拼接，不经主路径中和，故就地处理。
            imageDescription = neutralizePromptDelimiters(imageDescription)
            imageBlock = (
                "<IMAGE_DESCRIPTION>\n"
                "[用户发送了图片以下是图片内容的文字描述，请基于此描述回答用户关于图片的提问]\n"
                f"{imageDescription}\n"
                "</IMAGE_DESCRIPTION>"
            )
        else:
            imageBlock = _IMAGE_FAILED_BLOCK
        userContent = f"{imageBlock}\n\n{textContent}"

    elif images:
        # 单调用：图片直接传给主模型
        userContent = [
            *[{"type": "image_base64", "data": img["data"], "mimeType": img["mimeType"]} for img in images],
            {"type": "text", "text": textContent},
        ]

    else:
        userContent = textContent

    provider = getProvider(model)
    return await requestWithRetry(
        provider,
        systemMessages=systemMessages,
        userContent=userContent,
        model=model,
        maxTokens=maxTokens,
        temperature=temperature,
    )
