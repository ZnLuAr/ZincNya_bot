"""
utils/llm/afcApi/__init__.py

AFC API：LLM 与 AFC 模块的唯一桥接层

LLM 模块经由此处感知 AFC，AFC 模块本身不依赖 LLM（单点耦合）。

接口职责：
- buildAFCContextBlock：上游桥接，构建工具上下文块，供 handlers/afc.py 推入 bot_data
- handleAFCInLLMResponse：下游桥接，解析 LLM 回复中的 <AFC_ACTION>，执行工具，
  携带结果二次调用 LLM，直到无工具调用或达到迭代上限
"""

from utils.llm.afcApi.contextBlock import buildAFCContextBlock
from utils.llm.afcApi.responseHandler import handleAFCInLLMResponse


__all__ = [
    "buildAFCContextBlock",
    "handleAFCInLLMResponse",
]