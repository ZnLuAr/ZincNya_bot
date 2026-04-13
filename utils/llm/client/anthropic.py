"""
utils/llm/client/anthropic.py

Anthropic Claude 提供商实现。
"""

import re
from anthropic import AsyncAnthropic

from ._base import LLMProvider




class AnthropicProvider(LLMProvider):
    """Anthropic Claude 提供商。"""

    def __init__(self, apiKey: str | None, *, proxy: str | None = None):
        super().__init__(apiKey)
        self._proxy = proxy
        self._client: AsyncAnthropic | None = None


    def _getClient(self) -> AsyncAnthropic:
        if self._client is None:
            kwargs = {"api_key": self._apiKey}
            if self._proxy:
                import httpx
                kwargs["http_client"] = httpx.AsyncClient(proxy=self._proxy)
            self._client = AsyncAnthropic(**kwargs)
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

        response = await client.messages.create(
            model=model,
            max_tokens=maxTokens,
            temperature=temperature,
            system=systemText,
            messages=[
                {"role": "user", "content": content}
            ],
        )

        textBlock = next((b for b in response.content if b.type == "text"), None)
        if not textBlock:
            return ""

        # Claude 可能在回复中包含 <thinking> 标签，需要过滤
        return re.sub(r"<thinking>.*?</thinking>\s*", "", textBlock.text, flags=re.DOTALL).strip()
