"""
utils/afc/__init__.py

AFC (Automatic Function Calling) 模块

独立于 LLM 模块的工具调用系统，可被任何需要工具调用能力的模块复用。
"""

from utils.afc.afcIntent import hasAFCIntent, detectTools
from utils.afc.registry import getToolsSchema, getToolCallable, getAllToolNames
from utils.afc.contextBuilder import buildToolsContext
from utils.afc.executor import execute


__all__ = [
    "hasAFCIntent",
    "detectTools",
    "getToolsSchema",
    "getToolCallable",
    "getAllToolNames",
    "buildToolsContext",
    "execute",
]