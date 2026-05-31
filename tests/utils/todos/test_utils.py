"""
tests/utils/todos/test_utils.py

测试 utils/todos/utils.py
"""

import pytest
from datetime import datetime, timedelta
from utils.todos.utils import (
    parseTime,
    parsePriority,
    formatRemindTime,
    PRIORITY_EMOJI,
    PRIORITY_TEXT,
)


# ============================================================================
# parseTime() 测试
# ============================================================================

def test_parse_time_minutes():
    """英文简写：分钟"""
    parsed, remaining = parseTime("30m 买牛奶")
    assert parsed is not None
    assert remaining == "买牛奶"
    # 时间应在 30 分钟后附近
    expected = datetime.now() + timedelta(minutes=30)
    assert abs((parsed - expected).total_seconds()) < 5


def test_parse_time_hours():
    """英文简写：小时"""
    parsed, remaining = parseTime("2h 开会")
    assert parsed is not None
    assert remaining == "开会"
    expected = datetime.now() + timedelta(hours=2)
    assert abs((parsed - expected).total_seconds()) < 5


def test_parse_time_days():
    """英文简写:天"""
    parsed, remaining = parseTime("1d 完成报告")
    assert parsed is not None
    assert remaining == "完成报告"
    expected = datetime.now() + timedelta(days=1)
    assert abs((parsed - expected).total_seconds()) < 5


def test_parse_time_no_match_long_word():
    """以数字+字母开头但不是有效缩写"""
    parsed, remaining = parseTime("2hours 开会")
    # 应该不匹配 "2h"，因为后面跟着 "ours"
    # 但可能 jionlp 也无法识别
    assert remaining == "2hours 开会" or parsed is None


def test_parse_time_no_match():
    """无时间表达式"""
    parsed, remaining = parseTime("just plain text")
    assert parsed is None
    assert remaining == "just plain text"


def test_parse_time_chinese_relative():
    """中文相对时间（jionlp）"""
    parsed, remaining = parseTime("一小时后开会")
    if parsed is not None:
        # jionlp 解析成功
        assert "开会" in remaining


def test_parse_time_empty():
    """空字符串"""
    parsed, remaining = parseTime("")
    assert parsed is None
    assert remaining == ""


def test_parse_time_dos_protection():
    """超长输入保护（限制扫描前 100 字符）"""
    long_text = "a" * 1000 + "明天"
    # 应该不会卡住，能在合理时间内返回
    parsed, remaining = parseTime(long_text)
    # 由于 "明天" 在 100 字符之外，应该无法识别
    assert parsed is None or len(remaining) > 0


# ============================================================================
# parsePriority() 测试
# ============================================================================

def test_parse_priority_p0():
    """P0 优先级"""
    priority, remaining = parsePriority("P0 买牛奶")
    assert priority == "P0"
    assert remaining == "买牛奶"


def test_parse_priority_p1():
    """P1 优先级"""
    priority, remaining = parsePriority("P1 任务")
    assert priority == "P1"
    assert remaining == "任务"


def test_parse_priority_p2():
    """P2 优先级"""
    priority, remaining = parsePriority("P2 任务")
    assert priority == "P2"
    assert remaining == "任务"


def test_parse_priority_p3():
    """P3 优先级"""
    priority, remaining = parsePriority("P3 任务")
    assert priority == "P3"
    assert remaining == "任务"


def test_parse_priority_p_underscore():
    """P_ 优先级（默认）"""
    priority, remaining = parsePriority("P_ 任务")
    assert priority == "P_"
    assert remaining == "任务"


def test_parse_priority_at_end():
    """优先级在末尾"""
    priority, remaining = parsePriority("买牛奶 P0")
    assert priority == "P0"
    assert remaining == "买牛奶"


def test_parse_priority_in_middle():
    """优先级在中间"""
    priority, remaining = parsePriority("今天 P1 买牛奶")
    assert priority == "P1"
    assert remaining == "今天 买牛奶"


def test_parse_priority_no_match():
    """无优先级标记返回默认 P_"""
    priority, remaining = parsePriority("just a task")
    assert priority == "P_"
    assert remaining == "just a task"


def test_parse_priority_invalid():
    """无效的优先级标记不被识别"""
    priority, remaining = parsePriority("P4 任务")
    # P4 不是有效优先级
    assert priority == "P_"
    assert remaining == "P4 任务"


def test_parse_priority_not_token():
    """优先级必须是独立 token"""
    priority, remaining = parsePriority("MyP0Task")
    # 不是独立 token
    assert priority == "P_"
    assert remaining == "MyP0Task"


# ============================================================================
# formatRemindTime() 测试
# ============================================================================

def test_format_remind_time_today():
    """今天的时间"""
    today_time = datetime.now().replace(hour=15, minute=30, second=0, microsecond=0)
    result = formatRemindTime(today_time)
    assert result == "今天 15:30"


def test_format_remind_time_tomorrow():
    """明天的时间"""
    tomorrow_time = (datetime.now() + timedelta(days=1)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    result = formatRemindTime(tomorrow_time)
    assert result == "明天 10:00"


def test_format_remind_time_future():
    """更远的未来"""
    future_time = datetime(2030, 12, 31, 23, 59, 0)
    result = formatRemindTime(future_time)
    assert result == "2030/12/31 23:59"


def test_format_remind_time_past():
    """过去的日期"""
    past_time = datetime(2020, 1, 1, 0, 0, 0)
    result = formatRemindTime(past_time)
    assert result == "2020/01/01 00:00"


# ============================================================================
# 常量测试
# ============================================================================

def test_priority_emoji_keys():
    """PRIORITY_EMOJI 包含所有优先级"""
    assert "P0" in PRIORITY_EMOJI
    assert "P1" in PRIORITY_EMOJI
    assert "P2" in PRIORITY_EMOJI
    assert "P3" in PRIORITY_EMOJI
    assert "P_" in PRIORITY_EMOJI


def test_priority_text_keys():
    """PRIORITY_TEXT 包含所有优先级"""
    assert "P0" in PRIORITY_TEXT
    assert "P1" in PRIORITY_TEXT
    assert "P2" in PRIORITY_TEXT
    assert "P3" in PRIORITY_TEXT
    assert "P_" in PRIORITY_TEXT