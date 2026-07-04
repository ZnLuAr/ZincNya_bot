"""
日期参数解析。

将用户输入的日期字符串解析为目标日期偏移量。
"""

from datetime import datetime, timedelta
from typing import Optional




# ==============================================================================
# 日期解析
# ==============================================================================

def parseDate(dateStr: str) -> Optional[int]:
    """
    解析日期参数，返回目标日期偏移量。

    支持格式：
    - 相对日期：today/今天/今日 → 0, tomorrow/明天/明日 → 1, 后天 → 2
    - 绝对日期：YYYY-MM-DD（如 2024-03-15）

    参数：
        dateStr: 日期字符串

    返回：
        目标日期偏移量（0=今天, 1=明天, 2=后天）
        None 表示解析失败或超出范围（>2）

    示例：
        >>> parseDate("today")
        0
        >>> parseDate("明天")
        1
        >>> parseDate("2024-03-15")  # 假设今天是 2024-03-14
        1
    """
    dateStr = dateStr.strip().lower()

    # 相对日期
    if dateStr in ('today', '今天', '今日'):
        return 0
    elif dateStr in ('tomorrow', '明天', '明日'):
        return 1
    elif dateStr in ('后天', 'aftertomorrow'):
        return 2

    # 绝对日期（YYYY-MM-DD）
    try:
        targetDate = datetime.strptime(dateStr, '%Y-%m-%d').date()
        today = datetime.now().date()
        delta = (targetDate - today).days

        # 只支持未来 0-2 天
        if 0 <= delta <= 2:
            return delta
        else:
            return None  # 超出范围

    except ValueError:
        return None  # 格式不匹配
