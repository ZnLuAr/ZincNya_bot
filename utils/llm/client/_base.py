"""
utils/llm/client/_base.py

LLM 提供商抽象基类。
"""

from abc import ABC, abstractmethod




class LLMProvider(ABC):
    """LLM 提供商抽象基类。子类需实现 requestReply 和 isAvailable。"""

    def __init__(self, apiKey: str | None):
        self._apiKey = apiKey

    @abstractmethod
    async def requestReply(
        self,
        *,
        systemMessages: list[str],
        userContent: str | list,
        model: str,
        maxTokens: int,
        temperature: float,
    ) -> str:
        """
        发送请求并返回文本回复。

        userContent 为 str 时表示纯文本；为 list 时表示多模态内容，
        包含 {"type": "text", "text": "..."} 和
        {"type": "image_base64", "data": "<b64>", "mimeType": "image/jpeg"} 块。
        """
        ...

    def isAvailable(self) -> bool:
        """该提供商是否可用（API key 已配置）。"""
        return bool(self._apiKey)
