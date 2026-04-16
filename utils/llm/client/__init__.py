"""
utils/llm/client/

多模型 LLM 客户端：
    - _base: 提供商抽象基类（支持纯文本与多模态 userContent）
    - _router: 模型名前缀路由至 Anthropic / Gemini / OpenAI / DeepSeek / 豆包等 provider
    - _guardrails: 安全护栏、视觉描述 prompt、记忆操作指令
    - _request: 请求发送与自动重试
    - _generate: 回复生成编排（双调用视觉架构 + system prompt 构建）
    - anthropic / gemini / openaiCompat: 各 provider 实现
"""

from ._request import requestReply
from ._generate import generateReply


__all__ = ["generateReply", "requestReply"]