"""
utils/llm/client/anthropic.py

Anthropic Claude 提供商实现。

注：temperature 在 1.x 已从 messages.create 签名移除
（被 output_config.effort 体系取代），因此统一经 extra_body 透传——
两个版本都支持该参数且语义相同（额外 JSON 进请求体）。代理分支按 SDK
主版本选 httpx（0.x）或 httpx2（1.x 起换了 HTTP 栈，传入 httpx 对象
会被 TypeError 拒绝）。

……你说 httpx.AsyncClient 用完不关会漏？我们的 AsyncAnthropic 会帮我们管理好生命周期的😋
"""

import re

import anthropic as _anthropicModule
from anthropic import AsyncAnthropic

from config import LLM_MAX_TOKENS_HARD_CAP

from utils.core.logger import logSystemEvent, LogLevel

from ._base import LLMProvider


# 截断提额重试轮数（首次 + 重试）
_TRUNCATION_RETRY_ROUNDS = 2


def _sdkMajorVersion() -> int:
    """读 SDK 主版本号；解析失败按 0 处理（旧版行为）。"""
    try:
        return int(str(_anthropicModule.__version__).split(".")[0])
    except (AttributeError, ValueError):
        return 0


def _buildProxyHttpClient(proxy: str):
    """按 SDK 版本构造带代理的异步 HTTP 客户端。

    SDK ≥1.0 依赖 httpx2 并拒绝 httpx 对象；0.x 只认 httpx。
    两个构造器的 proxy 参数语义一致。

    唉 A÷
    """
    if _sdkMajorVersion() >= 1:
        import httpx2
        return httpx2.AsyncClient(proxy=proxy)
    import httpx
    return httpx.AsyncClient(proxy=proxy)




class AnthropicProvider(LLMProvider):
    """Anthropic Claude 提供商。"""

    def __init__(self, apiKey: str | None, *, proxy: str | None = None, authToken: str | None = None, baseUrl: str | None = None):
        super().__init__(apiKey)
        self._proxy = proxy
        self._authToken = authToken
        self._baseUrl = baseUrl
        self._client: AsyncAnthropic | None = None


    def _getClient(self) -> AsyncAnthropic:
        if self._client is None:
            kwargs = {}
            # auth_token（Bearer）与 api_key（x-api-key）二选一；auth_token 优先（自定义端点常用）
            if self._authToken:
                kwargs["auth_token"] = self._authToken
            else:
                kwargs["api_key"] = self._apiKey
            if self._baseUrl:
                kwargs["base_url"] = self._baseUrl
            if self._proxy:
                kwargs["http_client"] = _buildProxyHttpClient(self._proxy)
            self._client = AsyncAnthropic(**kwargs)
        return self._client


    def isAvailable(self) -> bool:
        """auth_token（自定义端点）或 api_key 任一存在即视为已配置。"""
        return bool(self._apiKey) or bool(self._authToken)


    async def requestReply(
        self,
        *,
        systemMessages: list[str],
        userContent: str | list,
        model: str,
        maxTokens: int,
        temperature: float,
    ) -> str:
        client = self._getClient()
        systemText = "\n\n".join(systemMessages)

        # 多模态：将通用中间格式翻译为 Anthropic content array
        if isinstance(userContent, list):
            content = []
            for block in userContent:
                if block["type"] == "text":
                    content.append({"type": "text", "text": block["text"]})
                elif block["type"] == "image_base64":
                    content.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": block["mimeType"],
                            "data": block["data"],
                        },
                    })
        else:
            content = userContent

        # 截断提额重试：思考模型（思考 token 计入 max_tokens 预算）思考过长时，
        # 响应会被截断且只有 thinking block、无 text block。再使用相同的参数重试会同样失败，
        # 故提升 max_tokens 后原样重试一次；仍空则抛错（上层 requestWithRetry 判定
        # RuntimeError 不可重试，直接冒泡给既有错误提示路径）。
        effectiveMaxTokens = maxTokens
        response = None
        for attempt in range(_TRUNCATION_RETRY_ROUNDS):
            response = await client.messages.create(
                model=model,
                max_tokens=effectiveMaxTokens,
                system=systemText,
                messages=[
                    {"role": "user", "content": content}
                ],
                # temperature 在 SDK 1.x 已从签名移除，新版和旧版统一进入 extra_body 透传
                extra_body={"temperature": temperature},
            )

            textBlock = next((b for b in response.content if b.type == "text"), None)
            if textBlock and textBlock.text.strip():
                break

            hasThinkingOnly = any(b.type == "thinking" for b in response.content)
            await logSystemEvent(
                "LLM 响应无文本块，提额重试",
                f"model={model}, attempt={attempt + 1}, max_tokens={effectiveMaxTokens} → {min(effectiveMaxTokens * 2, LLM_MAX_TOKENS_HARD_CAP)}, "
                f"stop_reason={response.stop_reason}, thinkingOnly={hasThinkingOnly}",
                LogLevel.WARNING,
            )
            effectiveMaxTokens = min(effectiveMaxTokens * 2, LLM_MAX_TOKENS_HARD_CAP)

        textBlock = next((b for b in response.content if b.type == "text"), None)
        if not textBlock or not textBlock.text.strip():
            raise RuntimeError(
                f"LLM 响应被截断且提额后仍无文本（model={model}, max_tokens 提额至 {effectiveMaxTokens}, "
                f"stop_reason={response.stop_reason}）——思考模型可能需要更高的 max_tokens 配置"
            )

        text = textBlock.text

        # 仅在 thinking block 存在时才做 <thinking> 标签清理，
        # 避免误 strip 将全部内容视为 thinking 的边界情况
        hasThinkingBlock = any(b.type == "thinking" for b in response.content)
        if hasThinkingBlock:
            text = re.sub(r"<thinking>.*?</thinking>\s*", "", text, flags=re.DOTALL).strip()

        return text
