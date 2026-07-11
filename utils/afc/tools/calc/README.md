# Calc 工具

## 功能

安全计算数学表达式，支持基础运算、科学函数、常数，使用 AST 白名单机制（无 `eval()` 注入风险）。

## 配置

**无需配置**，开箱即用。

## 触发词

用户消息包含以下任一关键词时，LLM 会收到本工具的 Schema：

- **关键词**（L1 子串命中）：`算`、`计算`、`等于`、`加`、`减`、`乘`、`除`、`平方`、`开方`
- **正则模式**（L2）：`\d+\s*[+\-*/]\s*\d+`（如 "2+3"）
- **通用词**：`帮我`、`帮忙`、`查一下`、`查查`、`告诉我` 等
- **上下文延续**：最近 10 条消息中提到过计算相关词汇

详见 [`triggers.py`](triggers.py)

## 调用示例

LLM 生成的函数调用格式：

```xml
<AFC_ACTION>
{"tool": "calc", "function": "calculate", "parameters": {"expression": "sqrt(2) + log(10)"}}
</AFC_ACTION>
```

- `expression`（必需）：数学表达式字符串

### 支持的运算

| 类别 | 符号/函数 | 示例 |
|------|----------|------|
| **基础运算** | `+` `-` `*` `/` `**` `//` `%` | `2 + 3 * 4` |
| **括号** | `( )` | `(1 + 2) * 3` |
| **科学函数** | `sin` `cos` `tan` `asin` `acos` `atan` `atan2` | `sin(pi/2)` |
| **对数/指数** | `log` `log10` `log2` `exp` `sqrt` | `log(e)` |
| **取整** | `abs` `ceil` `floor` `round` | `ceil(3.2)` |
| **进制转换** | `bin` `oct` `hex` | `hex(255)` → `'0xff'` |
| **角度转换** | `deg` `rad` | `deg(pi)` → `180` |
| **其他** | `max` `min` `pow` `fmod` | `max(1,2,3)` |
| **常数** | `pi` `e` `tau` `inf` `nan` | `pi * 2` |

完整列表见 [`operators.py`](operators.py)

### 特殊语法

- **百分比**：`20%` → `(20/100)` = `0.2`
- **角度符号**：`90°` → `rad(90)` = `1.5707...`（弧度）
- **科学计数法**：`1.5e10` = `15000000000`

## 错误处理

| 错误类型 | 异常类 | 返回示例 |
|---------|--------|---------|
| 不支持的运算符 | `UnsupportedOperatorError` | `错误：不支持的运算符：^` |
| 不支持的节点 | `UnsupportedNodeError` | `错误：不支持的表达式类型：Lambda` |
| 数学域错误 | `MathDomainError` | `错误：数学域错误（如 sqrt 负数、log 非正数）` |
| 除零 | `MathDomainError` | `错误：除数不能为零` |
| 幂次过大 | `MathDomainError` | `错误：幂次过大（超过 1000）` |
| 结果溢出 | `MathDomainError` | `错误：结果数值过大（超过 1e100）` |
| 语法错误 | `CalculationError` | `错误：表达式语法错误：...` |

所有错误类继承自 `CalculationError(ValueError)`，详见 [`errors.py`](errors.py)

## 安全边界

### AST 白名单机制

使用 `ast.parse()` 解析表达式为抽象语法树，仅允许以下节点类型：

- **表达式节点**：`Expression`、`BinOp`、`UnaryOp`、`Call`、`Constant`、`Name`
- **运算符节点**：`Add`、`Sub`、`Mult`、`Div`、`Pow`、`FloorDiv`、`Mod`、`USub`、`UAdd`

任何其他节点（`Lambda`、`Import`、`Attribute` 等）会抛出 `UnsupportedNodeError`。

详见 [`calculator.py:_evalNode`](calculator.py)

### 限制

- **最大幂次**：1000（`MAX_POWER_EXPONENT`）
- **结果上限**：1e100（`MAX_RESULT_MAGNITUDE`）
- **超时**：15 秒（工具调用框架统一超时，`executor.py` 中 `_TOOL_TIMEOUT_SECONDS`）
- **精度**：最多保留 10 位小数（`RESULT_PRECISION`）

### 不支持的操作

- ❌ 变量赋值（`x = 1`）
- ❌ 导入模块（`import os`）
- ❌ 属性访问（`obj.attr`）
- ❌ Lambda 表达式（`lambda x: x + 1`）
- ❌ 列表/字典构造（`[1,2,3]`、`{'a': 1}`）

## 文件结构

```
calc/
├── __init__.py          # 入口函数 calculate（注册到 AFC registry）
├── triggers.py          # 触发词列表
├── config.py            # 配置常量（精度、限制、错误消息）
├── errors.py            # 异常类定义
├── operators.py         # 运算符/函数/常数白名单
├── calculator.py        # AST 解析与求值核心
└── README.md            # 本文档
```

## 依赖

- 标准库：`ast`、`re`、`math`
- 项目模块：`utils.core.logger`（日志记录）

## 示例

```python
# 基础运算
"2 + 3 * 4"          → 14
"(1 + 2) * 3"        → 9
"2 ** 10"            → 1024

# 科学函数
"sqrt(16)"           → 4
"sin(pi/2)"          → 1
"log10(100)"         → 2

# 常数
"pi * 2"             → 6.283185307
"e ** 2"             → 7.389056099

# 特殊语法
"20%"                → 0.2
"90°"                → 1.5707963267（弧度，预处理为 rad(90)）

# 组合
"ceil(sqrt(50) * 10%)" → 1
```

## 角度模式

当前固定为**弧度模式**（`ANGLE_MODE = "radians"`）。

三角函数接收/返回弧度值：
- `sin(pi/2)` → `1`（不是 `sin(90)`）
- 需要角度输入时用 `rad()`：`sin(rad(90))` → `1`
- 需要角度输出时用 `deg()`：`deg(asin(1))` → `90`

未来可扩展为可配置（通过 `config.py`）。
