"""
utils/llm/client/gemini.py

Google Gemini 提供商实现。
"""

from google import genai
from google.genai import types

from ._base import LLMProvider




class GeminiProvider(LLMProvider):
    """Google Gemini 提供商。"""

    def __init__(self, apiKey: str | None):
        super().__init__(apiKey)
        self._client: genai.Client | None = None


    def _getClient(self) -> genai.Client:
        if self._client is None:
            self._client = genai.Client(api_key=self._apiKey)
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

        response = await client.aio.models.generate_content(
            model=model,
            contents=userContent,
            config=types.GenerateContentConfig(
                system_instruction=systemText,
                max_output_tokens=maxTokens,
                temperature=temperature,
            ),
        )

        return response.text or ""
