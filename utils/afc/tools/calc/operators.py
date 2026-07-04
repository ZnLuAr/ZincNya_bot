"""
计算器工具运算符和函数白名单。
"""

import math
import operator
import ast


# ==============================================================================
# 运算符白名单
# ==============================================================================

SAFE_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
    ast.FloorDiv: operator.floordiv,
    ast.USub: operator.neg,
    ast.UAdd: lambda x: x,  # 一元加法（如 +5）
}




# ==============================================================================
# 函数白名单
# ==============================================================================

SAFE_FUNCTIONS = {
    # 基础函数
    "abs": abs,
    "round": round,
    "max": max,
    "min": min,

    # 幂与根
    "sqrt": math.sqrt,
    "pow": pow,
    "exp": math.exp,

    # 对数
    "log": math.log,
    "log10": math.log10,
    "log2": math.log2,

    # 三角函数
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "atan2": math.atan2,

    # 双曲函数
    "sinh": math.sinh,
    "cosh": math.cosh,
    "tanh": math.tanh,

    # 取整函数
    "ceil": math.ceil,
    "floor": math.floor,

    # 角度转换
    "deg": math.radians,  # deg(90) → π/2
    "rad": lambda x: x,   # rad(1.57) → 1.57 (恒等)
}




# ==============================================================================
# 常量白名单
# ==============================================================================

SAFE_CONSTANTS = {
    "pi": math.pi,
    "e": math.e,
}
