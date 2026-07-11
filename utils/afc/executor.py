"""
utils/afc/executor.py

AFC 工具执行器

解析 LLM 输出的 <AFC_ACTION> JSON，校验后调用对应工具函数。
执行结果包裹为 <FUNCTION_RESULT> / <FUNCTION_ERROR>，回灌给 LLM 二次合成。
"""

import json
import asyncio
import inspect

from utils.core.logger import logSystemEvent

from utils.afc.registry import getToolsSchema, getToolCallable, getAllToolNames


# 单次工具执行的超时（秒）；防止某个 skill 卡死整条回复链
_TOOL_TIMEOUT_SECONDS = 15


def _resolveFuncName(toolName: str, action: dict) -> str:
    """
    解析本次调用要执行的函数名。

    优先级：
        1. action 里显式给出的 "function" 字段
        2. 工具只有单个函数时，自动取那个唯一函数
        3. 否则返回空串（交由上层报错）
    """
    explicit = action.get("function")
    if explicit:
        return str(explicit)

    schemas = getToolsSchema({toolName})
    if len(schemas) == 1:
        return schemas[0]["name"]

    return ""


def _coerceParameters(action: dict) -> dict:
    """从 action 中取出参数字典；容忍 parameters / arguments 两种键名。"""
    params = action.get("parameters")
    if params is None:
        params = action.get("arguments")
    if not isinstance(params, dict):
        return {}
    return params


async def execute(actionJSON: str) -> str:
    """
    执行单次工具调用。

    参数:
        actionJSON: <AFC_ACTION> 中提取出的 JSON 字符串

    返回:
        "<FUNCTION_RESULT>结果</FUNCTION_RESULT>"
        或 "<FUNCTION_ERROR>错误信息</FUNCTION_ERROR>"
    """
    # ── 1. 解析 JSON ──
    try:
        action = json.loads(actionJSON)
    except (json.JSONDecodeError, TypeError) as e:
        await logSystemEvent("AFC 执行失败", f"JSON 解析失败：{e}")
        return f"<FUNCTION_ERROR>无法解析工具调用 JSON：{e}</FUNCTION_ERROR>"

    if not isinstance(action, dict):
        return "<FUNCTION_ERROR>工具调用必须是 JSON 对象</FUNCTION_ERROR>"

    # ── 2. 校验 tool ──
    toolName = action.get("tool")
    if not toolName:
        return "<FUNCTION_ERROR>缺少 tool 字段</FUNCTION_ERROR>"

    if toolName not in getAllToolNames():
        return f"<FUNCTION_ERROR>未知工具：{toolName}</FUNCTION_ERROR>"

    # ── 3. 解析函数名 ──
    funcName = _resolveFuncName(toolName, action)
    if not funcName:
        return f"<FUNCTION_ERROR>工具 {toolName} 有多个函数，请在 function 字段指定</FUNCTION_ERROR>"

    func = getToolCallable(toolName, funcName)
    if func is None:
        return f"<FUNCTION_ERROR>工具 {toolName} 不存在函数 {funcName}</FUNCTION_ERROR>"

    # ── 4. 取参数 + 校验签名 ──
    params = _coerceParameters(action)
    try:
        sig = inspect.signature(func)
        sig.bind(**params)
    except TypeError as e:
        return f"<FUNCTION_ERROR>参数不匹配：{e}</FUNCTION_ERROR>"

    # ── 5. 执行（带超时）──
    try:
        if inspect.iscoroutinefunction(func):
            result = await asyncio.wait_for(func(**params), timeout=_TOOL_TIMEOUT_SECONDS)
        else:
            # 同步函数丢到线程池，避免阻塞事件循环
            result = await asyncio.wait_for(
                asyncio.to_thread(func, **params),
                timeout=_TOOL_TIMEOUT_SECONDS,
            )
    except asyncio.TimeoutError:
        await logSystemEvent("AFC 执行超时", f"{toolName}.{funcName}")
        return f"<FUNCTION_ERROR>工具 {toolName}.{funcName} 执行超时</FUNCTION_ERROR>"
    except Exception as e:
        await logSystemEvent("AFC 执行异常", f"{toolName}.{funcName}：{e}")
        return f"<FUNCTION_ERROR>工具执行出错</FUNCTION_ERROR>"

    await logSystemEvent("AFC 执行成功", f"{toolName}.{funcName}")
    return f"<FUNCTION_RESULT>{result}</FUNCTION_RESULT>"
