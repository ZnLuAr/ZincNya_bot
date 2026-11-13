"""
utils/llm/operation/types.py

操作循环的数据类型与公共契约。

一次任务从触发消息开始，到模型给出结束信号、达到调用上限、被停止或失败为止。
模型每次输出一批操作；每个操作由对应的能力（Capability）校验并处理，处理的
实际结果记入任务记录并在下一次调用时交还模型。
"""

import asyncio
from enum import Enum
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Protocol




class OperationStatus(str, Enum):
    """单个操作的实际结果。"""

    DONE = "done"               # 已完成：副作用已发生
    SUBMITTED = "submitted"     # 已送审：等待人工批准，副作用尚未发生
    FAILED = "failed"           # 失败：确认没有完成
    UNCERTAIN = "uncertain"     # 结果不确定：无法确认副作用是否发生
    SKIPPED = "skipped"         # 尚未执行：前序操作未成功或执行前收到停止信号


class TaskStatus(str, Enum):
    """任务的结束方式。"""

    COMPLETED = "completed"             # 模型给出结束信号，且该批操作全部处理成功
    LIMIT_REACHED = "limitReached"      # 达到模型调用次数上限
    STOPPED = "stopped"                 # 被停止信号中断
    FAILED = "failed"                   # 模型输入、模型请求或工作上下文存储失败




@dataclass(frozen=True, kw_only=True)
class TaskInfo:
    """
    循环交给能力的可信任务信息。

    chatID / userID / triggerMessageID 由入口在权限校验后填入；模型输出里出现的
    任何 ID 都不能替代它们。stopEvent 由控制入口置位，循环在每次模型调用和每个
    操作开始前检查，模型无法清除。
    """

    taskID: str
    chatID: str
    userID: int | str | None = None
    triggerMessageID: int | None = None
    includeContext: bool = False
    stopEvent: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(frozen=True, kw_only=True)
class Operation:
    """模型输出的一个操作；index 是它在本任务内的序号（从 1 起）。"""

    index: int
    name: str
    args: dict


@dataclass(frozen=True, kw_only=True)
class OperationResult:
    """
    能力交回的实际结果。

    modelText 在下一次调用时交给模型；display 给 ops 看，留空时展示 modelText。
    """

    status: OperationStatus
    modelText: str = ""
    display: str = ""


@dataclass(frozen=True, kw_only=True)
class OperationRecord:
    """任务记录中的一条：操作及其实际结果。"""

    operation: Operation
    result: OperationResult




@dataclass(frozen=True, kw_only=True)
class Capability:
    """
    一种操作能力交给循环的三样东西。

    describe(task) -> str
        给模型看的说明，任务开始时取一次；返回空串表示本任务不提供该能力。
    validate(args, task) -> str | None
        无副作用地判断一次操作是否合法；合法返回 None，否则返回给模型看的原因。
    handle(operation, task) -> OperationResult
        执行或送审，交回实际结果。只会在同批操作全部通过 validate 之后被调用。
    """

    name: str
    describe: Callable[[TaskInfo], str]
    validate: Callable[[dict, TaskInfo], Awaitable[str | None]]
    handle: Callable[[Operation, TaskInfo], Awaitable[OperationResult]]




@dataclass(frozen=True, kw_only=True)
class TaskOutcome:
    """任务结束后的汇总；error 仅在任务状态为失败时有值。"""

    status: TaskStatus
    records: tuple[OperationRecord, ...] = ()
    modelCalls: int = 0
    detail: str = ""
    error: Exception | None = None
    taskID: str = ""
    contextID: str = ""




class OperationJournal(Protocol):
    """
    操作记录的保存接口，由循环在执行前后调用。

    checkReady 检查存储是否可用；beforeOperation 保存即将执行的操作，
    afterOperation 保存实际结果，包括因前序失败而跳过的操作。
    保存异常由循环交回任务入口，已发生的实际结果保留在 TaskOutcome 中。
    """

    def checkReady(self) -> None: ...

    async def beforeOperation(self, operation: Operation) -> None: ...

    async def afterOperation(self, record: OperationRecord) -> None: ...
