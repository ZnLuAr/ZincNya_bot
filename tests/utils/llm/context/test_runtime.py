"""
tests/utils/llm/context/test_runtime.py

运行任务登记与上下文执行权的单元测试：登记/注销/列表、按前缀停止、
串行与排队作废（generation）。
"""

import asyncio

import pytest

from utils.llm.context import runtime
from utils.llm.context.runtime import (
    executionSlot,
    invalidateQueuedTasks,
    listRunningTasks,
    registerTask,
    stopTask,
    unregisterTask,
)
from utils.llm.operation.types import TaskInfo


def _makeTask(taskID: str = "0123456789abcdef-0000") -> TaskInfo:
    return TaskInfo(taskID=taskID, chatID="100")


def _register(taskID: str, *, chatID: str = "100", contextID: str = "ctx-1", preview: str = "预览") -> TaskInfo:
    task = _makeTask(taskID)
    registerTask(task, chatID=chatID, contextID=contextID, preview=preview)
    return task




# ============================================================================
# 登记 / 注销 / 列表
# ============================================================================

class TestRegistry:

    def test_listReturnsSummaryCopiesSortedByStart(self):
        _register("11111111-0000", preview="第一条")
        _register("22222222-0000", preview="第二条")

        tasks = listRunningTasks()

        assert [info["taskID"] for info in tasks] == ["11111111-0000", "22222222-0000"]
        assert tasks[0]["chatID"] == "100"
        assert tasks[0]["contextID"] == "ctx-1"
        assert tasks[0]["preview"] == "第一条"
        # 摘要是副本：改动不影响内部登记
        tasks[0]["preview"] = "改过的"
        assert listRunningTasks()[0]["preview"] == "第一条"

    def test_unregisterIsIdempotent(self):
        _register("11111111-0000")
        unregisterTask("11111111-0000")
        unregisterTask("11111111-0000")
        assert listRunningTasks() == []

    def test_previewCollapsesWhitespaceAndTruncates(self):
        _register("11111111-0000", preview="第一行\n第二行  带连续空格")
        info = listRunningTasks()[0]
        assert "\n" not in info["preview"]
        assert "  " not in info["preview"]
        assert len(info["preview"]) <= runtime._RUNNING_PREVIEW_LEN




# ============================================================================
# 停止
# ============================================================================

class TestStopTask:

    def test_stopByUniquePrefixSetsStopEvent(self):
        task = _register("aaaaaaaa-1111")

        stoppedID = stopTask("aaaaaaaa")

        assert stoppedID == "aaaaaaaa-1111"
        assert task.stopEvent.is_set()

    def test_stopMissingTaskRaises(self):
        with pytest.raises(ValueError, match="没有匹配"):
            stopTask("bbbbbbbb")

    def test_stopAmbiguousPrefixRaises(self):
        _register("cccccccc-1111")
        _register("cccccccc-2222")

        with pytest.raises(ValueError, match="2 个任务"):
            stopTask("cccccccc")

    def test_stopPrefixTooShortRaises(self):
        _register("aaaaaaaa-1111")

        with pytest.raises(ValueError, match="至少"):
            stopTask("aa")

    def test_stopRepeatedIsIdempotent(self):
        task = _register("aaaaaaaa-1111")

        assert stopTask("aaaaaaaa") == "aaaaaaaa-1111"
        assert stopTask("aaaaaaaa-1111") == "aaaaaaaa-1111"
        assert task.stopEvent.is_set()

    async def test_stopInvalidatesQueuedTasksOfSameContext(self):
        _register("aaaaaaaa-1111", contextID="ctx-shared")
        _register("dddddddd-1111", contextID="ctx-other")
        # 门由排队/运行中的共享任务建立；stopTask 只 bump 已存在的门
        async with executionSlot("ctx-shared"):
            pass

        stopTask("aaaaaaaa")

        assert runtime._contextGates["ctx-shared"].generation == 1
        assert "ctx-other" not in runtime._contextGates, "无关上下文不应被触碰"




# ============================================================================
# 执行权：串行与排队作废
# ============================================================================

class TestExecutionSlot:

    async def test_serializesSameContext(self):
        order = []
        release = asyncio.Event()

        async def first():
            async with executionSlot("ctx-s") as valid:
                assert valid is True
                order.append("first-enter")
                await release.wait()

        async def second():
            async with executionSlot("ctx-s") as valid:
                assert valid is True
                order.append("second-enter")

        task1 = asyncio.create_task(first())
        await asyncio.sleep(0)
        task2 = asyncio.create_task(second())
        await asyncio.sleep(0.05)
        assert order == ["first-enter"]

        release.set()
        await asyncio.gather(task1, task2)
        assert order == ["first-enter", "second-enter"]

    async def test_invalidateMakesQueuedRequestLoseSlot(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        results = []

        async def running():
            async with executionSlot("ctx-q") as valid:
                assert valid is True
                entered.set()
                await release.wait()

        async def queued():
            async with executionSlot("ctx-q") as valid:
                results.append(valid)

        task1 = asyncio.create_task(running())
        await entered.wait()
        task2 = asyncio.create_task(queued())
        await asyncio.sleep(0.05)

        invalidateQueuedTasks("ctx-q")
        release.set()
        await asyncio.gather(task1, task2)

        assert results == [False]

    async def test_requestAfterInvalidateGetsSlot(self):
        async with executionSlot("ctx-r") as valid:
            assert valid is True
            invalidateQueuedTasks("ctx-r")

        async with executionSlot("ctx-r") as valid:
            # 作废之后新到达的请求不受影响（generation 快照在到达时取）
            assert valid is True

    async def test_differentContextsRunIndependently(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        order = []

        async def first():
            async with executionSlot("ctx-a") as valid:
                assert valid is True
                entered.set()
                await release.wait()

        async def other():
            async with executionSlot("ctx-b") as valid:
                assert valid is True
                order.append("other-enter")

        task1 = asyncio.create_task(first())
        await entered.wait()
        task2 = asyncio.create_task(other())
        await asyncio.sleep(0.05)

        release.set()
        await asyncio.gather(task1, task2)
        assert order == ["other-enter"]

    async def test_ungatedContextDoesNotCreateGate(self):
        """独立上下文（gated=False）直接放行，不留门条目。"""
        async with executionSlot("ctx-once", gated=False) as valid:
            assert valid is True
        assert "ctx-once" not in runtime._contextGates

    def test_invalidateUnknownContextDoesNotCreateGate(self):
        """作废未知上下文不应顺手创建门条目（非共享上下文没有排队者可作废）。"""
        invalidateQueuedTasks("ctx-unknown")
        assert "ctx-unknown" not in runtime._contextGates
