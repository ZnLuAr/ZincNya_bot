"""
tests/utils/afc/tools/test_calc.py

计算器工具测试套件
"""

from unittest.mock import AsyncMock, patch

import pytest

from utils.core.logger import LogLevel
from utils.afc.errors import ToolError
from utils.afc.tools.calc import calculate
from utils.afc.tools.calc.config import MAX_POWER_EXPONENT


@pytest.fixture(autouse=True)
def mockLog():
    """算不出来时计算器会写系统日志；用例里换成 mock，不写真实日志文件。"""
    with patch("utils.afc.tools.calc.calculator.logSystemEvent", new=AsyncMock()) as mock:
        yield mock


async def _failureReason(expression: str) -> str:
    """表达式算不出来时取 ToolError 的消息，也就是模型看到的原因。"""
    with pytest.raises(ToolError) as excInfo:
        await calculate(expression)
    return str(excInfo.value)


# ==============================================================================
# 1. 基本运算
# ==============================================================================

@pytest.mark.asyncio
async def test_basic_addition():
    assert await calculate("2 + 3") == "2 + 3 = 5"


@pytest.mark.asyncio
async def test_basic_subtraction():
    assert await calculate("10 - 7") == "10 - 7 = 3"


@pytest.mark.asyncio
async def test_basic_multiplication():
    assert await calculate("4 * 5") == "4 * 5 = 20"


@pytest.mark.asyncio
async def test_basic_division():
    assert await calculate("20 / 4") == "20 / 4 = 5"


@pytest.mark.asyncio
async def test_basic_power():
    assert await calculate("2 ** 3") == "2 ** 3 = 8"


@pytest.mark.asyncio
async def test_complex_expression():
    result = await calculate("2 + 3 * 4")
    assert result == "2 + 3 * 4 = 14"


@pytest.mark.asyncio
async def test_parentheses():
    result = await calculate("(2 + 3) * 4")
    assert result == "(2 + 3) * 4 = 20"


@pytest.mark.asyncio
async def test_negative_numbers():
    assert await calculate("-5 + 3") == "-5 + 3 = -2"


# ==============================================================================
# 2. 新增运算符
# ==============================================================================

@pytest.mark.asyncio
async def test_modulo():
    assert await calculate("10 % 3") == "10 % 3 = 1"


@pytest.mark.asyncio
async def test_floor_division():
    assert await calculate("10 // 3") == "10 // 3 = 3"


# ==============================================================================
# 3. 新增函数
# ==============================================================================

@pytest.mark.asyncio
async def test_sqrt():
    assert await calculate("sqrt(16)") == "sqrt(16) = 4"


@pytest.mark.asyncio
async def test_abs():
    assert await calculate("abs(-10)") == "abs(-10) = 10"


@pytest.mark.asyncio
async def test_round():
    result = await calculate("round(3.14159, 2)")
    assert result == "round(3.14159, 2) = 3.14"


@pytest.mark.asyncio
async def test_max():
    assert await calculate("max(1, 5, 3)") == "max(1, 5, 3) = 5"


@pytest.mark.asyncio
async def test_min():
    assert await calculate("min(1, 5, 3)") == "min(1, 5, 3) = 1"


@pytest.mark.asyncio
async def test_pow():
    assert await calculate("pow(2, 3)") == "pow(2, 3) = 8"


@pytest.mark.asyncio
async def test_exp():
    result = await calculate("exp(0)")
    assert result == "exp(0) = 1"


@pytest.mark.asyncio
async def test_log():
    result = await calculate("log(2.718281828)")
    # log(e) ≈ 1
    assert "log(2.718281828) = 1" in result or "log(2.718281828) = 0.99999" in result


@pytest.mark.asyncio
async def test_log10():
    assert await calculate("log10(100)") == "log10(100) = 2"


@pytest.mark.asyncio
async def test_log2():
    assert await calculate("log2(8)") == "log2(8) = 3"


@pytest.mark.asyncio
async def test_sin():
    result = await calculate("sin(0)")
    assert result == "sin(0) = 0"


@pytest.mark.asyncio
async def test_cos():
    result = await calculate("cos(0)")
    assert result == "cos(0) = 1"


@pytest.mark.asyncio
async def test_tan():
    result = await calculate("tan(0)")
    assert result == "tan(0) = 0"


@pytest.mark.asyncio
async def test_ceil():
    result = await calculate("ceil(3.2)")
    assert result == "ceil(3.2) = 4"


@pytest.mark.asyncio
async def test_floor():
    result = await calculate("floor(3.8)")
    assert result == "floor(3.8) = 3"


