"""
utils/llm/client/anthropic.py

Anthropic Claude 提供商实现。
"""

import re
from anthropic import AsyncAnthropic

from ._base import LLMProvider




class AnthropicProvider(LLMProvider):
    """Anthropic Claude 提供商。"""

    def __init__(self, apiKey: str | None):
        super().__init__(apiKey)
        self._client: AsyncAnthropic | None = None


    def _getClient(self) -> AsyncAnthropic:
        if self._client is None:
            self._client = AsyncAnthropic(api_key=self._apiKey)
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

        response = await client.messages.create(
            model=model,
            max_tokens=maxTokens,
            temperature=temperature,
            system=systemText,
            messages=[
                {"role": "user", "content": userContent}
            ],
        )

        textBlock = next((b for b in response.content if b.type == "text"), None)
        if not textBlock:
            return ""

        # Claude 可能在回复中包含 <thinking> 标签，需要过滤
        return re.sub(r"<thinking>.*?</thinking>\s*", "", textBlock.text, flags=re.DOTALL).strip()
