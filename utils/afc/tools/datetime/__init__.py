"""
utils/afc/tools/datetime

日期时间工具 - 为 LLM 提供时间感知能力。

主函数（供 AFC 调用）：
    - getCurrentTime: 获取当前时间
    - calculateDateDiff: 计算两个日期的天数差
    - getFutureDate: 计算未来/过去的日期
"""

from . import core


# ==============================================================================
# 主入口（供 AFC registry 扫描）
# ==============================================================================

async def getCurrentTime(fmt: str = "full") -> str:
    """
    获取当前时间。

    参数：
        fmt: 输出格式
            - "full": 完整时间（日期 + 时间 + 星期 + 时区）
            - "datetime": 日期 + 时间
            - "date": 仅日期
            - "time": 仅时间
            - "weekday": 仅星期
            - "iso": ISO 8601 格式

    返回：
        格式化的当前时间字符串
    """
    return await core.getCurrentTime(fmt)


async def calculateDateDiff(date1: str, date2: str = "today") -> str:
    """
    计算两个日期之间的天数差。

    参数：
        date1: 目标日期（绝对日期或相对日期）
            - 绝对日期：YYYY-MM-DD（如 "2024-12-31"）
            - 相对日期：today/今天, tomorrow/明天, yesterday/昨天, 后天, 前天
        date2: 参考日期（默认 "today"）

    返回：
        描述性字符串（如 "距离 2024-12-31 还有 291 天"）
    """
    return await core.calculateDateDiff(date1, date2)


async def getFutureDate(days: int, fmt: str = "date") -> str:
    """
    计算未来或过去的日期。

    参数：
        days: 天数偏移量（正数=未来，负数=过去，0=今天）
        fmt: 输出格式（同 getCurrentTime）

    返回：
        格式化的日期字符串
    """
    return await core.getFutureDate(days, fmt)
