"""
utils/afc/tools/datetime/config.py

datetime 工具的常量配置。
"""

# 中文星期映射（周一=0, 周日=6）
WEEKDAY_CN = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


# 格式化模板（占位符由 _formatDateTime 替换）
FORMAT_TEMPLATES = {
    "full": "{date} {time} {weekday} {tz}",
    "datetime": "{date} {time}",
    "date": "{date}",
    "time": "{time}",
    "weekday": "{weekday}",
    "iso": "{iso}",
}


# 相对日期关键词映射（支持中英文）
RELATIVE_DATE_MAP = {
    "today": 0,
    "今天": 0,
    "tomorrow": 1,
    "明天": 1,
    "yesterday": -1,
    "昨天": -1,
    "后天": 2,
    "前天": -2,
}


# 错误消息模板
ERROR_MESSAGES = {
    "invalid_format": "错误：不支持的格式：{fmt}",
    "parse_failed": "错误：无法解析日期：{date_str}",
    "invalid_date_format": "错误：日期格式错误，请使用 YYYY-MM-DD",
    "empty_input": "错误：日期不能为空",
}