"""
tests/utils/afc/test_executor.py

测试 AFC 工具执行器：splitToolName / validateCall / runTool
"""

import asyncio
import threading
from unittest.mock import patch, AsyncMock

import pytest

from config import AFC_TOOL_RESULT_MAX_CHARS

from utils.core.logger import LogLevel
from utils.llm.operation.types import OperationResult, OperationStatus

import utils.afc.executor as executor
import utils.afc.registry as registry
import utils.afc.toolManager as toolManager
from utils.afc.errors import AFCError, ToolError, ToolDependencyError
from utils.afc.executor import splitToolName, validateCall, runTool




# 等待工具开始运行的上限（秒），只防用例卡死，正常情况下立即返回
_START_WAIT_SECONDS = 2

# 超时用例里替换 AFC_TOOL_TIMEOUT_SECONDS 的值（秒）
_SHORT_TIMEOUT_SECONDS = 0.05




# ============================================================================
# 测试用工具函数
# ============================================================================

async def echo(text: str, times: int = 1) -> str:
    """
    重复一段文字。

    参数：
        text: 要重复的文字
        times: 重复次数
    """
    return text * times


async def typed(
    text: str,
    count: int,
    ratio: float,
    flag: bool,
    items: list[str],
    options: dict[str, int],
) -> str:
    """每种 JSON 类型各占一个参数，用来测类型校验。"""
    return "ok"


async def loose(**kwargs) -> str:
    """接受任意参数名，参数不在 schema 里。"""
    return "ok"


_TYPED_VALID_PARAMS = {
    "text": "a",
    "count": 3,
    "ratio": 0.5,
    "flag": True,
    "items": ["a"],
    "options": {"a": 1},
}




# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture(autouse=True)
def mockLog():
    """替换执行器的日志函数：不落盘，并供用例检查记了什么。"""
    with patch("utils.afc.executor.logSystemEvent", new=AsyncMock()) as mock:
        yield mock


@pytest.fixture(autouse=True)
def allToolsEnabled(monkeypatch):
    """启用状态只查内存：空开关 + 空内置默认值 = 全部启用，不读真实 data/。"""
    monkeypatch.setattr(toolManager, "_afcToolsConfigCache", {})
    monkeypatch.setattr(toolManager, "_builtinDefaultsCache", {})


@pytest.fixture
def installTool(monkeypatch):
    """把测试函数登记成 registry 里的一个工具，用例结束后撤销。"""
    registry._scanTools()

    def _install(toolName, *funcs):
        monkeypatch.setitem(registry._toolsSchema, toolName, {
            "tool": toolName,
            "functions": [registry._generateFunctionSchema(func, toolName) for func in funcs],
        })
        monkeypatch.setitem(
            registry._toolsCallable, toolName, {func.__name__: func for func in funcs},
        )

    return _install


@pytest.fixture
def disableTool(monkeypatch):
    """把指定工具的开关写成停用。"""
    def _disable(toolName):
        monkeypatch.setattr(toolManager, "_afcToolsConfigCache", {toolName: {"enabled": False}})

    return _disable




# ============================================================================
# splitToolName
# ============================================================================

class TestSplitToolName:
    """测试「工具名.函数名」的拆分"""

    def test_valid_name(self):
        assert splitToolName("calc.calculate") == ("calc", "calculate")

    def test_strips_outer_whitespace(self):
        assert splitToolName("  calc.calculate\n") == ("calc", "calculate")

    def test_splits_at_first_separator(self):
        """多个点时只在第一个点处拆开，其余留在函数名里"""
        assert splitToolName("a.b.c") == ("a", "b.c")

    @pytest.mark.parametrize("name", [None, 123, ["calc.calculate"], {"name": "calc.calculate"}])
    def test_non_string_returns_none(self, name):
        assert splitToolName(name) is None

    @pytest.mark.parametrize("name", ["", "   ", "calc", ".calculate", "calc.", "."])
    def test_malformed_returns_none(self, name):
        """缺分隔符、缺工具名或缺函数名都算格式不对"""
        assert splitToolName(name) is None




# ============================================================================
# validateCall
# ============================================================================

