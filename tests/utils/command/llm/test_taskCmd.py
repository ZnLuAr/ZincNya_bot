"""
tests/utils/command/llm/test_taskCmd.py

llm tasks / llm stop 控制台子命令：列表渲染、停止三态输出与 _dispatch 分发。
"""

from unittest.mock import AsyncMock

import pytest

from utils.command.llm._dispatch import execute
from utils.command.llm.taskCmd import handleTaskCommand
from utils.llm.context import runtime
from utils.llm.context.runtime import registerTask
from utils.llm.operation.types import TaskInfo


@pytest.fixture(autouse=True)
def cleanRuntimeState():
    """本目录不在 tests/utils/llm/ 的 conftest 覆盖内，登记表就地清理。"""
    runtime._runningTasks.clear()
    runtime._contextGates.clear()
    yield
    runtime._runningTasks.clear()
    runtime._contextGates.clear()


def _registerOne(taskID: str = "aaaaaaaa-1111", *, preview: str = "帮我看一下这段代码"):
    registerTask(TaskInfo(taskID=taskID, chatID="100"), chatID="100", contextID="ctx", preview=preview)


async def test_tasksEmptyPrintsHint(capsys):
    await handleTaskCommand([], app=None)

    assert "没有正在运行" in capsys.readouterr().out


async def test_tasksListsRegisteredTask(capsys):
    _registerOne()

    await handleTaskCommand(["tasks"], app=None)

    out = capsys.readouterr().out
    assert "aaaaaaaa-1111" in out
    assert "100" in out
    assert "帮我看一下这段代码" in out


async def test_stopWithoutIDPrintsUsage(capsys):
    await handleTaskCommand(["stop"], app=None)

    assert "taskID" in capsys.readouterr().out


async def test_stopByPrefixPrintsFullTaskIDAndLogs(capsys, monkeypatch):
    _registerOne()
    logMock = AsyncMock()
    monkeypatch.setattr("utils.command.llm.taskCmd.logAction", logMock)

    await handleTaskCommand(["stop", "aaaaaaaa"], app=None)

    out = capsys.readouterr().out
    assert "aaaaaaaa-1111" in out
    assert "停止信号" in out
    logMock.assert_awaited_once()


async def test_stopMissingTaskPrintsError(capsys):
    await handleTaskCommand(["stop", "bbbbbbbb"], app=None)

    assert "没有匹配" in capsys.readouterr().out


async def test_stopAmbiguousPrefixPrintsError(capsys):
    _registerOne("cccccccc-1111")
    _registerOne("cccccccc-2222")

    await handleTaskCommand(["stop", "cccccccc"], app=None)

    assert "2 个任务" in capsys.readouterr().out


async def test_dispatchRoutesTasksAndStop(monkeypatch, capsys):
    routed = []

    async def fakeHandler(rest, app):
        routed.append(rest)

    monkeypatch.setattr("utils.command.llm._dispatch.handleTaskCommand", fakeHandler)
    await execute(None, ["tasks"])
    await execute(None, ["stop", "aaaaaaaa"])

    assert routed == [[], ["aaaaaaaa"]]
    assert capsys.readouterr().out == ""
    assert runtime.listRunningTasks() == []
