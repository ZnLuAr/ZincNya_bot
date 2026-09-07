"""
utils/llm/client/gemini.py

Google Gemini 提供商实现。
"""

import base64

from google import genai
from google.genai import types

from config import LLM_MAX_TOKENS_HARD_CAP

from utils.core.logger import logSystemEvent, LogLevel

from ._base import LLMProvider


# 截断提额重试轮数（首次 + 重试）
_TRUNCATION_RETRY_ROUNDS = 2




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

        # 截断提额重试：思考模型（思考计入 max_output_tokens 预算）思考过长时，
        # 会有 finish_reason=MAX_TOKENS 且生成的文本为 None。此时需要提升
        # max_output_tokens 后原样重试一次；仍空则抛错（requestWithRetry 判定
        # RuntimeError 不可重试，直接冒泡给既有错误提示路径）。
        effectiveMaxTokens = maxTokens
        response = None
        for attempt in range(_TRUNCATION_RETRY_ROUNDS):
            response = await client.aio.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=systemText,
                    max_output_tokens=effectiveMaxTokens,
                    temperature=temperature,
                ),
            )

            if (response.text or "").strip():
                break

            finishReason = ""
            if response.candidates:
                finishReason = str(response.candidates[0].finish_reason or "")
            await logSystemEvent(
                "LLM 响应无文本，提额重试",
                f"model={model}, attempt={attempt + 1}, max_output_tokens={effectiveMaxTokens} → {min(effectiveMaxTokens * 2, LLM_MAX_TOKENS_HARD_CAP)}, "
                f"finish_reason={finishReason}",
                LogLevel.WARNING,
            )
            effectiveMaxTokens = min(effectiveMaxTokens * 2, LLM_MAX_TOKENS_HARD_CAP)

        responseText = response.text or ""
        if not responseText.strip():
            raise RuntimeError(
                f"LLM 响应被截断且提额后仍无文本（model={model}, max_output_tokens 提额至 {effectiveMaxTokens}）"
                "——thinking 模型可能需要更高的 max_tokens 配置"
            )

        return responseText
