"""
计算器核心实现。

使用 AST 安全解析和计算数学表达式。
"""

import ast
import re
from typing import Any

from .config import (
    RESULT_PRECISION,
    ERROR_MESSAGES,
    MAX_POWER_EXPONENT,
    MAX_RESULT_MAGNITUDE,
)
from .errors import (
    CalculationError,
    UnsupportedOperatorError,
    UnsupportedNodeError,
    MathDomainError,
)
from .operators import SAFE_OPERATORS, SAFE_FUNCTIONS, SAFE_CONSTANTS




# ==============================================================================
# 预处理
# ==============================================================================

def _preprocessExpression(expression: str) -> str:
    """
    预处理表达式，支持特殊格式。

    - `20%` → `(20/100)`
    - `90°` → `rad(90)`

    参数：
        expression: 原始表达式

    返回：
        预处理后的表达式
    """
    # 处理百分比（修正科学计数法支持，避免与取模运算符 % 冲突）
    # 只匹配数字后紧跟百分号的情况（如 "20%"），不匹配运算符 "10 % 3"
    expression = re.sub(
        r'(\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)%(?!\s*\d)',
        r'(\1/100)',
        expression
    )

    # 处理角度符号
    expression = re.sub(
        r'(\d+(?:\.\d+)?)\s*°',
        r'rad(\1)',
        expression
    )

    return expression


# ==============================================================================
# AST 求值
# ==============================================================================

def _evalNode(node: ast.AST, constants: dict[str, Any]) -> Any:
    """
    递归求值 AST 节点。

    参数：
        node: AST 节点
        constants: 常量字典（支持 pi、e 等）

    返回：
        计算结果

    异常：
        UnsupportedOperatorError: 不支持的运算符
        UnsupportedNodeError: 不支持的节点类型
        MathDomainError: 数学域错误
    """
    # 常量
    if isinstance(node, ast.Constant):
        return node.value

    # Python 3.7 兼容（虽然生产环境是 3.11，但保持兼容性）
    if isinstance(node, ast.Num):
        return node.n

    # 变量（常量查找）
    if isinstance(node, ast.Name):
        name = node.id
        if name in constants:
            return constants[name]
        raise UnsupportedNodeError(f"未定义的变量：{name}")

    # 二元运算
    if isinstance(node, ast.BinOp):
        left = _evalNode(node.left, constants)
        right = _evalNode(node.right, constants)
        op = SAFE_OPERATORS.get(type(node.op))
        if op is None:
            raise UnsupportedOperatorError(
                ERROR_MESSAGES['unsupported_operator'].format(op=type(node.op).__name__)
            )

        # 幂运算前置防护：指数过大直接拒绝，避免执行昂贵的大整数幂运算
        if isinstance(node.op, ast.Pow) and isinstance(right, (int, float)):
            if abs(right) > MAX_POWER_EXPONENT:
                raise MathDomainError(
                    ERROR_MESSAGES['power_too_large'].format(limit=MAX_POWER_EXPONENT)
                )

        try:
            return op(left, right)
        except ZeroDivisionError:
            raise MathDomainError(ERROR_MESSAGES['division_by_zero'])
        except (ValueError, ArithmeticError) as e:
            raise MathDomainError(str(e))

    # 一元运算
    if isinstance(node, ast.UnaryOp):
        operand = _evalNode(node.operand, constants)
        op = SAFE_OPERATORS.get(type(node.op))
        if op is None:
            raise UnsupportedOperatorError(
                ERROR_MESSAGES['unsupported_operator'].format(op=type(node.op).__name__)
            )
        return op(operand)

    # 函数调用
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise UnsupportedNodeError("不支持复杂函数调用")

        funcName = node.func.id
        func = SAFE_FUNCTIONS.get(funcName)
        if func is None:
            raise UnsupportedNodeError(f"不支持的函数：{funcName}")

        args = [_evalNode(arg, constants) for arg in node.args]
        try:
            return func(*args)
        except (ValueError, TypeError, ArithmeticError) as e:
            raise MathDomainError(f"{funcName}() 错误：{e}")

    # 不支持的节点
    raise UnsupportedNodeError(
        ERROR_MESSAGES['unsupported_node'].format(node=type(node).__name__)
    )


# ==============================================================================
# 结果格式化
# ==============================================================================

def _formatResult(result: float) -> str:
    """
    格式化计算结果。

    - 整数去掉小数点：`5.0` → `"5"`
    - 小数保留有效位：`3.14000` → `"3.14"`
    - 科学计数法：`1e20` → `"1e+20"`

    参数：
        result: 计算结果（浮点数）

    返回：
        格式化后的字符串（近似值）
    """
    # 溢出检查
    if abs(result) > MAX_RESULT_MAGNITUDE:
        raise OverflowError(ERROR_MESSAGES['overflow'])

    # 保留精度
    rounded = round(result, RESULT_PRECISION)

    # 整数简化
    if rounded == int(rounded):
        return str(int(rounded))

    # 去除尾部零
    formatted = f"{rounded:.{RESULT_PRECISION}f}".rstrip('0').rstrip('.')

    return formatted




# ==============================================================================
# 主入口
# ==============================================================================

async def calculate(expression: str) -> str:
    """
    计算数学表达式。

    支持的运算：
    - 基本运算：+, -, *, /, **, %, //
    - 函数：sqrt, sin, cos, tan, log, exp, abs, round, max, min 等
    - 常量：pi, e
    - 特殊格式：20%, 90°

    参数：
        expression: 数学表达式字符串（如 "2 + 3" 或 "sqrt(16)"）

    返回：
        格式化的计算结果字符串（如 "2 + 3 = 5"）
        错误时返回 "计算错误：<错误消息>"

    示例：
        >>> await calculate("2 + 3")
        "2 + 3 = 5"
        >>> await calculate("sqrt(16)")
        "sqrt(16) = 4"
        >>> await calculate("sin(rad(90))")
        "sin(rad(90)) = 1"
    """
    try:
        # 输入校验
        expression = expression.strip()
        if not expression:
            return f"计算错误：{ERROR_MESSAGES['invalid_expression']}"

        # 预处理
        processedExpr = _preprocessExpression(expression)

        # 解析 AST
        try:
            tree = ast.parse(processedExpr, mode='eval')
        except SyntaxError:
            return f"计算错误：{ERROR_MESSAGES['invalid_expression']}"

        # 求值
        result = _evalNode(tree.body, SAFE_CONSTANTS)

        # 格式化
        formattedResult = _formatResult(result)

        return f"{expression} = {formattedResult}"

    except CalculationError as e:
        await logSystemEvent(
            "计算错误（用户输入）",
            f"表达式：{expression}，错误：{e}",
            LogLevel.INFO,
        )
        return f"错误：{e}"
    except OverflowError as e:
        await logSystemEvent(
            "计算错误（溢出）",
            f"表达式：{expression}，错误：{e}",
            LogLevel.INFO,
        )
        return f"错误：{e}"
    except Exception as e:
        await logSystemEvent(
            "计算错误（预期外）",
            f"表达式：{expression}，错误：{e}",
            LogLevel.ERROR,
            exception=e,
        )
        return f"错误：{ERROR_MESSAGES['invalid_expression']}"
    