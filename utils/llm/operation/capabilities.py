"""
utils/llm/operation/capabilities.py

外部能力列表。

发言与记忆操作是 LLM 自带能力，由入口按任务构造；工具等外部能力在各自模块的
初始化函数里调用 registerCapability 把自己加进来。LLM 不 import 任何能力模块。
"""

from .types import Capability


# 能力名 -> 能力；同名登记以后一次为准
_capabilities: dict[str, Capability] = {}




def registerCapability(capability: Capability) -> None:
    """登记一种外部能力；重复登记的同名能力会覆盖前一次。"""
    _capabilities[capability.name] = capability


def listCapabilities() -> list[Capability]:
    """返回已登记的外部能力，按登记顺序。"""
    return list(_capabilities.values())
