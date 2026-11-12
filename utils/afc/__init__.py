"""
utils/afc/__init__.py

AFC 工具箱

管理工具的安装与启停，交出工具说明，校验并带保护地运行一次工具调用。
多轮循环、调用格式和结果呈现归 LLM 操作循环（utils/llm/operation）；
AFC 在初始化时通过 capability.registerToolCapability 把自己登记为其中一种能力。
"""

from utils.afc.registry import getAllToolNames, getEnabledToolsSchema, getToolCallable
from utils.afc.contextBuilder import buildToolsDescription
from utils.afc.executor import runTool, validateCall
from utils.afc.capability import registerToolCapability


__all__ = [
    "getAllToolNames",
    "getEnabledToolsSchema",
    "getToolCallable",
    "buildToolsDescription",
    "runTool",
    "validateCall",
    "registerToolCapability",
]
