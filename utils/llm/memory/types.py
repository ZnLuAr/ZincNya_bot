"""
utils/llm/memory/types.py

检索与写入审核共用的数据结构：

- MemoryQuery / MemoryRetrievalResult：一次检索的输入与输出；
- MemoryWriteGuard：写入前比对目标状态，防旧审核卡覆盖新修改；
- 两个指纹函数：内容指纹决定向量缓存还能不能复用，状态指纹
  判断审核卡/检索结果是否已经过期。

字段读写同时兼容业务对象的 camelCase 与数据库行的 snake_case。
"""

import json
import hashlib
from dataclasses import dataclass, field
from typing import Any




@dataclass(frozen=True, kw_only=True)
class MemoryTurn:
    """
    一次当前消息及其可选回复；字段顺序也是检索查询的语义顺序。

    一次防抖批次可能合并多条消息，每条配对成 一个 turn。
    replyText 为空表示该消息没有引用回复。
    """

    currentText: str
    replyText: str = ""
    currentSender: str = ""
    replySender: str = ""


@dataclass(frozen=True, kw_only=True)
class MemoryQuery:
    """
    一次「该召回哪些记忆」提问的完整内容，构造后不可变。

    由 messagePrep 从本轮防抖消息组装，随请求传到 contextBuilder 的
    检索入口；retry 会复用其中的 turns，contextBuilder 则用本次共享的
    history 快照替换 history；:fb 会创建只在该次生成使用的副本。三个
    字段的分工：

    - turns 是当前请求（含引用的消息）；
    - history：本次请求一并加载的近期聊天记录——只作为语义通道的
      背景参考，不参与词面匹配（避免历史中的旧词让记忆凭字面命中）；
    - feedbackText：ops 用 :fb 补充的修改意见，仅在该次反馈重试并入查询。
    """

    turns: tuple[MemoryTurn, ...] = ()
    history: tuple[dict, ...] = ()
    feedbackText: str = ""


@dataclass(kw_only=True)
class MemoryRetrievalResult:
    """检索完成后交给 prompt 组装层的结果。

    items 是最终入选的记忆（已完成预算裁剪，且注入前重查过数据库、
    剔掉了选择期间被改/被删的条目）；contextBlock 是 items 按低信任
    格式渲染好的完整文本块，可直接拼进 prompt，空串表示本轮没选中
    任何记忆；diagnostics 记录模式/通道/耗时等过程数据，只写日志。
    """

    items: list[dict] = field(default_factory=list)
    contextBlock: str = ""
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        """移除只供检索内部使用的字段，限制结果对象的暴露面。"""
        # 只向后续 prompt/审核链路暴露业务字段，避免把内部评分或缓存元数据
        # 意外当成可供模型读取的 memory 内容。
        allowedKeys = {
            "id",
            "scope_type",
            "scope_id",
            "content",
            "priority",
            "source",
            "mode",
        }
        self.items = [
            {
                key: value
                for key, value in item.items()
                if key in allowedKeys
            }
            for item in self.items
        ]


@dataclass(frozen=True, kw_only=True)
class MemoryWriteGuard:
    """防旧审核卡覆盖新修改的比对凭据。

    发审核卡时算好目标记忆当时的指纹存进 expectedState；
    在 ops 批准时，先重算当前指纹，和 expectedState 比对。
    对不上（说明这期间有人改过这条记忆）则拒绝写入。
    allowPinned 标记「pinned 记忆的写入已获人工确认」——模型自主
    申请的 pinned 操作拿不到这个标记，则过不了这条检查。
    """

    expectedState: str
    scopeType: str
    scopeID: str
    allowPinned: bool = False




def _memoryValue(memory: dict, camelKey: str, snakeKey: str, default=None):
    """兼容业务对象的 camelCase 与数据库行的 snake_case 字段。

    仅 scope 两个字段需要双形态：_rowToMemoryDict 输出的 dict 保留
    scope_type/scope_id 原生列名（buildMemoryContextBlock、
    _checkWriteGuard 等既有消费方依赖），指纹函数在这里归一。
    """
    if camelKey in memory:
        return memory[camelKey]
    return memory.get(snakeKey, default)


def _stableFingerprint(payload: dict) -> str:
    """将结构按稳定 JSON 顺序序列化后计算 SHA-256。"""
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def buildMemoryContentFingerprint(
    memory: dict,
    *,
    modelRevision: str,
    encodingVersion: str,
) -> str:
    """
    计算一条记忆的指纹，即语义缓存的版本。

    应该注意的是，本函数是纯计算，没有新旧之分——「旧」与「新」出现在
    调用方：runtime 缓存向量时记下一个指纹（旧），下次检索用当前
    字段再算一个（新），两个指纹不相等即「向量已过期，需重编码」。

    enabled/priority 不参与，它们不影响向量内容，改了它们不必重编码。
    """
    return _stableFingerprint({
        "id": memory.get("id"),
        "content": memory.get("content", ""),
        "tags": list(memory.get("tags") or []),
        "retrievalHint": memory.get("retrievalHint"),
        "modelRevision": modelRevision,
        "encodingVersion": encodingVersion,
    })


def buildMemoryStateFingerprint(memory: dict) -> str:
    """
    算一条记忆 所有可写字段的当前值 的指纹。

    阅读思路同内容指纹：「旧」是审核卡签发时（或候选被选中时）算的
    expectedState，「新」是批准时（或注入前复核时）对数据库当前行
    再算的——两个指纹不相等即「窗口期内有人改过，旧凭据作废」。

    覆盖 scope/enabled/mode/priority 在内的全部字段（内容指纹不管）。
    """
    return _stableFingerprint({
        "id": memory.get("id"),
        "scopeType": _memoryValue(memory, "scopeType", "scope_type", ""),
        "scopeID": _memoryValue(memory, "scopeID", "scope_id", ""),
        "content": memory.get("content", ""),
        "tags": list(memory.get("tags") or []),
        "retrievalHint": memory.get("retrievalHint"),
        "enabled": bool(memory.get("enabled", False)),
        "priority": memory.get("priority", 0),
        "mode": memory.get("mode", "contextual"),
        "source": memory.get("source", ""),
    })
