"""
tests/utils/afc/test_capability.py

测试 AFC 向 LLM 操作循环登记的 tool 能力
"""

from unittest.mock import patch, AsyncMock

import pytest

from utils.llm.operation import capabilities
from utils.llm.operation.capabilities import listCapabilities
from utils.llm.operation.types import Operation, OperationResult, OperationStatus, TaskInfo

import utils.afc.toolManager as toolManager
from utils.afc.capability import (
    TOOL_CAPABILITY_NAME,
    registerToolCapability,
    _describe,
    _validate,
    _handle,
)




_TASK = TaskInfo(taskID="task-1", chatID="chat-1")




@pytest.fixture
def isolatedCapabilities(monkeypatch):
    """换上一份空的能力表，用例里的登记不留在全局；结束后还原。"""
    monkeypatch.setattr(capabilities, "_capabilities", {})


@pytest.fixture
def toolSwitches(monkeypatch):
    """启用状态只查内存，不读真实 data/；返回的函数用来写入开关。"""
    monkeypatch.setattr(toolManager, "_afcToolsConfigCache", {})
    monkeypatch.setattr(toolManager, "_builtinDefaultsCache", {})

    def _setSwitches(switches: dict):
        monkeypatch.setattr(
            toolManager,
            "_afcToolsConfigCache",
            {name: {"enabled": enabled} for name, enabled in switches.items()},
        )

    return _setSwitches


def _operation(args: dict) -> Operation:
    return Operation(index=1, name=TOOL_CAPABILITY_NAME, args=args)




class TestRegisterToolCapability:
    """测试 registerToolCapability 的登记结果"""

    def test_capability_name(self):
        assert TOOL_CAPABILITY_NAME == "tool"

    def test_registers_tool_capability(self, isolatedCapabilities):
        assert listCapabilities() == []

        registerToolCapability()

        registered = listCapabilities()
        assert [capability.name for capability in registered] == ["tool"]
        assert registered[0].describe is _describe
        assert registered[0].validate is _validate
        assert registered[0].handle is _handle

    def test_register_twice_keeps_single_entry(self, isolatedCapabilities):
        registerToolCapability()
        registerToolCapability()
        assert [capability.name for capability in listCapabilities()] == ["tool"]




class TestDescribe:
    """测试 _describe：说明里只有已启用的工具"""

    def test_lists_enabled_tools(self, toolSwitches):
        text = _describe(_TASK)
        assert '{"op": "tool"' in text
        assert "- calc.calculate(expression: string)" in text
        assert "- datetime.getFutureDate(days: integer, fmt?: string)" in text
        assert "- weather.getWeather(city: string, date?: string)" in text

    def test_omits_disabled_tool(self, toolSwitches):
        toolSwitches({"datetime": False})
        text = _describe(_TASK)
        assert "- calc.calculate(" in text
        assert "datetime." not in text

    def test_all_disabled_returns_empty(self, toolSwitches):
        """没有任何启用的工具时返回空串，本任务不提供 tool 能力"""
        toolSwitches({"calc": False, "datetime": False, "weather": False, "web": False})
        assert _describe(_TASK) == ""

    def test_passes_enabled_schema_to_builder(self):
        schemas = [{"tool": "x", "functions": []}]
        with patch("utils.afc.capability.getEnabledToolsSchema", return_value=schemas), \
             patch("utils.afc.capability.buildToolsDescription", return_value="说明") as mockBuild:
            assert _describe(_TASK) == "说明"
        mockBuild.assert_called_once_with(schemas)