# ==============================================================================
# 4. 常量
# ==============================================================================

@pytest.mark.asyncio
async def test_constant_pi():
    result = await calculate("pi")
    assert "pi = 3.14159" in result


@pytest.mark.asyncio
async def test_constant_e():
    result = await calculate("e")
    assert "e = 2.71828" in result


@pytest.mark.asyncio
async def test_constant_in_expression():
    result = await calculate("2 * pi")
    assert "2 * pi = 6.28318" in result


# ==============================================================================
# 5. 新增格式
# ==============================================================================

@pytest.mark.asyncio
async def test_percentage_simple():
    result = await calculate("20%")
    assert result == "20% = 0.2"


@pytest.mark.asyncio
async def test_percentage_in_expression():
    result = await calculate("50% * 100")
    assert result == "50% * 100 = 50"


@pytest.mark.asyncio
async def test_degree_symbol():
    result = await calculate("sin(90°)")
    # sin(deg(90)) = sin(π/2) = 1
    assert "sin(90°) = 1" in result or "sin(90°) = 0.99999" in result


@pytest.mark.asyncio
async def test_deg_function():
    result = await calculate("sin(deg(90))")
    assert "sin(deg(90)) = 1" in result or "sin(deg(90)) = 0.99999" in result


@pytest.mark.asyncio
async def test_scientific_notation():
    result = await calculate("1e10")
    assert result == "1e10 = 10000000000"


@pytest.mark.asyncio
async def test_scientific_notation_with_percentage():
    result = await calculate("1.5e2%")
    # 1.5e2 = 150, 150% = 1.5
    assert result == "1.5e2% = 1.5"


# ==============================================================================
# 6. 错误处理
# ==============================================================================

@pytest.mark.asyncio
@pytest.mark.parametrize("expression", ["1 / 0", "10 % 0", "10 // 0"])
async def test_division_by_zero(expression):
    assert await _failureReason(expression) == "除数不能为零"


@pytest.mark.asyncio
async def test_sqrt_negative():
    """函数参数超出定义域：原因以「函数名() 错误：」开头，后面是 math 库的原话"""
    assert (await _failureReason("sqrt(-1)")).startswith("sqrt() 错误：")


@pytest.mark.asyncio
async def test_function_wrong_argument_count():
    assert (await _failureReason("round(1, 2, 3)")).startswith("round() 错误：")


@pytest.mark.asyncio
async def test_function_wrong_argument_type():
    assert (await _failureReason("sqrt('a')")).startswith("sqrt() 错误：")


@pytest.mark.asyncio
async def test_invalid_expression(mockLog):
    """语法不对：不进日志"""
    assert await _failureReason("2 + * 3") == "表达式格式错误"
    mockLog.assert_not_awaited()


@pytest.mark.asyncio
async def test_unsupported_function():
    assert await _failureReason("eval('1+1')") == "不支持的函数：eval"


@pytest.mark.asyncio
async def test_attribute_call_rejected():
    """只能调用白名单里的函数名，不能写成 模块.函数"""
    assert await _failureReason("math.sqrt(4)") == "不支持复杂函数调用"


@pytest.mark.asyncio
async def test_undefined_variable():
    assert await _failureReason("x + 1") == "未定义的变量：x"


@pytest.mark.asyncio
async def test_unsupported_binary_operator():
    assert await _failureReason("1 << 2") == "不支持的运算符：LShift"


@pytest.mark.asyncio
async def test_unsupported_unary_operator():
    assert await _failureReason("not 1") == "不支持的运算符：Not"


@pytest.mark.asyncio
@pytest.mark.parametrize("expression, nodeName", [
    ("[1, 2]", "List"),
    ("1 if 2 else 3", "IfExp"),
    ("1 < 2", "Compare"),
])
async def test_unsupported_node(expression, nodeName):
    assert await _failureReason(expression) == f"不支持的表达式类型：{nodeName}"


@pytest.mark.asyncio
async def test_empty_expression(mockLog):
    assert await _failureReason("") == "表达式格式错误"
    mockLog.assert_not_awaited()


@pytest.mark.asyncio
async def test_whitespace_only(mockLog):
    assert await _failureReason("   ") == "表达式格式错误"
    mockLog.assert_not_awaited()


