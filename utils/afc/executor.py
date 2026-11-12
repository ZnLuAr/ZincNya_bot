"""
utils/afc/executor.py

AFC 工具执行器

对一次工具调用做两件事：
    - validateCall：不运行工具，判断这次调用能不能执行
    - runTool：带保护地运行，交回明确的成功或失败

保护包括：执行前再查一次启用状态、超时、异常兜底、返回值类型与长度检查。
工具用抛 ToolError / ToolDependencyError 表示失败，消息交给模型；其他异常
只进日志。调用格式与结果怎么呈现给模型由 LLM 公共层决定，这里不涉及。
"""

import asyncio
import inspect

from config import AFC_TOOL_TIMEOUT_SECONDS, AFC_TOOL_RESULT_MAX_CHARS

from utils.core.logger import logSystemEvent, LogLevel
from utils.llm.operation.types import OperationResult, OperationStatus

from utils.afc.errors import ToolError, ToolDependencyError
from utils.afc.registry import getAllToolNames, getFunctionSchema, getToolCallable
from utils.afc.toolManager import isAfcToolEnabled




_NAME_SEPARATOR = "."

# JSON Schema 类型 → 判断函数；bool 是 int 的子类，整数与数字要单独排除它
_JSON_TYPE_CHECKS = {
    "string": lambda value: isinstance(value, str),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
    "boolean": lambda value: isinstance(value, bool),
    "array": lambda value: isinstance(value, list),
    "object": lambda value: isinstance(value, dict),
}

_TRUNCATED_NOTE = "\n……（工具返回内容过长，已截断）"




def splitToolName(name) -> tuple[str, str] | None:
    """把「工具名.函数名」拆成两段；格式不对时返回 None。"""
    if not isinstance(name, str):
        return None
    toolName, separator, funcName = name.strip().partition(_NAME_SEPARATOR)
    if not separator or not toolName or not funcName:
        return None
    return toolName, funcName


def validateCall(name, parameters) -> str | None:
    """
    判断一次工具调用能不能执行，不运行工具。

    参数:
        name: 「工具名.函数名」
        parameters: 参数字典；None 视为没有参数

    返回:
        None 表示可以执行，否则是给模型看的原因
    """
    parts = splitToolName(name)
    if parts is None:
        return "name 必须写成「工具名.函数名」"
    toolName, funcName = parts

    if toolName not in getAllToolNames():
        return f"未知工具：{toolName}"
    if not isAfcToolEnabled(toolName):
        return f"工具 {toolName} 当前未启用"

    func = getToolCallable(toolName, funcName)
    if func is None:
        return f"工具 {toolName} 没有函数 {funcName}"

    if parameters is None:
        parameters = {}
    if not isinstance(parameters, dict):
        return "parameters 必须是 JSON 对象"

    try:
        inspect.signature(func).bind(**parameters)
    except TypeError as e:
        return f"参数不匹配：{e}"

    schema = getFunctionSchema(toolName, funcName) or {}
    properties = (schema.get("parameters") or {}).get("properties") or {}
    for paramName, value in parameters.items():
        jsonType = (properties.get(paramName) or {}).get("type")
        check = _JSON_TYPE_CHECKS.get(jsonType)
        if check is not None and not check(value):
            return f"参数 {paramName} 的类型应为 {jsonType}"

    return None




async def runTool(name: str, parameters: dict | None) -> OperationResult:
    """
    运行一次已通过 validateCall 的工具调用，交回实际结果。

    工具中途被取消时照常抛出 CancelledError（记日志）；其余情况都返回结果：
    成功记已完成，超时、工具报告的失败、意外异常、返回值不合要求都记失败。
    """
    toolName, funcName = splitToolName(name)
    label = f"{toolName}.{funcName}"

    # 校验与执行之间工具可能被停用，执行前再查一次
    if not isAfcToolEnabled(toolName):
        return _failed(f"工具 {toolName} 当前未启用")

    func = getToolCallable(toolName, funcName)
    if func is None:
        return _failed(f"工具 {toolName} 没有函数 {funcName}")

    params = parameters or {}
    try:
        if inspect.iscoroutinefunction(func):
            result = await asyncio.wait_for(func(**params), timeout=AFC_TOOL_TIMEOUT_SECONDS)
        else:
            # 同步函数丢到线程池，避免阻塞事件循环；超时只能放弃等待，线程本身停不下来
            result = await asyncio.wait_for(
                asyncio.to_thread(func, **params),
                timeout=AFC_TOOL_TIMEOUT_SECONDS,
            )
    except asyncio.TimeoutError:
        await logSystemEvent("AFC 执行超时", label, LogLevel.WARNING)
        return _failed(f"工具 {label} 执行超时（{AFC_TOOL_TIMEOUT_SECONDS} 秒）")
    except asyncio.CancelledError:
        await logSystemEvent("AFC 执行被取消", label, LogLevel.WARNING)
        raise
    except (ToolError, ToolDependencyError) as e:
        await logSystemEvent("AFC 工具报告失败", f"{label}：{e}", LogLevel.INFO)
        return _failed(str(e) or "工具没有给出失败原因")
    except Exception as e:
        await logSystemEvent("AFC 执行异常", label, LogLevel.ERROR, exception=e)
        return _failed(f"工具 {label} 内部出错（{type(e).__name__}）")

    if not isinstance(result, str):
        await logSystemEvent(
            "AFC 返回值类型不符", f"{label} 返回 {type(result).__name__}，应为 str", LogLevel.ERROR,
        )
        return _failed(f"工具 {label} 的返回值不是文字")

    if len(result) > AFC_TOOL_RESULT_MAX_CHARS:
        result = result[:AFC_TOOL_RESULT_MAX_CHARS] + _TRUNCATED_NOTE

    await logSystemEvent("AFC 执行成功", label)
    return OperationResult(status=OperationStatus.DONE, modelText=result)




def _failed(reason: str) -> OperationResult:
    return OperationResult(status=OperationStatus.FAILED, modelText=reason)
