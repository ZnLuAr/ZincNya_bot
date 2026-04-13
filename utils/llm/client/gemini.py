"""
utils/llm/client/gemini.py

Google Gemini 提供商实现。
"""

import base64

from google import genai
from google.genai import types

from ._base import LLMProvider




class GeminiProvider(LLMProvider):
    """Google Gemini 提供商。"""

    def __init__(self, apiKey: str | None, *, proxy: str | None = None):
        super().__init__(apiKey)
        self._proxy = proxy
        self._client: genai.Client | None = None


    def _getClient(self) -> genai.Client:
        if self._client is None:
            kwargs = {"api_key": self._apiKey}
            if self._proxy:
                import httpx
                kwargs["http_options"] = {"client": httpx.AsyncClient(proxy=self._proxy)}
            self._client = genai.Client(**kwargs)
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

        # 多模态：将通用中间格式翻译为 Gemini Part 列表
        if isinstance(userContent, list):
            contents = []
            for block in userContent:
                if block["type"] == "text":
                    contents.append(block["text"])
                elif block["type"] == "image_base64":
                    contents.append(types.Part.from_bytes(
                        data=base64.b64decode(block["data"]),
                        mime_type=block["mimeType"],
                    ))
        else:
            contents = userContent

        response = await client.aio.models.generate_content(
            model=model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=systemText,
                max_output_tokens=maxTokens,
                temperature=temperature,
            ),
        )

        return response.text or ""
