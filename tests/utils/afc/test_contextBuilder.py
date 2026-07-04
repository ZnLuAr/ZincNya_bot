"""
tests/utils/afc/test_contextBuilder.py

测试 AFC 工具上下文渲染模块
"""

from utils.afc.contextBuilder import buildToolsContext


# 复用 registry 输出形态的样例 schema，避免依赖具体工具实现
_WEATHER_SCHEMA = {
    "name": "getWeather",
    "description": "查询指定城市的天气",
    "parameters": {
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": "城市名称"},
            "date": {"type": "string", "description": "日期（today/tomorrow）"},
        },
        "required": ["city"],
    },
}

_CALC_SCHEMA = {
    "name": "calculate",
    "description": "执行数学计算",
    "parameters": {
        "type": "object",
        "properties": {
            "expression": {"type": "string", "description": "数学表达式"},
        },
        "required": ["expression"],
    },
}


class TestBuildToolsContext:
    """测试工具上下文渲染"""

    def test_empty_schema_returns_empty(self):
        """空 schema 列表返回空串"""
        assert buildToolsContext([]) == ""

    def test_single_tool_contains_tags(self):
        """单工具输出含 AFC_TOOLS 标签与函数签名"""
        text = buildToolsContext([_WEATHER_SCHEMA])
        assert "<AFC_TOOLS>" in text
        assert "</AFC_TOOLS>" in text
        assert "getWeather" in text
        assert "查询指定城市的天气" in text

    def test_required_optional_markers(self):
        """必填参数无 ?，可选参数带 ? 标记"""
        text = buildToolsContext([_WEATHER_SCHEMA])
        # city 必填
        assert "city: string" in text
        # date 可选
        assert "date?: string" in text

    def test_param_details_rendered(self):
        """参数明细行含必填/可选标注与描述"""
        text = buildToolsContext([_WEATHER_SCHEMA])
        assert "city(必填)" in text
        assert "date(可选)" in text
        assert "城市名称" in text

    def test_action_format_instruction(self):
        """输出含 AFC_ACTION 调用格式说明"""
        text = buildToolsContext([_WEATHER_SCHEMA])
        assert "<AFC_ACTION>" in text
        assert "</AFC_ACTION>" in text
        assert '"tool"' in text
        assert '"parameters"' in text

    def test_multiple_tools_all_listed(self):
        """多工具全部列出"""
        text = buildToolsContext([_WEATHER_SCHEMA, _CALC_SCHEMA])
        assert "getWeather" in text
        assert "calculate" in text
        assert text.count("- ") >= 2

    def test_no_param_function(self):
        """无参数函数不渲染参数明细行"""
        schema = {
            "name": "ping",
            "description": "测试连通",
            "parameters": {"type": "object", "properties": {}, "required": []},
        }
        text = buildToolsContext([schema])
        assert "ping()" in text
        assert "  参数:" not in text