class TestValidateCall:
    """测试 validateCall：能执行返回 None，否则返回给模型看的原因"""

    def test_valid_call(self, installTool):
        installTool("fake", echo)
        assert validateCall("fake.echo", {"text": "a", "times": 2}) is None

    def test_optional_parameter_can_be_omitted(self, installTool):
        installTool("fake", echo)
        assert validateCall("fake.echo", {"text": "a"}) is None

    def test_real_tool(self):
        """真实工具走完整条校验"""
        assert validateCall("calc.calculate", {"expression": "1 + 1"}) is None

    def test_does_not_run_tool(self, installTool):
        calls = []

        async def record(text: str) -> str:
            calls.append(text)
            return text

        installTool("fake", record)

        assert validateCall("fake.record", {"text": "a"}) is None
        assert calls == []

    @pytest.mark.parametrize("name", [None, 123, "", "calc", "calc.", ".calculate"])
    def test_malformed_name(self, name):
        assert validateCall(name, {}) == "name 必须写成「工具名.函数名」"

    def test_unknown_tool(self):
        assert validateCall("ghost.run", {}) == "未知工具：ghost"

    def test_disabled_tool(self, installTool, disableTool):
        installTool("fake", echo)
        disableTool("fake")
        assert validateCall("fake.echo", {"text": "a"}) == "工具 fake 当前未启用"

    def test_unknown_function(self, installTool):
        installTool("fake", echo)
        assert validateCall("fake.nope", {}) == "工具 fake 没有函数 nope"

    def test_none_parameters_treated_as_empty(self, installTool):
        """parameters 为 None 等同于没有参数"""
        async def ping() -> str:
            return "pong"

        installTool("fake", ping)
        assert validateCall("fake.ping", None) is None

    def test_none_parameters_still_checks_required(self, installTool):
        installTool("fake", echo)
        reason = validateCall("fake.echo", None)
        assert reason.startswith("参数不匹配：")
        assert "text" in reason

    @pytest.mark.parametrize("parameters", [["a"], "text=a", 1, True])
    def test_parameters_not_object(self, installTool, parameters):
        installTool("fake", echo)
        assert validateCall("fake.echo", parameters) == "parameters 必须是 JSON 对象"

    def test_missing_required_parameter(self, installTool):
        installTool("fake", echo)
        reason = validateCall("fake.echo", {"times": 2})
        assert reason.startswith("参数不匹配：")
        assert "text" in reason

    def test_unexpected_parameter(self, installTool):
        installTool("fake", echo)
        reason = validateCall("fake.echo", {"text": "a", "extra": 1})
        assert reason.startswith("参数不匹配：")
        assert "extra" in reason

    def test_all_types_accepted(self, installTool):
        installTool("fake", typed)
        assert validateCall("fake.typed", dict(_TYPED_VALID_PARAMS)) is None

    def test_number_accepts_integer(self, installTool):
        """number 类型的参数给整数也算对"""
        installTool("fake", typed)
        assert validateCall("fake.typed", {**_TYPED_VALID_PARAMS, "ratio": 1}) is None

    @pytest.mark.parametrize("paramName, value, jsonType", [
        ("text", 123, "string"),
        ("text", None, "string"),
        ("count", "3", "integer"),
        ("count", 3.0, "integer"),
        ("count", True, "integer"),
        ("ratio", "0.5", "number"),
        ("ratio", True, "number"),
        ("flag", 1, "boolean"),
        ("flag", "true", "boolean"),
        ("items", "a", "array"),
        ("items", {"a": 1}, "array"),
        ("options", ["a"], "object"),
        ("options", "a", "object"),
    ])
    def test_type_mismatch(self, installTool, paramName, value, jsonType):
        installTool("fake", typed)
        parameters = {**_TYPED_VALID_PARAMS, paramName: value}
        assert validateCall("fake.typed", parameters) == f"参数 {paramName} 的类型应为 {jsonType}"

    def test_parameter_without_schema_skips_type_check(self, installTool):
        """schema 里查不到的参数名不做类型判断"""
        installTool("fake", loose)
        assert validateCall("fake.loose", {"anything": 123}) is None




# ============================================================================
# runTool
# ============================================================================

