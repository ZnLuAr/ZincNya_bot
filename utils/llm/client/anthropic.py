"""
utils/llm/client/anthropic.py

Anthropic Claude 提供商实现。

……你说 httpx.AsyncClient 用完不关会漏？我们的 AsyncAnthropic 会帮我们管理好生命周期的😋
"""

import re

from anthropic import AsyncAnthropic

from ._base import LLMProvider




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
                import httpx
                kwargs["http_client"] = httpx.AsyncClient(proxy=self._proxy)
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

        text = textBlock.text

        # 仅在 thinking block 存在时才做 <thinking> 标签清理，
        # 避免误 strip 将全部内容视为 thinking 的边界情况
        hasThinkingBlock = any(b.type == "thinking" for b in response.content)
        if hasThinkingBlock:
            text = re.sub(r"<thinking>.*?</thinking>\s*", "", text, flags=re.DOTALL).strip()

        return text
