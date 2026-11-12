"""
utils/afc/tools/datetime/core.py

日期时间工具核心实现。

提供三个主函数：
    - getCurrentTime: 获取当前时间
    - calculateDateDiff: 计算两个日期的天数差
    - getFutureDate: 计算未来/过去的日期

所有函数成功时返回字符串；参数不合法时抛 ToolError，消息是给模型看的原因。
"""

from datetime import datetime, timedelta, timezone
from typing import Optional
import re

from utils.afc.errors import ToolError

from .config import (
    WEEKDAY_CN,
    FORMAT_TEMPLATES,
    RELATIVE_DATE_MAP,
    ERROR_MESSAGES,
)


# ============================================================================
# 主函数
# ============================================================================

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

    示例：
        >>> await getCurrentTime("full")
        "2024-03-15 14:30:00 星期五 UTC+8"
        >>> await getCurrentTime("date")
        "2024-03-15"
        >>> await getCurrentTime("weekday")
        "星期五"
    """
    now = datetime.now().astimezone()
    return _formatDateTime(now, fmt)


async def calculateDateDiff(date1: str, date2: str = "today") -> str:
    """
    计算两个日期之间的天数差。

    参数：
        date1: 目标日期（绝对日期或相对日期）
            - 绝对日期：YYYY-MM-DD（如 "2024-12-31"）
            - 相对日期：today/今天, tomorrow/明天, yesterday/昨天, 后天, 前天
        date2: 参考日期（默认 "today"）
            - 支持与 date1 相同的格式

    返回：
        描述性字符串（如 "距离 2024-12-31 还有 291 天"）

    示例：
        >>> await calculateDateDiff("2024-12-31", "today")
        "距离 2024-12-31 还有 291 天"
        >>> await calculateDateDiff("昨天", "今天")
        "昨天距今 1 天"
    """
    if not date1 or not date2:
        raise ToolError(ERROR_MESSAGES["empty_input"])

    parsedDate1 = _parseRelativeDate(date1)
    parsedDate2 = _parseRelativeDate(date2)

    if parsedDate1 is None:
        raise ToolError(ERROR_MESSAGES["parse_failed"].format(date_str=date1))
    if parsedDate2 is None:
        raise ToolError(ERROR_MESSAGES["parse_failed"].format(date_str=date2))

    diff = (parsedDate1 - parsedDate2).days

    if diff > 0:
        return f"距离 {date1} 还有 {diff} 天"
    elif diff < 0:
        return f"{date1} 距今 {-diff} 天"
    else:
        return f"{date1} 就是今天"


async def getFutureDate(days: int, fmt: str = "date") -> str:
    """
    计算未来或过去的日期。

    参数：
        days: 天数偏移量
            - 正数：未来（如 3 表示 "3 天后"）
            - 负数：过去（如 -2 表示 "2 天前"）
            - 0：今天
        fmt: 输出格式（同 getCurrentTime）

    返回：
        格式化的日期字符串

    示例：
        >>> await getFutureDate(3, "date")
        "3 天后是 2024-03-18 星期一"
        >>> await getFutureDate(-2, "date")
        "2 天前是 2024-03-13 星期三"
        >>> await getFutureDate(0, "weekday")
        "今天是 星期五"
    """
    base = datetime.now().astimezone()
    try:
        target = base + timedelta(days=days)
    except OverflowError as e:
        raise ToolError(f"天数偏移量超出可表示的日期范围：{days}") from e

    formatted = _formatDateTime(target, fmt)

    if days > 0:
        prefix = f"{days} 天后是 "
    elif days < 0:
        prefix = f"{-days} 天前是 "
    else:
        prefix = "今天是 "

    return prefix + formatted


# ============================================================================
# 辅助函数
# ============================================================================

def _parseRelativeDate(dateStr: str) -> Optional[datetime.date]:
    """
    解析相对日期或绝对日期为 date 对象。

    支持：
        - 相对日期：today/今天, tomorrow/明天, yesterday/昨天, 后天, 前天
        - 绝对日期：YYYY-MM-DD（如 "2024-12-31"）

    返回：
        date 对象，解析失败返回 None
    """
    dateStr = dateStr.strip()

    # 尝试相对日期
    if dateStr in RELATIVE_DATE_MAP:
        offset = RELATIVE_DATE_MAP[dateStr]
        return (datetime.now().date() + timedelta(days=offset))

    # 尝试绝对日期（YYYY-MM-DD）
    if re.match(r'^\d{4}-\d{2}-\d{2}$', dateStr):
        try:
            return datetime.strptime(dateStr, "%Y-%m-%d").date()
        except ValueError:
            return None

    return None


def _formatDateTime(dt: datetime, formatType: str) -> str:
    """
    格式化 datetime 对象为字符串。

    参数：
        dt: datetime 对象（必须是 aware，含时区）
        formatType: 格式类型（full/datetime/date/time/weekday/iso）

    返回：
        格式化字符串；formatType 无效时抛 ToolError
    """
    if formatType not in FORMAT_TEMPLATES:
        raise ToolError(ERROR_MESSAGES["invalid_format"].format(fmt=formatType))

    template = FORMAT_TEMPLATES[formatType]

    replacements = {
        "{date}": dt.strftime("%Y-%m-%d"),
        "{time}": dt.strftime("%H:%M:%S"),
        "{weekday}": WEEKDAY_CN[dt.weekday()],
        "{tz}": _formatTimezone(dt),
        "{iso}": dt.isoformat(),
    }

    result = template
    for placeholder, value in replacements.items():
        result = result.replace(placeholder, value)

    return result


def _formatTimezone(dt: datetime) -> str:
    """
    从 aware datetime 的 UTC 偏移构造 'UTC+8' / 'UTC-5' / 'UTC+5:30'。

    参数：
        dt: aware datetime 对象

    返回：
        时区字符串（如 "UTC+8", "UTC-5", "UTC+5:30"）

    注意：
        不使用 strftime("%Z")，因其返回本地化时区名（如 "中国标准时间"），
        跨环境不稳定。改用 utcoffset() 手动构造。
    """
    offset = dt.utcoffset()
    if offset is None:
        return "UTC"

    totalMinutes = int(offset.total_seconds() // 60)
    sign = "+" if totalMinutes >= 0 else "-"
    hours, minutes = divmod(abs(totalMinutes), 60)

    if minutes == 0:
        return f"UTC{sign}{hours}"
    else:
        return f"UTC{sign}{hours}:{minutes:02d}"