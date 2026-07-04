"""
tests/utils/afc/test_executor.py

测试 AFC 工具执行器
"""

import json
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from utils.afc.executor import execute, _resolveFuncName, _coerceParameters


# 所有用例统一静音 logSystemEvent，避免落盘副作用
@pytest.fixture(autouse=True)
def _silenceLogger():
    with patch("utils.afc.executor.logSystemEvent", new=AsyncMock()):
        yield


@pytest.mark.asyncio
class TestExecute:
    """测试 execute 主流程"""

    async def test_weather_success(self):
        """天气工具正常执行，结果包裹 FUNCTION_RESULT"""
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
            action = json.dumps({"tool": "weather", "parameters": {"city": "北京", "date": "today"}})
            result = await execute(action)
            assert result.startswith("<FUNCTION_RESULT>")
            assert result.endswith("</FUNCTION_RESULT>")
            assert "北京" in result

    async def test_calc_success(self):
        """计算器工具正常执行"""
        action = json.dumps({"tool": "calc", "parameters": {"expression": "2 + 3"}})
        result = await execute(action)
        assert "<FUNCTION_RESULT>" in result
        assert "5" in result

    async def test_invalid_json(self):
        """非法 JSON 返回 FUNCTION_ERROR"""
        result = await execute("{not valid json")
        assert result.startswith("<FUNCTION_ERROR>")

    async def test_not_object(self):
        """JSON 不是对象时报错"""
        result = await execute("[1, 2, 3]")
        assert "<FUNCTION_ERROR>" in result

    async def test_missing_tool_field(self):
        """缺少 tool 字段报错"""
        result = await execute(json.dumps({"parameters": {}}))
        assert "<FUNCTION_ERROR>" in result
        assert "tool" in result

    async def test_unknown_tool(self):
        """未知工具报错"""
        result = await execute(json.dumps({"tool": "nonexistent", "parameters": {}}))
        assert "<FUNCTION_ERROR>" in result
        assert "nonexistent" in result

    async def test_bad_parameters(self):
        """参数不匹配（多余参数）报错"""
        action = json.dumps({"tool": "calc", "parameters": {"wrongParam": "x"}})
        result = await execute(action)
        assert "<FUNCTION_ERROR>" in result

    async def test_arguments_alias(self):
        """容忍 arguments 键名作为 parameters 别名"""
        action = json.dumps({"tool": "calc", "arguments": {"expression": "1 + 1"}})
        result = await execute(action)
        assert "<FUNCTION_RESULT>" in result
        assert "2" in result

    async def test_tool_exception_wrapped(self):
        """工具内部抛异常时包裹为 FUNCTION_ERROR"""
        async def _boom(expression: str) -> str:
            raise ValueError("炸了")

        with patch("utils.afc.executor.getToolCallable", return_value=_boom):
            action = json.dumps({"tool": "calc", "parameters": {"expression": "x"}})
            result = await execute(action)
        assert "<FUNCTION_ERROR>" in result
        assert "炸了" in result


class TestResolveFuncName:
    """测试函数名解析"""

    def test_explicit_function(self):
        """显式 function 字段优先"""
        assert _resolveFuncName("weather", {"function": "getWeather"}) == "getWeather"

    def test_single_function_auto(self):
        """单函数工具自动取唯一函数"""
        assert _resolveFuncName("calc", {}) == "calculate"

    def test_unknown_tool_returns_empty(self):
        """未知工具无法解析，返回空串"""
        assert _resolveFuncName("nonexistent", {}) == ""


class TestCoerceParameters:
    """测试参数提取"""

    def test_parameters_key(self):
        assert _coerceParameters({"parameters": {"a": 1}}) == {"a": 1}

    def test_arguments_fallback(self):
        assert _coerceParameters({"arguments": {"b": 2}}) == {"b": 2}

    def test_missing_returns_empty(self):
        assert _coerceParameters({}) == {}

    def test_non_dict_returns_empty(self):
        assert _coerceParameters({"parameters": "notdict"}) == {}
