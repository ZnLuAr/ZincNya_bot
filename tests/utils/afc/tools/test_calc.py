"""
tests/utils/afc/tools/test_calc.py

计算器工具测试套件
"""

import pytest
from utils.afc.tools.calc import calculate


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
async def test_division_by_zero():
    result = await calculate("1 / 0")
    assert "错误" in result
    assert "除数不能为零" in result


@pytest.mark.asyncio
async def test_sqrt_negative():
    result = await calculate("sqrt(-1)")
    assert "错误" in result


@pytest.mark.asyncio
async def test_invalid_expression():
    result = await calculate("2 + * 3")
    assert "计算错误" in result
    assert "表达式格式错误" in result


@pytest.mark.asyncio
async def test_unsupported_function():
    result = await calculate("eval('1+1')")
    assert "错误" in result


@pytest.mark.asyncio
async def test_empty_expression():
    result = await calculate("")
    assert "计算错误" in result
    assert "表达式格式错误" in result


@pytest.mark.asyncio
async def test_whitespace_only():
    result = await calculate("   ")
    assert "计算错误" in result
    assert "表达式格式错误" in result


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
    result = await calculate("2 ** 1000000")
    assert "错误" in result
    assert "指数过大" in result


@pytest.mark.asyncio
async def test_nested_power_bomb_rejected():
    """幂塔 9**9**9 的内层结果作为指数超限，被拒绝"""
    result = await calculate("9 ** 9 ** 9")
    assert "错误" in result
    assert "指数过大" in result


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