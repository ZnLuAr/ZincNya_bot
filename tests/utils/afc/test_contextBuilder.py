"""
tests/utils/afc/test_contextBuilder.py

测试 AFC 工具说明渲染：buildToolsDescription
"""

from utils.afc.contextBuilder import buildToolsDescription, _TOOL_OPERATION_USAGE




# registry.getEnabledToolsSchema() 输出形态的样例，不依赖具体工具实现
_WEATHER_TOOL = {
    "tool": "weather",
    "functions": [{
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
    }],
}

_DATETIME_TOOL = {
    "tool": "datetime",
    "functions": [
        {
            "name": "getCurrentTime",
            "description": "获取当前时间",
            "parameters": {
                "type": "object",
                "properties": {"fmt": {"type": "string", "description": "输出格式"}},
                "required": [],
            },
        },
        {
            "name": "getFutureDate",
            "description": "计算未来或过去的日期",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "天数偏移量"},
                    "fmt": {"type": "string", "description": "输出格式"},
                },
                "required": ["days"],
            },
        },
    ],
}

_PING_TOOL = {
    "tool": "net",
    "functions": [{
        "name": "ping",
        "description": "测试连通",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }],
}




class TestBuildToolsDescriptionEmpty:
    """没有可用工具函数时不提供说明"""

    def test_empty_list_returns_empty(self):
        assert buildToolsDescription([]) == ""

    def test_tool_without_functions_returns_empty(self):
        """工具存在但没有任何函数：连用法说明也不输出"""
        assert buildToolsDescription([{"tool": "empty", "functions": []}]) == ""

    def test_missing_or_none_functions_returns_empty(self):
        assert buildToolsDescription([{"tool": "empty"}]) == ""
        assert buildToolsDescription([{"tool": "empty", "functions": None}]) == ""

    def test_tool_without_functions_is_skipped(self):
        """没有函数的工具不占行，其余工具照常列出"""
        text = buildToolsDescription([{"tool": "empty", "functions": []}, _PING_TOOL])
        assert "empty" not in text
        assert "- net.ping(): 测试连通" in text


class TestBuildToolsDescription:
    """测试 tool 操作说明的渲染"""

    def test_starts_with_operation_usage(self):
        """说明以 tool 操作的写法开头，工具列表跟在后面"""
        text = buildToolsDescription([_WEATHER_TOOL])
        assert text.startswith(_TOOL_OPERATION_USAGE + "\n- ")
        assert '{"op": "tool", "name": "工具名.函数名", "parameters": {"参数名": "参数值"}}' in text

    def test_full_name_and_signature(self):
        """函数行写完整名称「工具名.函数名」，必填参数无 ?，可选参数带 ?"""
        text = buildToolsDescription([_WEATHER_TOOL])
        assert "- weather.getWeather(city: string, date?: string): 查询指定城市的天气" in text

    def test_param_details_line(self):
        """参数明细行含必填/可选标注与描述"""
        text = buildToolsDescription([_WEATHER_TOOL])
        assert "  参数: city(必填) - 城市名称; date(可选) - 日期（today/tomorrow）" in text

    def test_exact_output(self):
        assert buildToolsDescription([_WEATHER_TOOL]) == "\n".join([
            _TOOL_OPERATION_USAGE,
            "- weather.getWeather(city: string, date?: string): 查询指定城市的天气",
            "  参数: city(必填) - 城市名称; date(可选) - 日期（today/tomorrow）",
        ])

    def test_no_param_function(self):
        """无参数函数不渲染参数明细行"""
        text = buildToolsDescription([_PING_TOOL])
        assert "- net.ping(): 测试连通" in text
        assert "  参数:" not in text

    def test_multiple_functions_in_one_tool(self):
        """一个工具的多个函数各占一行，都带工具名前缀"""
        text = buildToolsDescription([_DATETIME_TOOL])
        assert "- datetime.getCurrentTime(fmt?: string): 获取当前时间" in text
        assert "- datetime.getFutureDate(days: integer, fmt?: string): 计算未来或过去的日期" in text

    def test_multiple_tools_keep_input_order(self):
        text = buildToolsDescription([_WEATHER_TOOL, _DATETIME_TOOL, _PING_TOOL])
        positions = [
            text.index("- weather.getWeather("),
            text.index("- datetime.getCurrentTime("),
            text.index("- datetime.getFutureDate("),
            text.index("- net.ping("),
        ]
        assert positions == sorted(positions)
        assert text.count(_TOOL_OPERATION_USAGE) == 1

    def test_param_without_description(self):
        """参数没有描述时只写名称与必填/可选标注"""
        tool = {
            "tool": "t",
            "functions": [{
                "name": "f",
                "description": "说明",
                "parameters": {
                    "type": "object",
                    "properties": {"a": {"type": "integer", "description": ""}, "b": {"type": "boolean"}},
                    "required": ["a"],
                },
            }],
        }
        text = buildToolsDescription([tool])
        assert "- t.f(a: integer, b?: boolean): 说明" in text
        assert "  参数: a(必填); b(可选)" in text

    def test_param_type_defaults_to_string(self):
        """参数 schema 没写类型或为空时按 string 渲染"""
        tool = {
            "tool": "t",
            "functions": [{
                "name": "f",
                "description": "说明",
                "parameters": {"properties": {"a": {}, "b": None}, "required": ["a", "b"]},
            }],
        }
        text = buildToolsDescription([tool])
        assert "- t.f(a: string, b: string): 说明" in text
        assert "  参数: a(必填); b(必填)" in text

    def test_function_without_parameters_key(self):
        """函数 schema 缺 parameters / description 时不报错"""
        tool = {"tool": "t", "functions": [{"name": "f"}, {"name": "g", "parameters": None}]}
        text = buildToolsDescription([tool])
        assert "- t.f(): " in text
        assert "- t.g(): " in text
        assert "  参数:" not in text
