"""
tests/utils/afc/tools/test_datetime.py

测试 datetime 工具的核心功能。

覆盖：
    - getCurrentTime: 各种格式输出、不支持的格式
    - calculateDateDiff: 相对/绝对日期、未来/过去/今天、空日期与无法解析的日期
    - getFutureDate: 正数/负数/零偏移、格式参数、偏移量超出范围
    - _parseRelativeDate: 中英文相对日期、绝对日期
    - _formatTimezone: UTC+8 / UTC-5 / UTC+5:30

参数不合法时工具抛 ToolError，消息是给模型看的原因。
"""

import pytest
from datetime import datetime, timezone, timedelta
from freezegun import freeze_time

from utils.afc.errors import ToolError
from utils.afc.tools.datetime import getCurrentTime, calculateDateDiff, getFutureDate
from utils.afc.tools.datetime.core import _formatTimezone


# ============================================================================
# TestGetCurrentTime - 当前时间获取（7 个）
# ============================================================================

@freeze_time("2024-03-15 14:30:00")
class TestGetCurrentTime:
    """测试 getCurrentTime 的各种格式输出"""

    @pytest.mark.asyncio
    async def test_full_format(self):
        """完整格式：日期 + 时间 + 星期 + 时区"""
        result = await getCurrentTime("full")
        assert "2024-03-15" in result
        assert "14:30:00" in result
        assert "星期五" in result
        # 不断言 "UTC+8"——测试环境系统时区不确定

    @pytest.mark.asyncio
    async def test_datetime_format(self):
        """日期 + 时间"""
        result = await getCurrentTime("datetime")
        assert result == "2024-03-15 14:30:00"

    @pytest.mark.asyncio
    async def test_invalid_format(self):
        """不支持的格式：抛 ToolError"""
        with pytest.raises(ToolError, match="不支持的格式：yearly"):
            await getCurrentTime("yearly")

    @pytest.mark.asyncio
    async def test_date_format(self):
        """仅日期"""
        result = await getCurrentTime("date")
        assert result == "2024-03-15"

    @pytest.mark.asyncio
    async def test_time_format(self):
        """仅时间"""
        result = await getCurrentTime("time")
        assert result == "14:30:00"

    @pytest.mark.asyncio
    async def test_weekday_format(self):
        """仅星期"""
        result = await getCurrentTime("weekday")
        assert result == "星期五"

    @pytest.mark.asyncio
    async def test_iso_format(self):
        """ISO 8601 格式"""
        result = await getCurrentTime("iso")
        assert "2024-03-15T14:30:00" in result


# ============================================================================
# TestCalculateDateDiff - 日期差计算（8 个）
# ============================================================================

@freeze_time("2024-03-15")
class TestCalculateDateDiff:
    """测试 calculateDateDiff 的相对/绝对日期、未来/过去/今天"""

    @pytest.mark.asyncio
    async def test_future_date(self):
        """未来日期：距离 2024-12-31 还有 N 天"""
        result = await calculateDateDiff("2024-12-31", "today")
        assert "距离 2024-12-31 还有" in result
        assert "天" in result

    @pytest.mark.asyncio
    async def test_past_date(self):
        """过去日期：2024-01-01 距今 N 天"""
        result = await calculateDateDiff("2024-01-01", "today")
        assert "2024-01-01 距今" in result
        assert "天" in result

    @pytest.mark.asyncio
    async def test_same_date(self):
        """同一天：今天就是今天"""
        result = await calculateDateDiff("today", "今天")
        assert "就是今天" in result

    @pytest.mark.asyncio
    async def test_relative_tomorrow(self):
        """相对日期：明天 vs 今天"""
        result = await calculateDateDiff("明天", "today")
        assert "距离 明天 还有 1 天" in result

    @pytest.mark.asyncio
    async def test_absolute_date(self):
        """绝对日期 YYYY-MM-DD 解析"""
        result = await calculateDateDiff("2024-03-20", "2024-03-15")
        assert "距离 2024-03-20 还有 5 天" in result

    @pytest.mark.asyncio
    async def test_invalid_date(self):
        """无效日期：抛 ToolError"""
        with pytest.raises(ToolError, match="无法解析日期：invalid_date"):
            await calculateDateDiff("invalid_date")

    @pytest.mark.asyncio
    async def test_invalid_reference_date(self):
        """参考日期无法解析：错误消息指向参考日期"""
        with pytest.raises(ToolError, match="无法解析日期：某一天"):
            await calculateDateDiff("today", "某一天")

    @pytest.mark.asyncio
    async def test_empty_reference_date(self):
        """参考日期为空：抛 ToolError"""
        with pytest.raises(ToolError, match="日期不能为空"):
            await calculateDateDiff("today", "")


# ============================================================================
# TestGetFutureDate - 未来/过去日期（8 个）
# ============================================================================

