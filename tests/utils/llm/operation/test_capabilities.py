"""
tests/utils/llm/operation/test_capabilities.py

测试外部能力登记表（capabilities.py）：registerCapability / listCapabilities，
以及 operation 包的导出。
"""

import json
from unittest.mock import AsyncMock

import pytest

import utils.llm.operation as operationPackage
from utils.llm.operation import capabilities as capabilitiesModule
from utils.llm.operation.loop import runTask
from utils.llm.operation.capabilities import listCapabilities, registerCapability
from utils.llm.operation.types import (
    Capability,
    OperationResult,
    OperationStatus,
    TaskInfo,
)




_EMPTY_END = '<OPERATIONS>{"operations": [], "end": true}</OPERATIONS>'




@pytest.fixture(autouse=True)
def mockLog(monkeypatch):
    """循环的日志不落盘。"""
    mock = AsyncMock()
    monkeypatch.setattr("utils.llm.operation.loop.logSystemEvent", mock)
    return mock


@pytest.fixture
def emptyRegistry(monkeypatch):
    """把模块级登记表换成空表，用例结束后由 monkeypatch 恢复原表。"""
    registry = {}
    monkeypatch.setattr(capabilitiesModule, "_capabilities", registry)
    return registry




def _task(**overrides) -> TaskInfo:
    fields = {"taskID": "t1", "chatID": "100", "userID": 42, "includeContext": True}
    fields.update(overrides)
    return TaskInfo(**fields)


def _batch(*operations, end=True) -> str:
    payload = json.dumps({"operations": list(operations), "end": end}, ensure_ascii=False)
    return f"<OPERATIONS>{payload}</OPERATIONS>"


def _scriptedModel(*outputs):
    """按顺序返回预设输出的模型回调，连同它每次收到的 appendix 列表一起返回。"""
    appendices = []

    async def _callModel(*, instructions, appendix):
        appendices.append(appendix)
        return outputs[len(appendices) - 1]

    return _callModel, appendices


def _plainCapability(name: str, description: str = "") -> Capability:
    async def _validate(args, task):
        return None

    async def _handle(operation, task):
        return OperationResult(status=OperationStatus.DONE)

    return Capability(
        name=name,
        describe=lambda task: description or f"【{name}】",
        validate=_validate,
        handle=_handle,
    )




# ===========================================================================
# 外部能力登记表
# ===========================================================================

def test_listCapabilitiesEmptyWhenNothingRegistered(emptyRegistry):
    assert listCapabilities() == []


def test_registerCapabilityKeepsRegistrationOrder(emptyRegistry):
    tool = _plainCapability("tool")
    search = _plainCapability("search")
    calc = _plainCapability("calc")

    for capability in (tool, search, calc):
        registerCapability(capability)

    assert listCapabilities() == [tool, search, calc]


def test_registerCapabilityReplacesSameName(emptyRegistry):
    old = _plainCapability("tool", "【tool】旧说明")
    other = _plainCapability("search")
    new = _plainCapability("tool", "【tool】新说明")

    registerCapability(old)
    registerCapability(other)
    registerCapability(new)

    # 同名以后一次为准；位置留在第一次登记的地方
    capabilities = listCapabilities()
    assert len(capabilities) == 2
    assert capabilities[0] is new
    assert capabilities[1] is other


def test_listCapabilitiesReturnsCopy(emptyRegistry):
    tool = _plainCapability("tool")
    registerCapability(tool)

    listed = listCapabilities()
    listed.clear()
    listed.append(_plainCapability("intruder"))

    assert listCapabilities() == [tool]


def test_emptyRegistryFixtureLeavesRealRegistryUntouched(emptyRegistry):
    registerCapability(_plainCapability("tool"))

    # 登记进了替换上去的空表，不是模块原来的那张
    assert list(emptyRegistry) == ["tool"]
    assert capabilitiesModule._capabilities is emptyRegistry


async def test_runTaskDoesNotReadRegistryByItself(emptyRegistry):
    """登记表由入口取出后传给循环；循环只认传进来的能力。"""
    registerCapability(_plainCapability("tool"))
    callModel, appendices = _scriptedModel(_batch({"op": "tool"}), _EMPTY_END)

    outcome = await runTask(task=_task(), capabilities=[], callModel=callModel)

    assert "没有这个能力；可用的能力：无" in appendices[1]
    assert outcome.records == ()




# ===========================================================================
# operation 包的导出
# ===========================================================================

def test_packageExportsResolve():
    for name in operationPackage.__all__:
        assert hasattr(operationPackage, name), name

    assert operationPackage.registerCapability is registerCapability
    assert operationPackage.listCapabilities is listCapabilities
    assert operationPackage.runTask is runTask
