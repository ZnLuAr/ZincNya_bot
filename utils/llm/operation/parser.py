"""
utils/llm/operation/parser.py

解析模型的一次输出。

约定的输出是恰好一个 <OPERATIONS> 块，块内是一个 JSON 对象：
    {"operations": [{"op": "能力名", ...参数}, ...], "end": true}

这里只做格式层面的整批检查：任何一处不符合约定都抛 BatchFormatError，整批不
执行。各操作的参数是否合法由对应能力的 validate 判断。块之外的文字直接忽略，
不当作发言。
"""

import re
import json
from dataclasses import dataclass

from config import LLM_BATCH_MAX_OPERATIONS


_OPEN_TAG = "<OPERATIONS>"
_CLOSE_TAG = "</OPERATIONS>"
_OP_KEY = "op"

# 模型常把 JSON 包在 ```json ... ``` 里；只剥最外层这一对
_CODE_FENCE = re.compile(r"^```[a-zA-Z]*\s*\n?(.*?)\n?```$", re.DOTALL)




class BatchFormatError(ValueError):
    """模型输出不符合操作块格式；消息文本会原样反馈给模型。"""


@dataclass(frozen=True, kw_only=True)
class ParsedBatch:
    """一次输出解析出的操作（能力名, 参数）与结束信号。"""

    operations: tuple[tuple[str, dict], ...]
    end: bool




def parseBatch(output: str, *, maxOperations: int = LLM_BATCH_MAX_OPERATIONS) -> ParsedBatch:
    """解析一次模型输出；格式不符时抛 BatchFormatError。"""
    text = output or ""
    openCount = text.count(_OPEN_TAG)
    closeCount = text.count(_CLOSE_TAG)

    if openCount == 0 and closeCount == 0:
        raise BatchFormatError(f"没有找到 {_OPEN_TAG} 块")
    if openCount != 1 or closeCount != 1:
        raise BatchFormatError(
            f"{_OPEN_TAG} 块必须恰好出现一次且完整闭合"
            f"（起始标记 {openCount} 个，结束标记 {closeCount} 个）。"
            "输出可能被截断；操作的参数里也不能原样写出这两个标记"
        )

    start = text.index(_OPEN_TAG) + len(_OPEN_TAG)
    stop = text.index(_CLOSE_TAG)
    if stop < start:
        raise BatchFormatError(f"{_CLOSE_TAG} 出现在 {_OPEN_TAG} 之前")

    body = text[start:stop].strip()
    fenced = _CODE_FENCE.match(body)
    if fenced:
        body = fenced.group(1).strip()

    try:
        data = json.loads(body)
    except RecursionError as e:
        raise BatchFormatError(f"{_OPEN_TAG} 块内的 JSON 嵌套层数过多") from e
    except ValueError as e:
        raise BatchFormatError(f"{_OPEN_TAG} 块内不是合法 JSON：{e}") from e

    if not isinstance(data, dict):
        raise BatchFormatError(f"{_OPEN_TAG} 块内必须是一个 JSON 对象")

    end = data.get("end", False)
    if not isinstance(end, bool):
        raise BatchFormatError("end 必须是 true 或 false")

    rawOperations = data.get("operations", [])
    if not isinstance(rawOperations, list):
        raise BatchFormatError("operations 必须是数组")
    if len(rawOperations) > maxOperations:
        raise BatchFormatError(
            f"一次最多 {maxOperations} 个操作，这次有 {len(rawOperations)} 个"
        )
    if not rawOperations and not end:
        raise BatchFormatError("operations 为空且 end 不是 true：要么给出操作，要么结束")

    operations = []
    for position, item in enumerate(rawOperations, start=1):
        if not isinstance(item, dict):
            raise BatchFormatError(f"第 {position} 个操作必须是 JSON 对象")
        name = item.get(_OP_KEY)
        if not isinstance(name, str) or not name.strip():
            raise BatchFormatError(f"第 {position} 个操作的 {_OP_KEY} 字段必须是非空字符串")
        args = {key: value for key, value in item.items() if key != _OP_KEY}
        operations.append((name.strip(), args))

    return ParsedBatch(operations=tuple(operations), end=end)
