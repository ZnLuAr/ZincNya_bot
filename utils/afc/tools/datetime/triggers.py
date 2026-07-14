"""
utils/afc/tools/datetime/triggers.py

datetime 工具的 AFC 触发配置。
"""

from .core import getCurrentTime, calculateDateDiff, getFutureDate


TOOL_NAME = "datetime"
TOOL_DESCRIPTION = "获取当前时间、计算日期差、计算未来/过去日期"


# AFC 关键词触发列表（L1 召回）
KEYWORDS = [
    # 当前时间查询
    "现在几点",
    "几点了",
    "当前时间",
    "今天几号",
    "今天日期",
    "今天星期几",
    "星期几",
    "what time",
    "current time",

    # 日期差计算
    "距离",
    "还有多久",
    "还有几天",
    "过去了多久",
    "过去几天",

    # 未来/过去日期
    "天后",
    "天前",
    "明天",
    "昨天",
    "后天",
    "前天",
]


# AFC 模式触发列表（L2 召回，正则表达式）
PATTERNS = []


TOOLS = [
    {
        "name": "getCurrentTime",
        "description": "获取当前时间，支持多种格式（完整时间、仅日期、仅时间、星期等）",
        "parameters": {
            "type": "object",
            "properties": {
                "fmt": {
                    "type": "string",
                    "description": "输出格式：full（完整，默认）, datetime（日期+时间）, date（仅日期）, time（仅时间）, weekday（仅星期）, iso（ISO 8601）",
                    "enum": ["full", "datetime", "date", "time", "weekday", "iso"],
                    "default": "full",
                }
            },
            "required": [],
        },
        "function": getCurrentTime,
    },
    {
        "name": "calculateDateDiff",
        "description": "计算两个日期之间的天数差。支持绝对日期（YYYY-MM-DD）和相对日期（today/今天, tomorrow/明天, yesterday/昨天, 后天, 前天）",
        "parameters": {
            "type": "object",
            "properties": {
                "date1": {
                    "type": "string",
                    "description": "目标日期（绝对日期如 2024-12-31，或相对日期如 明天）",
                },
                "date2": {
                    "type": "string",
                    "description": "参考日期（默认 today），格式同 date1",
                    "default": "today",
                }
            },
            "required": ["date1"],
        },
        "function": calculateDateDiff,
    },
    {
        "name": "getFutureDate",
        "description": "计算未来或过去的日期（正数表示未来，负数表示过去）",
        "parameters": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "天数偏移量（正数=未来，负数=过去，0=今天）",
                },
                "fmt": {
                    "type": "string",
                    "description": "输出格式（同 getCurrentTime）",
                    "enum": ["full", "datetime", "date", "time", "weekday", "iso"],
                    "default": "date",
                }
            },
            "required": ["days"],
        },
        "function": getFutureDate,
    },
]