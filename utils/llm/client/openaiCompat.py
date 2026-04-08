"""
utils/llm/client/openaiCompat.py

OpenAI 兼容提供商实现。
支持 OpenAI、DeepSeek 等使用 OpenAI API 格式的端点。
"""

from openai import AsyncOpenAI

from ._base import LLMProvider




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
        userContent: str,
        model: str,
        maxTokens: int,
        temperature: float,
    ) -> str:
        client = self._getClient()
        systemText = "\n\n".join(systemMessages)

        response = await client.chat.completions.create(
            model=model,
            max_tokens=maxTokens,
            temperature=temperature,
            messages=[
                {"role": "system", "content": systemText},
                {"role": "user", "content": userContent},
            ],
        )

        choice = response.choices[0] if response.choices else None
        if not choice or not choice.message:
            return ""

        return choice.message.content or ""
