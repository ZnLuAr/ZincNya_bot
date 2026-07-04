"""
utils/afc/tools/weather/triggers.py

天气工具触发词
"""

KEYWORDS = [
    "天气",
    "气温",
    "温度",
    "下雨",
    "晴",
    "阴",
    "冷不冷",
    "热不热",
]

PATTERNS = [
    r".*天气.*怎么样",
    r"会下雨吗",
    r"明天.*冷",
    r"今天.*热",
]
