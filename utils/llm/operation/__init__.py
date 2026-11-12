"""
utils/llm/operation/__init__.py

LLM 操作循环公共层。

模型的每次输出是一批操作；发言、记忆操作、工具调用都是操作。本包提供：
    - types：操作、结果、任务信息与能力契约
    - capabilities：外部能力列表（能力模块初始化时登记）
    - parser / prompt：操作块的整批解析，给模型的协议说明与操作记录
    - loop：顺序工作循环 runTask

LLM 自带的发言与记忆操作能力在 utils/llm/capability/。
"""

from .types import (
    Capability,
    Operation,
    OperationRecord,
    OperationResult,
    OperationStatus,
    TaskInfo,
    TaskOutcome,
    TaskStatus,
)
from .capabilities import registerCapability, listCapabilities
from .parser import BatchFormatError, ParsedBatch, parseBatch
from .loop import runTask


__all__ = [
    "Capability",
    "Operation",
    "OperationRecord",
    "OperationResult",
    "OperationStatus",
    "TaskInfo",
    "TaskOutcome",
    "TaskStatus",
    "registerCapability",
    "listCapabilities",
    "BatchFormatError",
    "ParsedBatch",
    "parseBatch",
    "runTask",
]
