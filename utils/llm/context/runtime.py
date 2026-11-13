"""
utils/llm/context/runtime.py

运行中任务的登记与上下文执行权的调度。

任务入口在任务开始运行时登记（持有 stopEvent），停止入口按 taskID 前缀定位并
置位；同一上下文的任务经 executionSlot 串行执行，停止运行中的任务会同时作废
该上下文已经排队但尚未开始的请求。
"""

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator

from utils.llm.operation.types import TaskInfo




# 运行任务列表的预览截断长度（两端命令的展示需要，非业务阈值）
_RUNNING_PREVIEW_LEN = 60
# 停止命令接受的 taskID 前缀最短长度（更短的前缀在任务稍多时几乎必然歧义）
_STOP_PREFIX_MIN_LEN = 4


@dataclass
class RunningTask:
    """登记中的一条运行任务；task 持有接收停止信号的 stopEvent。"""

    task: TaskInfo
    chatID: str
    contextID: str
    startedAt: float
    preview: str


@dataclass
class _ContextGate:
    """一个上下文的执行权：串行锁，加一代数用于作废排队中的请求。"""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    generation: int = 0


_runningTasks: dict[str, RunningTask] = {}


_contextGates: dict[str, _ContextGate] = {}




def registerTask(task: TaskInfo, *, chatID: str, contextID: str, preview: str) -> None:
    """登记一个开始运行的任务，供停止入口按 taskID 定位。"""
    _runningTasks[task.taskID] = RunningTask(
        task=task, chatID=str(chatID), contextID=contextID,
        startedAt=time.time(), preview=" ".join(preview.split())[:_RUNNING_PREVIEW_LEN],
    )


def unregisterTask(taskID: str) -> None:
    """移除任务登记；不在表中时静默返回（收尾路径可能重复经过）"""
    _runningTasks.pop(taskID, None)


def listRunningTasks() -> list[dict]:
    """按开始时间排序返回运行任务的摘要副本，供两端命令展示。"""
    return [
        {
            "taskID": entry.task.taskID,
            "chatID": entry.chatID,
            "contextID": entry.contextID,
            "startedAt": entry.startedAt,
            "preview": entry.preview,
        }
        for entry in sorted(_runningTasks.values(), key=lambda entry: entry.startedAt)
    ]


def stopTask(idPrefix: str) -> str:
    """
    按唯一前缀定位运行任务并置位停止信号，返回完整 taskID。

    无匹配或多于一个匹配时抛 ValueError。停止只阻止新的开始，不打断进行中的
    模型请求与操作（循环在检查点响应）；同上下文排队未开始的请求一并作废。
    """
    if len(idPrefix) < _STOP_PREFIX_MIN_LEN:
        raise ValueError(f"taskID 前缀至少要 {_STOP_PREFIX_MIN_LEN} 个字符")
    matches = [taskID for taskID in _runningTasks if taskID.startswith(idPrefix)]
    if not matches:
        raise ValueError("没有匹配的运行任务")
    if len(matches) > 1:
        raise ValueError(f"前缀匹配到 {len(matches)} 个任务，请加长前缀区分")
    entry = _runningTasks[matches[0]]
    entry.task.stopEvent.set()
    invalidateQueuedTasks(entry.contextID)
    return entry.task.taskID




def _gateFor(contextID: str) -> _ContextGate:
    """
    取得或创建上下文的执行权。只有共享上下文会建门，量级有界于活跃聊天数；
    常驻不删：释放后删除会让并发到达的请求各持一把锁，串行失效。
    """
    gate = _contextGates.get(contextID)
    if gate is None:
        gate = _ContextGate()
        _contextGates[contextID] = gate
    return gate


@asynccontextmanager
async def executionSlot(contextID: str, *, gated: bool = True) -> AsyncIterator[bool]:
    """
    获取上下文的执行权，同一上下文的任务一次只跑一个。

    独立上下文（gated=False）是一次性的，不可能有并发任务知道这个
    contextID，直接放行且不建门——否则每个请求都会留下永久门条目。
    排队等待期间该上下文有任务被停止（generation 递增）时，获得锁后 yield
    False 表示本次排队已作废；正常获得执行权 yield True。
    """
    if not gated:
        yield True
        return
    gate = _gateFor(contextID)
    snapshot = gate.generation
    async with gate.lock:
        yield gate.generation == snapshot


def invalidateQueuedTasks(contextID: str) -> None:
    """作废该上下文排队未开始的请求；正在运行的任务不受影响（由 stopEvent 负责）。"""
    gate = _contextGates.get(contextID)
    if gate is not None:
        gate.generation += 1
