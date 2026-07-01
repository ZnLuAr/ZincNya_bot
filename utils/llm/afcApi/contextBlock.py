"""
utils/llm/afcApi/contextBlock.py

AFC 上游桥接：构建工具上下文块

把工具名集合渲染成 LLM 可理解的文本块，供 handlers/afc.py (group=1)
推入 bot_data 推送层。LLM 模块经由此处感知 AFC，afc 模块本身不依赖 LLM。
"""

from utils.afc.registry import getToolsSchema
from utils.afc.contextBuilder import buildToolsContext


def buildAFCContextBlock(toolNames: set[str]) -> str:
    """
    根据工具名集合，生成 LLM 上下文块。

    流程：
        1. 通过 registry.getToolsSchema(toolNames) 获取 OpenAI format 的 schema
        2. 通过 afc.contextBuilder.buildToolsContext() 渲染成文本

    参数:
        toolNames: 工具名集合（由 afcIntent.detectTools 返回）

    返回:
        工具上下文文本（含 <AFC_TOOLS> 标签和使用说明）；
        toolNames 为空或无匹配 schema 时返回空字符串。
    """
    if not toolNames:
        return ""

    schema = getToolsSchema(toolNames)
    if not schema:
        return ""

    return buildToolsContext(schema)