class TestRunToolSuccess:
    """测试 runTool 的成功路径"""

    async def test_async_tool(self, installTool, mockLog):
        installTool("fake", echo)

        result = await runTool("fake.echo", {"text": "ab", "times": 2})

        assert result == OperationResult(status=OperationStatus.DONE, modelText="abab")
        assert result.display == ""
        assert mockLog.await_args.args == ("AFC 执行成功", "fake.echo")

    async def test_sync_tool_runs_in_worker_thread(self, installTool):
        """同步函数放到线程池里跑，不占事件循环所在线程"""
        def whichThread() -> str:
            return str(threading.get_ident())

        installTool("fake", whichThread)

        result = await runTool("fake.whichThread", {})

        assert result.status is OperationStatus.DONE
        assert result.modelText != str(threading.get_ident())

    async def test_sync_tool_receives_parameters(self, installTool):
        def join(left: str, right: str) -> str:
            return left + right

        installTool("fake", join)

        result = await runTool("fake.join", {"left": "a", "right": "b"})

        assert result == OperationResult(status=OperationStatus.DONE, modelText="ab")

    async def test_none_parameters_call_without_arguments(self, installTool):
        async def ping() -> str:
            return "pong"

        installTool("fake", ping)

        result = await runTool("fake.ping", None)

        assert result == OperationResult(status=OperationStatus.DONE, modelText="pong")

    async def test_name_with_outer_whitespace(self, installTool):
        installTool("fake", echo)
        result = await runTool("  fake.echo ", {"text": "a"})
        assert result == OperationResult(status=OperationStatus.DONE, modelText="a")

    async def test_empty_string_result_is_done(self, installTool):
        """空字符串也是文字，记成功"""
        async def blank() -> str:
            return ""

        installTool("fake", blank)

        result = await runTool("fake.blank", {})

        assert result == OperationResult(status=OperationStatus.DONE, modelText="")

    async def test_real_tool(self):
        result = await runTool("calc.calculate", {"expression": "2 + 3"})
        assert result == OperationResult(status=OperationStatus.DONE, modelText="2 + 3 = 5")


class TestRunToolNotRunnable:
    """测试 runTool 在工具运行前就能判定的失败"""

    async def test_disabled_tool_is_not_called(self, installTool, disableTool):
        """校验通过后工具被停用：执行时拒绝，工具函数没有被调用"""
        calls = []

        async def record(text: str) -> str:
            calls.append(text)
            return text

        installTool("fake", record)
        assert validateCall("fake.record", {"text": "a"}) is None

        disableTool("fake")
        result = await runTool("fake.record", {"text": "a"})

        assert result == OperationResult(status=OperationStatus.FAILED, modelText="工具 fake 当前未启用")
        assert calls == []

    async def test_unknown_function(self, installTool):
        installTool("fake", echo)
        result = await runTool("fake.nope", {})
        assert result == OperationResult(status=OperationStatus.FAILED, modelText="工具 fake 没有函数 nope")

    async def test_unknown_tool(self):
        result = await runTool("ghost.run", {})
        assert result == OperationResult(status=OperationStatus.FAILED, modelText="工具 ghost 没有函数 run")


class TestRunToolFailure:
    """测试 runTool 对工具运行中各种失败的处理"""

    @pytest.mark.parametrize("errorType", [ToolError, ToolDependencyError])
    async def test_tool_reported_failure_goes_to_model(self, installTool, mockLog, errorType):
        """工具抛 ToolError / ToolDependencyError：消息原样交给模型"""
        async def refuse() -> str:
            raise errorType("城市名不能为空")

        installTool("fake", refuse)

        result = await runTool("fake.refuse", {})

        assert result == OperationResult(status=OperationStatus.FAILED, modelText="城市名不能为空")
        assert mockLog.await_args.args == ("AFC 工具报告失败", "fake.refuse：城市名不能为空", LogLevel.INFO)

    @pytest.mark.parametrize("errorType", [ToolError, ToolDependencyError])
    async def test_tool_reported_failure_without_message(self, installTool, errorType):
        async def refuse() -> str:
            raise errorType()

        installTool("fake", refuse)

        result = await runTool("fake.refuse", {})

        assert result == OperationResult(status=OperationStatus.FAILED, modelText="工具没有给出失败原因")

    async def test_sync_tool_reported_failure(self, installTool):
        """线程池里抛出的 ToolError 同样交给模型"""
        def refuse() -> str:
            raise ToolError("算不出来")

        installTool("fake", refuse)

        result = await runTool("fake.refuse", {})

        assert result == OperationResult(status=OperationStatus.FAILED, modelText="算不出来")

    async def test_unexpected_exception_only_logged(self, installTool, mockLog):
        """其他异常：模型只看到异常类型，异常内容只进日志"""
        error = ValueError("/srv/secret/path 读取失败")

        async def boom() -> str:
            raise error

        installTool("fake", boom)

        result = await runTool("fake.boom", {})

        assert result == OperationResult(
            status=OperationStatus.FAILED, modelText="工具 fake.boom 内部出错（ValueError）",
        )
        assert mockLog.await_args.args == ("AFC 执行异常", "fake.boom", LogLevel.ERROR)
        assert mockLog.await_args.kwargs == {"exception": error}

    async def test_base_afc_error_is_unexpected(self, installTool):
        """只有 ToolError / ToolDependencyError 的消息交给模型，AFCError 基类不算"""
        async def boom() -> str:
            raise AFCError("内部细节")

        installTool("fake", boom)

        result = await runTool("fake.boom", {})

        assert result == OperationResult(
            status=OperationStatus.FAILED, modelText="工具 fake.boom 内部出错（AFCError）",
        )

    async def test_bad_arguments_reported_as_internal_error(self, installTool):
        """跳过校验直接执行、参数对不上时，TypeError 被兜住记失败"""
        installTool("fake", echo)

        result = await runTool("fake.echo", {"wrong": 1})

        assert result == OperationResult(
            status=OperationStatus.FAILED, modelText="工具 fake.echo 内部出错（TypeError）",
        )

    @pytest.mark.parametrize("value", [None, 42, 1.5, ["a"], {"a": 1}, b"bytes"])
    async def test_non_string_result(self, installTool, mockLog, value):
        async def wrongType():
            return value

        installTool("fake", wrongType)

        result = await runTool("fake.wrongType", {})

        assert result == OperationResult(
            status=OperationStatus.FAILED, modelText="工具 fake.wrongType 的返回值不是文字",
        )
        assert mockLog.await_args.args == (
            "AFC 返回值类型不符",
            f"fake.wrongType 返回 {type(value).__name__}，应为 str",
            LogLevel.ERROR,
        )


