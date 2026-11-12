"""
tests/utils/afc/test_registry.py

测试 AFC 工具注册表模块
"""

from unittest.mock import patch, AsyncMock, MagicMock

import pytest

import utils.afc.registry as registry
import utils.afc.toolManager as toolManager
from utils.afc.errors import ToolError
from utils.afc.registry import (
    getAllToolNames,
    getEnabledToolsSchema,
    getFunctionSchema,
    getToolCallable,
)


@pytest.fixture(autouse=True)
def toolSwitches(monkeypatch):
    """启用状态只查内存，不读真实 data/；默认全部启用，返回的函数用来写入开关。"""
    monkeypatch.setattr(toolManager, "_afcToolsConfigCache", {})
    monkeypatch.setattr(toolManager, "_builtinDefaultsCache", {})

    def _setSwitches(switches: dict):
        monkeypatch.setattr(
            toolManager,
            "_afcToolsConfigCache",
            {name: {"enabled": enabled} for name, enabled in switches.items()},
        )

    return _setSwitches


def _enabledToolNames() -> list[str]:
    return [toolSchema["tool"] for toolSchema in getEnabledToolsSchema()]


def _functionsOf(toolName: str) -> list[dict]:
    """从已启用工具的 schema 里取出某个工具的函数 schema 列表；工具不在其中时返回空列表。"""
    for toolSchema in getEnabledToolsSchema():
        if toolSchema["tool"] == toolName:
            return toolSchema["functions"]
    return []


class TestGetEnabledToolsSchema:
    """测试已启用工具的 schema"""

    def test_entry_shape(self):
        """每个工具一项：工具名 + 非空的函数 schema 列表"""
        schemas = getEnabledToolsSchema()
        assert len(schemas) >= 3

        for toolSchema in schemas:
            assert set(toolSchema) == {"tool", "functions"}
            assert toolSchema["functions"]
            for funcSchema in toolSchema["functions"]:
                assert set(funcSchema) == {"name", "description", "parameters"}

    def test_get_weather_schema(self):
        """获取天气工具 schema"""
        functions = _functionsOf("weather")

        # 检查 getWeather 函数 schema
        weatherSchema = next((s for s in functions if s["name"] == "getWeather"), None)
        assert weatherSchema is not None
        assert weatherSchema["description"] == "查询指定城市的天气信息。"

        # 检查参数
        params = weatherSchema["parameters"]
        assert params["properties"]["city"]["type"] == "string"
        assert params["properties"]["date"]["type"] == "string"
        assert "city" in params["required"]
        assert "date" not in params["required"]  # date 有默认值

    def test_external_imports_not_registered(self):
        """工具 __init__ 中导入的外部函数不应被登记为工具函数

        weather/__init__.py 导入了 logSystemEvent（来自 utils.core.logger），
        registry 应按 __module__ 过滤，只登记本工具包内定义的函数。
        """
        names = {s["name"] for s in _functionsOf("weather")}

        # 只应有 getWeather，不应混入导入的辅助函数
        assert names == {"getWeather"}
        assert "logSystemEvent" not in names

        # getToolCallable 也不应返回外部导入的函数
        assert getToolCallable("weather", "logSystemEvent") is None

    def test_private_functions_not_registered(self):
        """下划线开头的函数不登记（web/__init__.py 里的 _formatResult）"""
        names = {s["name"] for s in _functionsOf("web")}
        assert names == {"readURL"}
        assert getToolCallable("web", "_formatResult") is None

    def test_get_calc_schema(self):
        """获取计算器工具 schema"""
        functions = _functionsOf("calc")

        # 检查 calculate 函数 schema
        calcSchema = next((s for s in functions if s["name"] == "calculate"), None)
        assert calcSchema is not None
        assert "description" in calcSchema
        assert "parameters" in calcSchema

        # 检查参数
        params = calcSchema["parameters"]
        assert "expression" in params["properties"]
        assert "expression" in params["required"]

    def test_multi_function_tool(self):
        """一个工具的多个函数都在同一项里"""
        names = {s["name"] for s in _functionsOf("datetime")}
        assert names == {"getCurrentTime", "calculateDateDiff", "getFutureDate"}

    def test_sorted_by_tool_name(self):
        names = _enabledToolNames()
        assert names == sorted(names)
        assert {"calc", "datetime", "weather", "web"} <= set(names)

    def test_disabled_tool_excluded(self, toolSwitches):
        """停用的工具不出现在 schema 里，其余不受影响"""
        toolSwitches({"datetime": False})

        names = _enabledToolNames()
        assert "datetime" not in names
        assert {"calc", "weather"} <= set(names)

    def test_toggle_takes_effect_without_rescan(self, toolSwitches):
        """启停只影响取 schema 的结果，不需要重新扫描"""
        toolSwitches({"calc": False})
        assert "calc" not in _enabledToolNames()

        toolSwitches({"calc": True})
        assert "calc" in _enabledToolNames()

    def test_all_disabled_returns_empty(self, toolSwitches):
        toolSwitches({name: False for name in getAllToolNames()})
        assert getEnabledToolsSchema() == []

    def test_web_disabled_by_builtin_manifest(self, monkeypatch):
        """仓库里的内置 manifest 声明 web 默认停用：没写过开关时不出现在 schema 里"""
        monkeypatch.setattr(toolManager, "_builtinDefaultsCache", None)

        names = _enabledToolNames()
        assert "web" not in names
        assert {"calc", "datetime", "weather"} <= set(names)

    def test_web_enabled_by_switch(self, toolSwitches, monkeypatch):
        """显式启用后 web 出现在 schema 里"""
        toolSwitches({"web": True})
        monkeypatch.setattr(toolManager, "_builtinDefaultsCache", None)

        assert "web" in _enabledToolNames()