class TestValidate:
    """测试 _validate：先拦多余字段，再转给 validateCall"""

    async def test_rejects_unknown_key(self):
        with patch("utils.afc.capability.validateCall") as mockValidate:
            reason = await _validate({"name": "calc.calculate", "parameters": {}, "function": "x"}, _TASK)

        assert reason == "tool 操作只接受 name 和 parameters 两个字段，多出了：function"
        mockValidate.assert_not_called()

    async def test_rejects_multiple_unknown_keys_sorted(self):
        """多出的字段名按字典序列出"""
        with patch("utils.afc.capability.validateCall") as mockValidate:
            reason = await _validate({"name": "calc.calculate", "tool": "calc", "arguments": {}}, _TASK)

        assert reason == "tool 操作只接受 name 和 parameters 两个字段，多出了：arguments、tool"
        mockValidate.assert_not_called()

    async def test_forwards_to_validate_call(self):
        parameters = {"expression": "1 + 1"}
        with patch("utils.afc.capability.validateCall", return_value=None) as mockValidate:
            reason = await _validate({"name": "calc.calculate", "parameters": parameters}, _TASK)

        assert reason is None
        mockValidate.assert_called_once_with("calc.calculate", parameters)

    async def test_returns_validate_call_reason(self):
        with patch("utils.afc.capability.validateCall", return_value="未知工具：ghost"):
            reason = await _validate({"name": "ghost.run", "parameters": {}}, _TASK)

        assert reason == "未知工具：ghost"

    async def test_missing_fields_forwarded_as_none(self):
        """缺 name / parameters 时以 None 转交，由 validateCall 判断"""
        with patch("utils.afc.capability.validateCall", return_value="原因") as mockValidate:
            reason = await _validate({}, _TASK)

        assert reason == "原因"
        mockValidate.assert_called_once_with(None, None)

    async def test_missing_name_rejected_by_real_validate_call(self):
        assert await _validate({"parameters": {}}, _TASK) == "name 必须写成「工具名.函数名」"

    async def test_real_call_passes(self, toolSwitches):
        args = {"name": "calc.calculate", "parameters": {"expression": "1 + 1"}}
        assert await _validate(args, _TASK) is None

    async def test_real_call_to_disabled_tool(self, toolSwitches):
        toolSwitches({"calc": False})
        args = {"name": "calc.calculate", "parameters": {"expression": "1 + 1"}}
        assert await _validate(args, _TASK) == "工具 calc 当前未启用"




class TestHandle:
    """测试 _handle：转给 runTool 并交回它的结果"""

    async def test_forwards_to_run_tool(self):
        expected = OperationResult(status=OperationStatus.DONE, modelText="2")
        parameters = {"expression": "1 + 1"}

        with patch("utils.afc.capability.runTool", new=AsyncMock(return_value=expected)) as mockRun:
            result = await _handle(_operation({"name": "calc.calculate", "parameters": parameters}), _TASK)

        assert result is expected
        mockRun.assert_awaited_once_with("calc.calculate", parameters)

    async def test_missing_parameters_forwarded_as_none(self):
        expected = OperationResult(status=OperationStatus.DONE, modelText="now")

        with patch("utils.afc.capability.runTool", new=AsyncMock(return_value=expected)) as mockRun:
            result = await _handle(_operation({"name": "datetime.getCurrentTime"}), _TASK)

        assert result is expected
        mockRun.assert_awaited_once_with("datetime.getCurrentTime", None)

    async def test_failed_result_passed_through(self):
        expected = OperationResult(status=OperationStatus.FAILED, modelText="除数不能为零")

        with patch("utils.afc.capability.runTool", new=AsyncMock(return_value=expected)):
            result = await _handle(_operation({"name": "calc.calculate", "parameters": {}}), _TASK)

        assert result is expected




class TestRegisteredCapabilityEndToEnd:
    """从能力表里取出 tool 能力，按循环的用法走一遍真实工具"""

    @pytest.fixture(autouse=True)
    def silenceLogger(self):
        with patch("utils.afc.executor.logSystemEvent", new=AsyncMock()), \
             patch("utils.afc.tools.calc.calculator.logSystemEvent", new=AsyncMock()):
            yield

    def _toolCapability(self):
        registerToolCapability()
        return listCapabilities()[0]

    async def test_validate_then_handle_success(self, isolatedCapabilities, toolSwitches):
        capability = self._toolCapability()
        args = {"name": "calc.calculate", "parameters": {"expression": "2 + 3"}}

        assert "- calc.calculate(" in capability.describe(_TASK)
        assert await capability.validate(args, _TASK) is None

        result = await capability.handle(_operation(args), _TASK)
        assert result == OperationResult(status=OperationStatus.DONE, modelText="2 + 3 = 5")

    async def test_tool_failure_reaches_model(self, isolatedCapabilities, toolSwitches):
        capability = self._toolCapability()
        args = {"name": "calc.calculate", "parameters": {"expression": "1 / 0"}}

        assert await capability.validate(args, _TASK) is None

        result = await capability.handle(_operation(args), _TASK)
        assert result == OperationResult(status=OperationStatus.FAILED, modelText="除数不能为零")