@pytest.mark.asyncio
async def test_unexpected_error_reported_as_invalid_expression(mockLog):
    """求值时冒出的其他异常：模型只看到「表达式格式错误」，异常本身记 ERROR 日志"""
    with pytest.raises(ToolError) as excInfo:
        await calculate("'a' + 1")

    assert str(excInfo.value) == "表达式格式错误"
    assert isinstance(excInfo.value.__cause__, TypeError)

    mockLog.assert_awaited_once()
    assert mockLog.await_args.args[0] == "计算错误（预期外）"
    assert mockLog.await_args.args[2] == LogLevel.ERROR
    assert mockLog.await_args.kwargs["exception"] is excInfo.value.__cause__


@pytest.mark.asyncio
async def test_calculation_error_logged_as_user_input(mockLog):
    """算不出来属于输入问题：记 INFO 日志，原始异常挂在 __cause__ 上"""
    with pytest.raises(ToolError) as excInfo:
        await calculate("1 / 0")

    assert excInfo.value.__cause__ is not None

    mockLog.assert_awaited_once()
    assert mockLog.await_args.args[0] == "计算错误（用户输入）"
    assert "1 / 0" in mockLog.await_args.args[1]
    assert mockLog.await_args.args[2] == LogLevel.INFO


@pytest.mark.asyncio
async def test_success_not_logged(mockLog):
    assert await calculate("1 + 1") == "1 + 1 = 2"
    mockLog.assert_not_awaited()


# ==============================================================================
# 7. 结果格式化
# ==============================================================================

@pytest.mark.asyncio
async def test_format_integer():
    result = await calculate("2 + 3")
    assert result == "2 + 3 = 5"  # 不是 "5.0"


@pytest.mark.asyncio
async def test_format_float():
    result = await calculate("10 / 4")
    assert result == "10 / 4 = 2.5"


@pytest.mark.asyncio
async def test_format_trailing_zeros():
    result = await calculate("1 / 2")
    assert result == "1 / 2 = 0.5"  # 不是 "0.5000000000"


# ==============================================================================
# 8. 边界情况
# ==============================================================================

@pytest.mark.asyncio
async def test_very_large_number():
    result = await calculate("10 ** 20")
    assert "10 ** 20 = " in result


@pytest.mark.asyncio
async def test_power_exponent_rejected():
    """超大指数在求值前被拒绝，防止资源耗尽"""
    with pytest.raises(ToolError, match="指数过大"):
        await calculate("2 ** 1000000")


@pytest.mark.asyncio
async def test_nested_power_bomb_rejected():
    """幂塔 9**9**9 的内层结果作为指数超限，被拒绝"""
    with pytest.raises(ToolError, match="指数过大"):
        await calculate("9 ** 9 ** 9")


@pytest.mark.asyncio
async def test_power_exponent_at_limit_ok():
    """指数恰好等于上限：放行"""
    expression = f"1 ** {MAX_POWER_EXPONENT}"
    assert await calculate(expression) == f"{expression} = 1"


@pytest.mark.asyncio
async def test_power_exponent_one_over_limit_rejected():
    """指数比上限多 1：拒绝，原因里带上限数值"""
    reason = await _failureReason(f"1 ** {MAX_POWER_EXPONENT + 1}")
    assert reason == f"幂运算指数过大（超过 {MAX_POWER_EXPONENT}），已拒绝以防资源耗尽"


@pytest.mark.asyncio
async def test_negative_power_exponent_rejected():
    """上限按指数的绝对值算"""
    with pytest.raises(ToolError, match="指数过大"):
        await calculate(f"2 ** -{MAX_POWER_EXPONENT + 1}")


@pytest.mark.asyncio
@pytest.mark.parametrize("expression", ["10 ** 200", "-(10 ** 200)", "1e400"])
async def test_result_overflow(expression):
    """结果的绝对值超过上限（含算成无穷大）：报溢出"""
    assert await _failureReason(expression) == "计算结果溢出"


@pytest.mark.asyncio
async def test_power_within_limit_ok():
    """指数在上限内正常计算"""
    result = await calculate("10 ** 100")
    assert "10 ** 100 = " in result


@pytest.mark.asyncio
async def test_very_small_number():
    result = await calculate("1 / 10000000")
    assert "1 / 10000000 = " in result


@pytest.mark.asyncio
async def test_nested_functions():
    result = await calculate("sqrt(abs(sin(deg(90)) * 100))")
    # sin(deg(90)) ≈ 1, abs(1 * 100) = 100, sqrt(100) = 10
    assert "sqrt(abs(sin(deg(90)) * 100)) = 10" in result


@pytest.mark.asyncio
async def test_max_with_expressions():
    result = await calculate("max(2+3, 4*2, 10-1)")
    # max(5, 8, 9) = 9
    assert result == "max(2+3, 4*2, 10-1) = 9"