class TestRunToolTimeout:
    """测试 runTool 的超时保护"""

    async def test_async_tool_timeout(self, installTool, mockLog, monkeypatch):
        monkeypatch.setattr(executor, "AFC_TOOL_TIMEOUT_SECONDS", _SHORT_TIMEOUT_SECONDS)
        cancelled = []

        async def hang() -> str:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
            return "不会到这里"

        installTool("fake", hang)

        result = await runTool("fake.hang", {})

        assert result == OperationResult(
            status=OperationStatus.FAILED,
            modelText=f"工具 fake.hang 执行超时（{_SHORT_TIMEOUT_SECONDS} 秒）",
        )
        assert cancelled == [True]
        assert mockLog.await_args.args == ("AFC 执行超时", "fake.hang", LogLevel.WARNING)

    async def test_sync_tool_timeout(self, installTool, monkeypatch):
        """同步函数超时：不再等待并记失败，线程由用例自己放行"""
        monkeypatch.setattr(executor, "AFC_TOOL_TIMEOUT_SECONDS", _SHORT_TIMEOUT_SECONDS)
        release = threading.Event()

        def block() -> str:
            release.wait(_START_WAIT_SECONDS)
            return "晚了"

        installTool("fake", block)

        try:
            result = await runTool("fake.block", {})
        finally:
            release.set()

        assert result == OperationResult(
            status=OperationStatus.FAILED,
            modelText=f"工具 fake.block 执行超时（{_SHORT_TIMEOUT_SECONDS} 秒）",
        )


class TestRunToolCancel:
    """测试 runTool 被取消时照常抛出 CancelledError"""

    async def test_cancel_propagates(self, installTool, mockLog):
        started = asyncio.Event()

        async def hang() -> str:
            started.set()
            await asyncio.Event().wait()
            return "不会到这里"

        installTool("fake", hang)

        task = asyncio.create_task(runTool("fake.hang", {}))
        await asyncio.wait_for(started.wait(), _START_WAIT_SECONDS)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()
        assert mockLog.await_args.args == ("AFC 执行被取消", "fake.hang", LogLevel.WARNING)

    async def test_cancelled_error_from_tool_propagates(self, installTool):
        """工具内部抛出的 CancelledError 不被当成普通失败吞掉"""
        async def giveUp() -> str:
            raise asyncio.CancelledError()

        installTool("fake", giveUp)

        with pytest.raises(asyncio.CancelledError):
            await runTool("fake.giveUp", {})


class TestRunToolTruncate:
    """测试 runTool 对过长返回文字的截断"""

    async def test_result_at_limit_is_kept(self, installTool):
        """长度恰好等于上限：原样交回"""
        text = "字" * AFC_TOOL_RESULT_MAX_CHARS

        async def longText() -> str:
            return text

        installTool("fake", longText)

        result = await runTool("fake.longText", {})

        assert result == OperationResult(status=OperationStatus.DONE, modelText=text)

    async def test_result_over_limit_is_truncated(self, installTool):
        """超出一个字符：保留前上限个字符并加截断标注，仍记成功"""
        text = "字" * AFC_TOOL_RESULT_MAX_CHARS + "尾"

        async def longText() -> str:
            return text

        installTool("fake", longText)

        result = await runTool("fake.longText", {})

        assert result.status is OperationStatus.DONE
        assert result.modelText == "字" * AFC_TOOL_RESULT_MAX_CHARS + executor._TRUNCATED_NOTE
        assert "尾" not in result.modelText
        assert "已截断" in result.modelText
