"""
utils/llm/afcApi/responseHandler.py

AFC 下游桥接：解析与执行 LLM 回复中的工具调用

负责 LLM 生成回复后的 AFC 执行循环（解析 <AFC_ACTION> → 执行工具 →
携带结果二次调用 LLM），直到无工具调用或达到迭代上限。
"""

import re
from typing import Optional

from utils.afc.executor import execute as executeAFCTool


AFC_ACTION_PATTERN = re.compile(r"<AFC_ACTION>(.*?)</AFC_ACTION>", re.DOTALL)


def extractAFCAction(llmResponse: str) -> Optional[str]:
    """
    提取并清洗 LLM 回复中的 <AFC_ACTION> JSON。

    鲁棒性处理：
        - 去除可能的 Markdown 代码块标记（```json / ```）
        - 清理首尾空白

    常见 LLM 输出格式：
        <AFC_ACTION>
        ```json
        {"tool": "weather", ...}
        ```
        </AFC_ACTION>

    或：
        <AFC_ACTION>{"tool": "weather", ...}</AFC_ACTION>

    参数:
        llmResponse: LLM 的原始回复文本

    返回:
        提取并清洗后的 JSON 字符串；无匹配时返回 None。
    """
    match = AFC_ACTION_PATTERN.search(llmResponse)
    if not match:
        return None

    raw = match.group(1).strip()

    # 去除 Markdown 代码块标记
    raw = re.sub(r'^```(?:json)?\s*', '', raw)  # 开头的 ```json 或 ```
    raw = re.sub(r'\s*```$', '', raw)           # 结尾的 ```

    return raw.strip()


async def handleAFCInLLMResponse(
    llmResponse: str,
    userMessage: str,
    chatID: str,
    userID: str | int | None = None,
    sessionID: str | int | None = None,
    includeContext: bool = False,
    maxIterations: int = 10,
) -> str:
    """
    循环处理 AFC 调用，直到 LLM 不再输出 <AFC_ACTION> 或达到上限。

    参数:
        llmResponse: LLM 的初始回复（可能含 <AFC_ACTION>）
        userMessage: 原始用户消息（用于二次调用时的上下文）
        chatID: 会话 ID
        userID: 用户 ID
        sessionID: 会话 ID（用于 memory scope）
        includeContext: 是否包含上下文（二次调用时为 False）
        maxIterations: 最多执行几轮工具调用

    返回:
        最终 LLM 回复（已去除所有 <AFC_ACTION> 标签）

    流程示例:
        1. LLM 回复含 <AFC_ACTION>{"tool":"weather",...}</AFC_ACTION>
        2. 提取 JSON，调用 executor.execute()
        3. 携带 <FUNCTION_RESULT>xxx</FUNCTION_RESULT> 作为新 userMessage 二次调用 LLM
        4. 重复直到无 <AFC_ACTION> 或达到 maxIterations
        5. 返回最终文本回复
    """
    currentResponse = llmResponse

    for iteration in range(maxIterations):
        actionJSON = extractAFCAction(currentResponse)
        if not actionJSON:
            # 无工具调用，结束循环
            break

        # 执行工具
        result = await executeAFCTool(actionJSON)

        # 携带工具结果作为新的 userMessage 二次调用 LLM
        followUpMessage = (
            f"[工具执行结果]\n"
            f"{result}\n\n"
            f"[原始用户消息]\n"
            f"{userMessage}"
        )

        # 内部导入，避免循环依赖（afcApi 在 utils/llm/afcApi/，generateReply 在 utils/llm/__init__.py）
        from utils.llm import generateReply

        currentResponse = await generateReply(
            userMessage=followUpMessage,
            chatID=chatID,
            userID=userID,
            sessionID=sessionID,
            includeContext=False,  # 二次调用不再拉 memory/history，避免重复注入
        )

    # 去除残留的 <AFC_ACTION> 标签（防止截断导致的格式错误）
    currentResponse = AFC_ACTION_PATTERN.sub("", currentResponse)

    return currentResponse