class TestGetFunctionSchema:
    """测试单个工具函数 schema 的获取"""

    def test_existing_function(self):
        schema = getFunctionSchema("datetime", "getFutureDate")
        assert schema["name"] == "getFutureDate"
        assert schema["parameters"]["properties"]["days"]["type"] == "integer"
        assert schema["parameters"]["required"] == ["days"]

    def test_nonexistent_tool(self):
        assert getFunctionSchema("nonexistent", "someFunc") is None

    def test_nonexistent_function(self):
        assert getFunctionSchema("weather", "nonexistentFunc") is None

    def test_disabled_tool_still_returned(self, toolSwitches):
        """取单个函数的 schema 不看启用状态"""
        toolSwitches({"calc": False})
        assert getFunctionSchema("calc", "calculate")["name"] == "calculate"


class TestGetToolCallable:
    """测试工具可调用对象获取"""

    def test_get_weather_callable(self):
        """获取天气工具可调用对象"""
        func = getToolCallable("weather", "getWeather")
        assert func is not None
        assert callable(func)

    def test_get_calc_callable(self):
        """获取计算器工具可调用对象"""
        func = getToolCallable("calc", "calculate")
        assert func is not None
        assert callable(func)

    def test_get_nonexistent_tool_callable(self):
        """获取不存在的工具"""
        func = getToolCallable("nonexistent", "someFunc")
        assert func is None

    def test_get_nonexistent_func_callable(self):
        """获取不存在的函数"""
        func = getToolCallable("weather", "nonexistentFunc")
        assert func is None

    def test_disabled_tool_still_returned(self, toolSwitches):
        """取可调用对象不看启用状态；是否能执行由执行器判断"""
        toolSwitches({"calc": False})
        assert callable(getToolCallable("calc", "calculate"))


class TestGetAllToolNames:
    """测试获取所有工具名"""

    def test_get_all_tool_names(self):
        """获取所有工具名"""
        toolNames = getAllToolNames()
        assert isinstance(toolNames, set)
        assert {"weather", "calc", "datetime", "web"} <= toolNames

    def test_includes_disabled_tools(self, toolSwitches):
        """已安装的工具不论是否启用都在其中"""
        toolSwitches({"weather": False, "web": False})
        assert {"weather", "web"} <= getAllToolNames()


