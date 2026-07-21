"""
utils/afc/registry.py

AFC 工具注册表

扫描 tools/*/__init__.py，从类型注解 + docstring 生成 OpenAI function schema。
"""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path
from typing import get_type_hints, get_origin, get_args


# ── 工具 schema 缓存 ──────────────────────────────

_toolsSchema: dict[str, dict] = {}  # {tool_name: schema}
_toolsCallable: dict[str, dict[str, callable]] = {}  # {tool_name: {func_name: callable}}
_schemaBuilt = False




# ── Python 类型 → JSON Schema 映射 ────────────────

def _pythonTypeToJsonType(pyType) -> str:
    """将 Python 类型转换为 JSON Schema 类型"""
    origin = get_origin(pyType)

    # 处理 Optional[T] / Union[T, None]
    if origin is type(None):
        return "null"

    # 处理泛型类型（如 list[str], dict[str, int]）
    if origin is list:
        return "array"
    if origin is dict:
        return "object"

    # 处理基础类型
    if pyType is str or pyType is type(str):
        return "string"
    if pyType is int or pyType is type(int):
        return "integer"
    if pyType is float or pyType is type(float):
        return "number"
    if pyType is bool or pyType is type(bool):
        return "boolean"

    # 默认返回 string
    return "string"




def _extractDocstring(func) -> tuple[str, dict[str, str]]:
    """
    从函数 docstring 提取描述与参数说明

    返回: (函数描述, {参数名: 参数描述})
    """
    doc = inspect.getdoc(func) or ""
    lines = [line.strip() for line in doc.split("\n")]

    # 函数描述（第一行或第一个非空行）
    description = ""
    paramDescriptions = {}

    inParamsSection = False
    for line in lines:
        if not line:
            continue

        # 检测参数段落开始
        if line.lower().startswith("参数") or line.lower().startswith("parameters"):
            inParamsSection = True
            continue
        if line.lower().startswith("返回") or line.lower().startswith("return"):
            inParamsSection = False
            continue

        # 提取参数描述（格式：`param: 描述` 或 `param - 描述`）
        if inParamsSection:
            if ":" in line:
                paramName, paramDesc = line.split(":", 1)
                paramDescriptions[paramName.strip()] = paramDesc.strip()
            elif "-" in line:
                paramName, paramDesc = line.split("-", 1)
                paramDescriptions[paramName.strip()] = paramDesc.strip()
        elif not description:
            description = line

    return description, paramDescriptions




def _generateFunctionSchema(func, toolName: str) -> dict:
    """
    从函数生成 OpenAI function schema

    参数:
        func: 函数对象
        toolName: 工具名（用于错误日志）

    返回:
        OpenAI function schema dict
    """
    funcName = func.__name__
    description, paramDescriptions = _extractDocstring(func)

    # 获取类型注解
    try:
        typeHints = get_type_hints(func)
    except Exception:
        typeHints = {}

    # 获取函数签名
    sig = inspect.signature(func)
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    for paramName, param in sig.parameters.items():
        # 跳过 self / cls
        if paramName in ("self", "cls"):
            continue

        # 获取参数类型
        paramType = typeHints.get(paramName, str)
        jsonType = _pythonTypeToJsonType(paramType)

        # 获取参数描述
        paramDesc = paramDescriptions.get(paramName, "")

        # 构建参数 schema
        parameters["properties"][paramName] = {
            "type": jsonType,
            "description": paramDesc,
        }

        # 判断是否必填（无默认值 = required）
        if param.default is inspect.Parameter.empty:
            parameters["required"].append(paramName)

    return {
        "name": funcName,
        "description": description or f"{toolName}.{funcName}",
        "parameters": parameters,
    }




def _scanTools():
    """扫描 tools/*/__init__.py，生成工具 schema"""
    global _schemaBuilt
    if _schemaBuilt:
        return

    toolsPath = Path(__file__).parent / "tools"
    if not toolsPath.exists():
        _schemaBuilt = True
        return

    for toolDir in toolsPath.iterdir():
        if not toolDir.is_dir() or toolDir.name.startswith(("_", ".")):
            continue

        toolName = toolDir.name

        # 跳过被禁用的工具（不注册 schema / callable）
        from utils.afc.toolManager import isAfcToolEnabled
        if not isAfcToolEnabled(toolName):
            continue

        toolPackage = f"utils.afc.tools.{toolName}"

        # 导入工具模块
        try:
            toolModule = importlib.import_module(toolPackage)
        except ImportError:
            continue

        # 扫描模块中的公开函数（不以 _ 开头）
        toolCallables = {}
        toolSchemas = []

        for name, obj in inspect.getmembers(toolModule, inspect.isfunction):
            if name.startswith("_"):
                continue

            # 只登记本工具包内定义的函数，排除从外部导入的函数
            # （如 logSystemEvent 等工具辅助导入），避免被误当作工具函数
            objModule = getattr(obj, "__module__", "")
            if objModule != toolPackage and not objModule.startswith(toolPackage + "."):
                continue

            # 生成 schema
            schema = _generateFunctionSchema(obj, toolName)
            toolSchemas.append(schema)
            toolCallables[name] = obj

        # 存储到缓存
        if toolSchemas:
            _toolsSchema[toolName] = {
                "tool": toolName,
                "functions": toolSchemas,
            }
            _toolsCallable[toolName] = toolCallables

    _schemaBuilt = True




def getToolsSchema(toolNames: set[str]) -> list[dict]:
    """
    根据工具名集合，返回 OpenAI format 的 schema 列表

    参数:
        toolNames: 工具名集合（由 afcIntent.detectTools 返回）

    返回:
        工具 schema 列表（OpenAI function calling format）
    """
    _scanTools()

    schemas = []
    for toolName in toolNames:
        toolSchema = _toolsSchema.get(toolName)
        if toolSchema:
            schemas.extend(toolSchema["functions"])

    return schemas




def getToolCallable(toolName: str, funcName: str) -> callable | None:
    """
    根据工具名和函数名，返回可调用对象

    参数:
        toolName: 工具名
        funcName: 函数名

    返回:
        可调用对象，如果不存在则返回 None
    """
    _scanTools()

    toolCallables = _toolsCallable.get(toolName)
    if not toolCallables:
        return None

    return toolCallables.get(funcName)




def getAllToolNames() -> set[str]:
    """
    返回所有已注册的工具名

    返回:
        工具名集合
    """
    _scanTools()
    return set(_toolsSchema.keys())
