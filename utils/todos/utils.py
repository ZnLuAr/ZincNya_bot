"""
utils/todos/utils.py

Todo 功能的共享工具模块。

提供时间解析、优先级解析、格式化等通用函数，
供 handlers/todos.py（Telegram 端）和 utils/command/todos.py（控制台端）共同使用。

主要接口：

parseTime(text) -> tuple[datetime | None, str]
    从文本开头识别时间表达式，返回 (解析出的时间, 剩余文本)

parsePriority(text) -> tuple[str, str]
    从文本中提取优先级标记（P0-P3/P_），返回 (优先级, 剩余文本)

formatRemindTime(dt) -> str
    将 datetime 格式化为友好显示（今天 HH:MM / 明天 HH:MM / YYYY/MM/DD HH:MM）

常量：
    PRIORITY_EMOJI  Telegram 用 emoji 映射（🔴🟠🟡🟢⚪）
    PRIORITY_TEXT   控制台用文本映射（[P0] [P1] ...）
"""


import re
import sys
from io import StringIO
from datetime import datetime, timedelta


# jionlp 在 import 时会 print 广告，临时压制
_stdout = sys.stdout
sys.stdout = StringIO()
import jionlp as jio
sys.stdout = _stdout
del _stdout




# ============================================================================
# 常量
# ============================================================================

PRIORITY_EMOJI = {
    'P0': '🔴', 'P1': '🟠', 'P2': '🟡', 'P3': '🟢', 'P_': '⚪',
}

PRIORITY_TEXT = {
    'P0': '[P0]', 'P1': '[P1]', 'P2': '[P2]', 'P3': '[P3]', 'P_': '[P_]',
}




# ============================================================================
# 时间解析
# ============================================================================

def parseTime(text: str) -> tuple[datetime | None, str]:
    """
    从文本开头识别时间表达式，返回 (解析出的时间, 剩余文本)。

    支持：
        2h / 30m / 1d           英文简写（需在开头）
        一分钟后 / 两小时后      中文相对时间（jionlp）
        明天 / 后天              自然语言（jionlp）
        明天9:00 / 下午3点       自然语言+时刻（jionlp）

    无法识别时返回 (None, text) 原文不变。
    """
    # 先识别英文简写相对时间：2h, 30m, 1d（仅开头）
    # (?![a-zA-Z]) 确保 "2hours" 不匹配，同时允许后接中文或空格
    relMatch = re.match(r'^(\d+)([mhd])(?![a-zA-Z])', text)
    if relMatch:
        value, unit = relMatch.groups()
        delta = {
            'm': timedelta(minutes=int(value)),
            'h': timedelta(hours=int(value)),
            'd': timedelta(days=int(value)),
        }[unit]
        remaining = text[relMatch.end():].strip()
        return (datetime.now() + delta, remaining)

    # 再使用 jionlp strict 模式识别自然语言时间表达式。
    #
    # strict=True 要求整个输入必须是纯时间表达式，不允许混入非时间内容。
    # 利用这一特性做字符级扫描：
    #   - 外层循环枚举起点（处理 "在明天…" 这类带前缀词的情况）
    #   - 内层循环从文本末尾缩短，找该起点的最长 strict 时间前缀
    #   - 找到最靠前的有效起点即停止
    # 时间表达式剔出后，将前后剩余文本拼接为 remaining。
    base = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    _tp = jio.TimeParser()

    for start in range(len(text)):
        for end in range(len(text), start, -1):
            sub = text[start:end]
            try:
                result = _tp(sub, time_base=base, strict=True)
                parsed = datetime.strptime(result['time'][0], '%Y-%m-%d %H:%M:%S')
                if parsed > datetime.now():
                    remaining = (text[:start] + text[end:]).strip()
                    return (parsed, remaining)
                break   # 解析成功但时间在过去，缩短没有意义
            except Exception:
                continue

    return (None, text)




# ============================================================================
# 优先级解析
# ============================================================================

def parsePriority(text: str) -> tuple[str, str]:
    """
    从文本中提取优先级标记，返回 (优先级, 剩余文本)。
    优先级必须是独立的空格分隔 token。

    示例：
        "P0 买牛奶"    → ("P0", "买牛奶")
        "买牛奶 P0"    → ("P0", "买牛奶")
        "买牛奶"       → ("P_", "买牛奶")
    """
    tokens = text.split()
    for i, token in enumerate(tokens):
        if re.fullmatch(r'P[0-3_]', token):
            remaining = ' '.join(tokens[:i] + tokens[i+1:])
            return (token, remaining)
    return ('P_', text)




# ============================================================================
# 格式化工具
# ============================================================================

def formatRemindTime(dt: datetime) -> str:
    """格式化提醒时间为友好显示"""
    now = datetime.now()
    today = now.date()
    tomorrow = today + timedelta(days=1)

    if dt.date() == today:
        return f"今天 {dt.strftime('%H:%M')}"
    elif dt.date() == tomorrow:
        return f"明天 {dt.strftime('%H:%M')}"
    else:
        return dt.strftime("%Y/%m/%d %H:%M")
