"""
utils/afc/capability.py

把 AFC 工具箱登记为 LLM 操作循环的一种能力。

AFC 只登记一个能力 tool；具体调用哪个工具写在操作的 name 字段里，工具名因此
自成命名空间，不会和其他能力重名。说明、校验、处理分别转给 contextBuilder 与
executor。
"""

from utils.llm.operation.types import Capability, Operation, OperationResult, TaskInfo
from utils.llm.operation.capabilities import registerCapability

from utils.afc.contextBuilder import buildToolsDescription
from utils.afc.executor import runTool, validateCall
from utils.afc.registry import getEnabledToolsSchema




TOOL_CAPABILITY_NAME = "tool"

_ALLOWED_KEYS = {"name", "parameters"}




def _describe(task: TaskInfo) -> str:
    return buildToolsDescription(getEnabledToolsSchema())


async def _validate(args: dict, task: TaskInfo) -> str | None:
    unknownKeys = set(args) - _ALLOWED_KEYS
    if unknownKeys:
        return f"tool 操作只接受 name 和 parameters 两个字段，多出了：{'、'.join(sorted(unknownKeys))}"
    return validateCall(args.get("name"), args.get("parameters"))


async def _handle(operation: Operation, task: TaskInfo) -> OperationResult:
    return await runTool(operation.args["name"], operation.args.get("parameters"))




def registerToolCapability() -> None:
    """登记 tool 能力（模块 initFunction；LLM 不 import AFC，由 AFC 把自己加进去）。"""
    registerCapability(Capability(
        name=TOOL_CAPABILITY_NAME,
        describe=_describe,
        validate=_validate,
        handle=_handle,
    ))