class TestGenerateFunctionSchema:
    """测试从函数签名与 docstring 生成 schema"""

    def test_types_required_and_descriptions(self):
        def sample(text: str, count: int, ratio: float, flag: bool, items: list[str],
                   options: dict[str, int], note: str = "") -> str:
            """
            示例函数。

            参数：
                text: 文字
                count: 次数
                note - 备注

            返回：
                结果: 不属于参数说明
            """
            return ""

        schema = registry._generateFunctionSchema(sample, "demo")
        properties = schema["parameters"]["properties"]

        assert schema["name"] == "sample"
        assert schema["description"] == "示例函数。"
        assert {name: prop["type"] for name, prop in properties.items()} == {
            "text": "string",
            "count": "integer",
            "ratio": "number",
            "flag": "boolean",
            "items": "array",
            "options": "object",
            "note": "string",
        }
        assert schema["parameters"]["required"] == ["text", "count", "ratio", "flag", "items", "options"]
        assert properties["text"]["description"] == "文字"
        assert properties["count"]["description"] == "次数"
        assert properties["note"]["description"] == "备注"
        assert properties["flag"]["description"] == ""
        assert "结果" not in properties

    def test_description_falls_back_to_full_name(self):
        """没有 docstring 时描述用「工具名.函数名」"""
        def bare():
            return ""

        schema = registry._generateFunctionSchema(bare, "demo")
        assert schema["description"] == "demo.bare"
        assert schema["parameters"] == {"type": "object", "properties": {}, "required": []}

    def test_unannotated_parameter_defaults_to_string(self):
        def sample(value):
            return ""

        schema = registry._generateFunctionSchema(sample, "demo")
        assert schema["parameters"]["properties"]["value"]["type"] == "string"

    def test_bare_container_annotations(self):
        def sample(items: list, options: dict) -> str:
            return ""

        properties = registry._generateFunctionSchema(sample, "demo")["parameters"]["properties"]
        assert properties["items"]["type"] == "array"
        assert properties["options"]["type"] == "object"


@pytest.mark.asyncio
class TestToolExecution:
    """测试工具实际执行"""

    async def test_weather_execution(self):
        """测试天气工具执行"""
        # Mock API response
        mockResp = AsyncMock()
        mockResp.status = 200
        mockResp.json = AsyncMock(return_value={
            "location": {"name": "北京", "localtime": "2026-06-28 14:30"},
            "current": {
                "temp_c": 28,
                "condition": {"text": "晴"},
                "humidity": 45,
                "wind_kph": 12,
                "feelslike_c": 26
            },
            "forecast": {
                "forecastday": [
                    {
                        "date": "2026-06-28",
                        "day": {
                            "maxtemp_c": 30,
                            "mintemp_c": 22,
                            "condition": {"text": "晴"},
                            "avghumidity": 50,
                            "maxwind_kph": 15
                        }
                    }
                ]
            }
        })
        mockResp.__aenter__ = AsyncMock(return_value=mockResp)
        mockResp.__aexit__ = AsyncMock(return_value=None)

        # Mock session (同步对象，但 get 方法返回 async context manager)
        mockSession = MagicMock()
        mockSession.get.return_value = mockResp
        mockSession.closed = False

        with patch('utils.afc.tools.weather.config.WEATHER_API_KEY', 'test_key_12345'), \
             patch('utils.afc.tools.weather.client._getSession', return_value=mockSession):
            func = getToolCallable("weather", "getWeather")
            result = await func(city="北京", date="today")
            assert "北京" in result

    async def test_calc_execution(self):
        """测试计算器工具执行"""
        func = getToolCallable("calc", "calculate")
        result = await func(expression="2 + 3")
        assert "5" in result

    async def test_calc_complex_expression(self):
        """测试计算器复杂表达式"""
        func = getToolCallable("calc", "calculate")
        result = await func(expression="(10 + 5) * 2")
        assert "30" in result

    async def test_calc_error_handling(self):
        """算不出来时工具抛 ToolError"""
        func = getToolCallable("calc", "calculate")
        with pytest.raises(ToolError, match="表达式格式错误"):
            await func(expression="invalid expression")
