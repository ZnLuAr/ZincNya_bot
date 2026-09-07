"""
utils/llm/client/openaiCompat.py

OpenAI 兼容提供商实现。
支持 OpenAI、DeepSeek 等使用 OpenAI API 格式的端点。
"""

from openai import AsyncOpenAI

from config import LLM_MAX_TOKENS_HARD_CAP

from utils.core.logger import logSystemEvent, LogLevel

from ._base import LLMProvider


# 截断提额重试轮数（首次 + 重试）
_TRUNCATION_RETRY_ROUNDS = 2




class OpenAICompatProvider(LLMProvider):
    """OpenAI 兼容提供商（OpenAI、DeepSeek 等）。"""

    def __init__(self, apiKey: str | None, baseURL: str | None = None, *, proxy: str | None = None):
        super().__init__(apiKey)
        self._baseURL = baseURL
        self._proxy = proxy
        self._client: AsyncOpenAI | None = None


    def _getClient(self) -> AsyncOpenAI:
        if self._client is None:
            kwargs = {
                "api_key": self._apiKey,
                "base_url": self._baseURL,
            }
            if self._proxy:
                import httpx
                kwargs["http_client"] = httpx.AsyncClient(proxy=self._proxy)
            self._client = AsyncOpenAI(**kwargs)
        return self._client


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

        # 多模态：将通用中间格式翻译为 OpenAI vision content array
        if isinstance(userContent, list):
            content = []
            for block in userContent:
                if block["type"] == "text":
                    content.append({"type": "text", "text": block["text"]})
                elif block["type"] == "image_base64":
                    content.append({
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{block['mimeType']};base64,{block['data']}"
                        },
                    })
        else:
            content = userContent

        # 截断提额重试：推理模型（reasoning token 计入 max_tokens 预算）思考过长时，
        # 会有 finish_reason=length 且 content 为空。此时需要提升 max_tokens
        # 后原样重试一次。仅 length+空 触发——stop+空可能是模型有意空回，交上层空检测处理。
        # 仍截断则抛错（requestWithRetry 判定 RuntimeError 不可重试，直接冒泡给错误提示路径）。
        effectiveMaxTokens = maxTokens
        choice = None
        for attempt in range(_TRUNCATION_RETRY_ROUNDS):
            response = await client.chat.completions.create(
                model=model,
                max_tokens=effectiveMaxTokens,
                temperature=temperature,
                messages=[
                    {"role": "system", "content": systemText},
                    {"role": "user", "content": content},
                ],
            )

            choice = response.choices[0] if response.choices else None
            if choice and choice.message and (choice.message.content or "").strip():
                break
            if not choice or choice.finish_reason != "length":
                break  # 非截断形态（无 choice / stop 但空），跳出交由末尾统一处理

            await logSystemEvent(
                "LLM 响应被截断（finish_reason=length），提额重试",
                f"model={model}, attempt={attempt + 1}, max_tokens={effectiveMaxTokens} → {min(effectiveMaxTokens * 2, LLM_MAX_TOKENS_HARD_CAP)}",
                LogLevel.WARNING,
            )
            effectiveMaxTokens = min(effectiveMaxTokens * 2, LLM_MAX_TOKENS_HARD_CAP)

        if not choice or not choice.message:
            return ""

        contentText = choice.message.content or ""
        if not contentText.strip() and choice.finish_reason == "length":
            raise RuntimeError(
                f"LLM 响应被截断且提额后仍无文本（model={model}, max_tokens 提额至 {effectiveMaxTokens}, "
                f"finish_reason=length）——reasoning 模型可能需要更高的 max_tokens 配置"
            )

        return contentText