@freeze_time("2024-03-15")
class TestGetFutureDate:
    """测试 getFutureDate 的正数/负数/零偏移"""

    @pytest.mark.asyncio
    async def test_positive_days(self):
        """正数偏移：3 天后"""
        result = await getFutureDate(3, "date")
        assert "3 天后是" in result
        assert "2024-03-18" in result

    @pytest.mark.asyncio
    async def test_negative_days(self):
        """负数偏移：2 天前"""
        result = await getFutureDate(-2, "date")
        assert "2 天前是" in result
        assert "2024-03-13" in result

    @pytest.mark.asyncio
    async def test_zero_days(self):
        """零偏移：今天"""
        result = await getFutureDate(0, "date")
        assert "今天是" in result
        assert "2024-03-15" in result

    @pytest.mark.asyncio
    async def test_large_offset(self):
        """大偏移量：365 天后"""
        result = await getFutureDate(365, "date")
        assert "365 天后是" in result
        assert "2025-03-15" in result

    @pytest.mark.asyncio
    async def test_fmt_parameter(self):
        """fmt 参数：返回星期"""
        result = await getFutureDate(3, "weekday")
        assert "3 天后是" in result
        assert "星期一" in result  # 2024-03-18 是周一

    @pytest.mark.asyncio
    async def test_invalid_format(self):
        """不支持的格式：抛 ToolError"""
        with pytest.raises(ToolError, match="不支持的格式：yearly"):
            await getFutureDate(3, "yearly")

    @pytest.mark.asyncio
    async def test_offset_beyond_timedelta_range(self):
        """偏移量超过 timedelta 能表示的天数：抛 ToolError"""
        with pytest.raises(ToolError, match="天数偏移量超出可表示的日期范围：1000000000"):
            await getFutureDate(1000000000, "date")

    @pytest.mark.asyncio
    async def test_offset_beyond_date_range(self):
        """算出的日期超出公元 9999 年：抛 ToolError"""
        with pytest.raises(ToolError, match="天数偏移量超出可表示的日期范围：5000000"):
            await getFutureDate(5000000, "date")


# ============================================================================
# TestDateParsing - 日期解析（4 个）
# ============================================================================

@freeze_time("2024-03-15")
class TestDateParsing:
    """测试 _parseRelativeDate 的中英文相对日期、绝对日期"""

    @pytest.mark.asyncio
    async def test_parse_today(self):
        """相对日期：today / 今天"""
        result1 = await calculateDateDiff("today", "今天")
        result2 = await calculateDateDiff("今天", "today")
        assert "就是今天" in result1
        assert "就是今天" in result2

    @pytest.mark.asyncio
    async def test_parse_absolute(self):
        """绝对日期：YYYY-MM-DD"""
        result = await calculateDateDiff("2024-03-20", "2024-03-15")
        assert "距离 2024-03-20 还有 5 天" in result

    @pytest.mark.asyncio
    async def test_parse_chinese_relative(self):
        """中文相对日期：明天 / 昨天 / 后天 / 前天"""
        result1 = await calculateDateDiff("明天", "今天")
        result2 = await calculateDateDiff("昨天", "今天")
        result3 = await calculateDateDiff("后天", "今天")
        result4 = await calculateDateDiff("前天", "今天")
        assert "还有 1 天" in result1
        assert "距今 1 天" in result2
        assert "还有 2 天" in result3
        assert "距今 2 天" in result4

    @pytest.mark.asyncio
    async def test_parse_invalid(self):
        """无效日期：格式错误"""
        with pytest.raises(ToolError, match="无法解析日期：2024-99-99"):
            await calculateDateDiff("2024-99-99")
        with pytest.raises(ToolError, match="无法解析日期：not_a_date"):
            await calculateDateDiff("not_a_date")
        with pytest.raises(ToolError, match="日期不能为空"):
            await calculateDateDiff("")


# ============================================================================
# TestFormatting - 格式化输出（2 个）
# ============================================================================

@freeze_time("2024-03-15 14:30:00")
class TestFormatting:
    """测试格式化相关（星期、ISO 8601）"""

    @pytest.mark.asyncio
    async def test_weekday_chinese(self):
        """中文星期输出"""
        result = await getCurrentTime("weekday")
        assert result == "星期五"  # 2024-03-15 是周五

    @pytest.mark.asyncio
    async def test_iso8601_format(self):
        """ISO 8601 格式"""
        result = await getCurrentTime("iso")
        assert "2024-03-15T14:30:00" in result
        assert "+" in result or "Z" in result  # 时区偏移或 UTC


# ============================================================================
# TestFormatTimezone - 时区格式化（4 个）
# ============================================================================

class TestFormatTimezone:
    """测试 _formatTimezone 的时区字符串构造（不依赖系统时区）"""

    def test_utc_plus_8(self):
        """UTC+8（中国标准时间）"""
        dt = datetime(2024, 3, 15, 14, 30, tzinfo=timezone(timedelta(hours=8)))
        assert _formatTimezone(dt) == "UTC+8"

    def test_utc_minus_5(self):
        """UTC-5（美国东部时间）"""
        dt = datetime(2024, 3, 15, tzinfo=timezone(timedelta(hours=-5)))
        assert _formatTimezone(dt) == "UTC-5"

    def test_half_hour_offset(self):
        """UTC+5:30（印度标准时间）"""
        dt = datetime(2024, 3, 15, tzinfo=timezone(timedelta(hours=5, minutes=30)))
        assert _formatTimezone(dt) == "UTC+5:30"

    def test_naive_datetime(self):
        """不带时区的 datetime 没有偏移可取，写成 UTC"""
        assert _formatTimezone(datetime(2024, 3, 15)) == "UTC"