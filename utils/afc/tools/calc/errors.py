"""
计算器工具自定义异常
"""


class CalculationError(ValueError):
    """计算错误基类（用户输入错误）"""
    pass


class UnsupportedOperatorError(CalculationError):
    """不支持的运算符"""
    pass


class UnsupportedNodeError(CalculationError):
    """不支持的 AST 节点"""
    pass


class MathDomainError(CalculationError):
    """数学域错误（如负数开方）"""
    pass
