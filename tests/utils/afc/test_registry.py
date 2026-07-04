"""
tests/utils/afc/test_registry.py

测试 AFC 工具注册表模块
"""

from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from utils.afc.registry import getToolsSchema, getToolCallable, getAllToolNames


class TestGetToolsSchema:
    """测试工具 schema 生成"""

    def test_get_weather_schema(self):
        """获取天气工具 schema"""
        schemas = getToolsSchema({"weather"})
        assert len(schemas) >= 1

        # 检查 getWeather 函数 schema
        weather_schema = next((s for s in schemas if s["name"] == "getWeather"), None)
        assert weather_schema is not None
        assert "description" in weather_schema
        assert "parameters" in weather_schema

        # 检查参数
        params = weather_schema["parameters"]
        assert "city" in params["properties"]
        assert "date" in params["properties"]
        assert "city" in params["required"]
        assert "date" not in params["required"]  # date 有默认值

    def test_external_imports_not_registered(self):
        """工具 __init__ 中导入的外部函数不应被登记为工具函数

        weather/__init__.py 导入了 logSystemEvent（来自 utils.core.logger），
        registry 应按 __module__ 过滤，只登记本工具包内定义的函数。
        """
        schemas = getToolsSchema({"weather"})
        names = {s["name"] for s in schemas}

        # 只应有 getWeather，不应混入导入的辅助函数
        assert names == {"getWeather"}
        assert "logSystemEvent" not in names

        # getToolCallable 也不应返回外部导入的函数
        assert getToolCallable("weather", "logSystemEvent") is None

    def test_get_calc_schema(self):
        """获取计算器工具 schema"""
        schemas = getToolsSchema({"calc"})
        assert len(schemas) >= 1

        # 检查 calculate 函数 schema
        calc_schema = next((s for s in schemas if s["name"] == "calculate"), None)
        assert calc_schema is not None
        assert "description" in calc_schema
        assert "parameters" in calc_schema

        # 检查参数
        params = calc_schema["parameters"]
        assert "expression" in params["properties"]
        assert "expression" in params["required"]

    def test_get_multiple_tools_schema(self):
        """获取多个工具 schema"""
        schemas = getToolsSchema({"weather", "calc"})
        assert len(schemas) >= 2

        tool_names = {s["name"] for s in schemas}
        assert "getWeather" in tool_names
        assert "calculate" in tool_names

    def test_get_nonexistent_tool(self):
        """获取不存在的工具"""
        schemas = getToolsSchema({"nonexistent"})
        assert len(schemas) == 0


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


class TestGetAllToolNames:
    """测试获取所有工具名"""

    def test_get_all_tool_names(self):
        """获取所有工具名"""
        tool_names = getAllToolNames()
        assert isinstance(tool_names, set)
        assert "weather" in tool_names
        assert "calc" in tool_names
        assert len(tool_names) >= 2


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
        """测试计算器错误处理"""
        func = getToolCallable("calc", "calculate")
        result = await func(expression="invalid expression")
        assert "错误" in result or "error" in result.lower()