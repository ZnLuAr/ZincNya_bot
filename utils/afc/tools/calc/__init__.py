"""
utils/afc/tools/calc/__init__.py

计算器工具
"""

from . import calculator


async def calculate(expression: str) -> str:
    """
    计算数学表达式。

    参数：
        expression: 要计算的数学表达式（如 "2 + 3 * 4"、"sqrt(16)"）

    返回：
        计算结果字符串；出错时返回 "错误：..." 说明
    """
    return await calculator.calculate(expression)


__all__ = ["calculate"]
