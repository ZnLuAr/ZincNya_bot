"""
utils/afc/contextBuilder.py

AFC 工具上下文渲染

把 registry 产出的 OpenAI function schema 列表，渲染成 LLM 可理解的文本块。
输出含 <AFC_TOOLS> 工具清单与 <AFC_ACTION> 调用格式说明，供推送层注入。
"""


def _renderSignature(funcSchema: dict) -> str:
    """把单个函数 schema 渲染为 name(param: type, param?: type) 形式的签名串"""
    name = funcSchema.get("name", "")
    parameters = funcSchema.get("parameters") or {}
    properties = parameters.get("properties") or {}
    required = set(parameters.get("required") or [])

    parts = []
    for paramName, paramSchema in properties.items():
        jsonType = (paramSchema or {}).get("type", "string")
        # 非必填参数加 ? 标记，和必填区分开
        if paramName in required:
            parts.append(f"{paramName}: {jsonType}")
        else:
            parts.append(f"{paramName}?: {jsonType}")

    return f"{name}({', '.join(parts)})"


def _renderParamDetails(funcSchema: dict) -> str:
    """渲染参数明细行（含必填/可选标注与描述）；无参数时返回空串"""
    parameters = funcSchema.get("parameters") or {}
    properties = parameters.get("properties") or {}
    required = set(parameters.get("required") or [])

    if not properties:
        return ""

    items = []
    for paramName, paramSchema in properties.items():
        desc = (paramSchema or {}).get("description", "")
        flag = "必填" if paramName in required else "可选"
        if desc:
            items.append(f"{paramName}({flag}) - {desc}")
        else:
            items.append(f"{paramName}({flag})")

    return "; ".join(items)




def buildToolsContext(toolsSchema: list[dict]) -> str:
    """
    将工具 schema 列表转换为 LLM 可理解的上下文字符串。

    参数:
        toolsSchema: OpenAI function calling format 的 schema 列表
                     （由 registry.getToolsSchema 返回）

    返回:
        含 <AFC_TOOLS> 标签与 <AFC_ACTION> 调用说明的文本块；
        toolsSchema 为空时返回空字符串。
    """
    if not toolsSchema:
        return ""

    lines = ["<AFC_TOOLS>"]
    for funcSchema in toolsSchema:
        signature = _renderSignature(funcSchema)
        description = funcSchema.get("description", "")
        lines.append(f"- {signature}: {description}")

        details = _renderParamDetails(funcSchema)
        if details:
            lines.append(f"  参数: {details}")
    lines.append("</AFC_TOOLS>")

    lines.append("")
    lines.append("需要调用工具时，输出如下格式（可一次输出多个 AFC_ACTION 块）：")
    lines.append("<AFC_ACTION>")
    lines.append('{"tool": "工具名", "parameters": {"参数名": "参数值"}}')
    lines.append("</AFC_ACTION>")
    lines.append("调用后会收到 <FUNCTION_RESULT>，再据此组织最终回复。")

    return "\n".join(lines)
