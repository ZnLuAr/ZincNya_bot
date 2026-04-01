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
        userContent: str,
        model: str,
        maxTokens: int,
        temperature: float,
    ) -> str:
        """发送请求并返回文本回复。"""
        ...

    def isAvailable(self) -> bool:
        """该提供商是否可用（API key 已配置）。"""
        return bool(self._apiKey)
