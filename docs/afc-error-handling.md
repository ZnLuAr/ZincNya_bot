# AFC 错误处理规范

> 最后更新：2026-07-03
>
> Written by ZincNya~ ❤

这份规范定义 AFC 框架内错误如何抛出、传递、捕获、记录——区分用户错误、外部依赖错误、预期外错误，确保日志可追溯、用户体验友好、测试友好。

## 目录

- [概述](#概述)
- [核心原则](#核心原则)
- [错误类体系](#错误类体系)
- [错误处理层级](#错误处理层级)
- [框架级异常类](#框架级异常类)
- [日志记录规范](#日志记录规范)
- [测试指南](#测试指南)
- [参考](#参考)

---

## 概述

AFC 工具执行是一条长长的调用栈：从 `handler` 到 `executor`，再到 `工具函数`，最后到 `外部 API`，错误可能在任何一层冒出来。要是每层各按各的处理，最后就会乱成一团——所以，这份统一规范便应势而生了。目的有四：

1. **让错误传递清晰**：区分用户错误、外部依赖错误、预期外错误；
2. **让日志可追溯**：关键错误记 `logSystemEvent`，便于审计和调试；
3. **使用户体验友好**：返回可读的错误消息而非一大坨堆栈 traceback；
4. **使测试友好**：特定异常类型，便于单元测试断言。

---

## 核心原则

用一句话来概括，就是 **工具函数抛出语义化异常，工具入口统一捕获转为文本。**

不应该在工具函数内部直接返回 `"错误：xxx"` 字符串——因为这样，调用方就没法分辨拿到的到底是"正常结果"还是"出错了"，两者混在同一种返回值里，很难认得出来。正确的做法是让异常携带足够的上下文一路抛上去，由工具入口函数（如 `getWeather` / `calculate`）在顶层分类捕获，再翻译成用户读得懂的文本。

---

## 错误类体系

错误也是分种类的——用户填错了参数，和外部 API 突然掉线，是两回事，不该混为一谈。目前 AFC 把错误分成四类，各自对应一种异常：

### 1. 用户输入错误 → `ValueError`

> **语义**：用户提供的参数不合法（格式错误、超出范围、不支持的值）。
> 
> **处理**：工具入口 `except ValueError` 捕获，返回 `f"错误：{e}"`。

### 2. 外部依赖错误 → `ToolDependencyError`

> **语义**：外部 API / 服务不可用、超时、认证失败、限流。
> 
> **含义**：工具函数抛出此异常表示"我的实现没问题，但依赖的外部服务遇到了问题"。
> 
> **处理**：工具入口 `except ToolDependencyError` 捕获，返回 `f"错误：{e}"`。
> 
> **定义位置**：`utils/afc/errors.py`（框架级，所有工具共享）。

### 3. 工具内部业务错误 → 工具自定义异常（建议继承 `ValueError`）

> **语义**：工具特有的业务逻辑错误（如"不支持的运算符"、"数学域错误"）。
> 
> **建议**：让工具自定义异常继承 `ValueError`，这样，在工具入口统一使用 `except ValueError` 即可覆盖，同时保留精确的异常类型供单测断言。
> 
> **定义位置**：工具目录下的 `errors.py`。

### 4. 预期外错误 → `Exception`

> **语义**：未预料的异常（代码 bug、依赖库内部错误）。
> 
> **处理**：工具入口由 `except Exception` 兜底，返回通用错误消息，避免暴露异常细节给用户。还有——**记日志是必要的**。

---

## 错误处理层级

四类异常抛出来之后，得有能接住它们的东西。AFC 从下到上分为四层，每层各管一段——工具函数只管抛出、入口函数负责翻译、executor 负责打包，框架层则是负责别让一个坏工具拖垮全场：

### 1. 工具函数层（内部实现，如 API 客户端）

**职责**：抛出语义化异常，携带足够的上下文信息。

**规则**：
- 外部依赖错误按类型抛 `ToolDependencyError`（认证失败、限流、服务不可用、超时、网络错误）
- 用户输入错误抛 `ValueError`
- **不捕获 `Exception`**（让入口层兜底）

**示例**：

```python
from utils.afc.errors import ToolDependencyError

# 正面示例：语义清晰
if response.status == 401:
    raise ToolDependencyError("API 密钥无效或已过期")
elif response.status == 429:
    raise ToolDependencyError("API 请求频率超限")
elif response.status == 400:
    raise ValueError(f"参数不合法：{param}")

# 反面示例：全用 ValueError，语义不清
if response.status != 200:
    raise ValueError(f"API 错误 {response.status}")
```

### 2. 工具入口函数层（如 `getWeather` / `calculate`）

**职责**：统一捕获异常，转换为用户友好的文本消息。

**规则**：
- 按异常类型分类捕获（`ValueError` → `ToolDependencyError` → `Exception`）
- `ValueError` 和 `ToolDependencyError` 返回 `f"错误：{e}"`（用户可读）
- `Exception` 兜底返回通用消息 + 记日志（不暴露内部细节）
- 关键错误记日志 `await logSystemEvent(...)`

**模板**：

```python
async def toolEntry(...) -> str:
    """工具入口函数"""
    try:
        # ... 实现逻辑
        return formattedResult

    except ValueError as e:
        # 用户输入错误
        await logSystemEvent("tool_user_error", f"...{e}")
        return f"错误：{e}"

    except ToolDependencyError as e:
        # 外部依赖错误
        await logSystemEvent("tool_dependency_error", f"...{e}")
        return f"错误：{e}"

    except Exception as e:
        # 预期外错误——不暴露细节，仅记日志
        await logSystemEvent("tool_unexpected_error", f"...{type(e).__name__}: {e}")
        return "错误：工具暂时不可用"
```

### 3. Executor 层（`utils/afc/executor.py`）

**职责**：执行工具函数，包装结果为 `<FUNCTION_RESULT>` / `<FUNCTION_ERROR>`。

**规则**：
- 工具函数返回字符串（包括以 `"错误：xxx"` 开头的错误消息）→ 包装为 `<FUNCTION_RESULT>`（注意：工具入口已捕获并返回错误描述字符串时，executor 仍将其视为成功结果，因为函数本身未抛异常）
- 工具函数抛出异常（超时 / 未捕获异常）→ 包装为 `<FUNCTION_ERROR>`
- **所有异常应该记入日志，但不暴露异常细节给 LLM**（executor 的 Exception 兜底返回通用错误消息）

Executor 是框架统一入口，工具开发者几乎无需关心此层——工具函数只需正常返回结果字符串（包括错误描述），或抛出异常让框架捕获。

### 4. 框架层（registry / afcIntent）

**职责**：扫描工具、构建索引，失败时静默跳过（不阻断启动）。

**规则**：
- 工具加载失败 / 触发器导入失败 → 记日志，**继续扫描其它工具**
- **不向上抛异常**（避免一个坏工具导致整个 AFC 不可用）

——这一层的克制是有意的：一个新工具写坏了，最多让它自己不上线，而不该连累整个 AFC 陪它一起罢工。

---

## 框架级异常类

上面反复提到的 `ToolDependencyError`，就定义在这里。框架级异常放在一处，所有工具共享，不用自己再造一个：

### 定义位置：`utils/afc/errors.py`

```python
"""
AFC 框架级异常定义。

所有 AFC 工具应使用这些基类或标准库异常，不应直接抛 Exception。
"""


class AFCError(Exception):
    """AFC 框架基础异常类。"""
    pass


class ToolDependencyError(AFCError):
    """
    外部依赖错误（网络 API、数据库、文件系统等）。

    工具遇到外部依赖不可用时应抛此异常，executor 会捕获并返回降级响应。
    """
    pass
```

### 工具自定义异常（继承 `ValueError`）

工具目录下 `errors.py` 定义业务异常，继承 `ValueError` 以便入口层统一捕获：

```python
class MyToolError(ValueError):
    """工具业务错误基类（继承 ValueError）"""
    pass


class SpecificError(MyToolError):
    """具体错误类型（供单测精确断言）"""
    pass
```

**好处**：

1. 单测可精确断言 `with pytest.raises(SpecificError)`
2. 入口层统一 `except ValueError` 覆盖所有子类
3. 语义清晰：这些都是"用户输入的错误"，不是内部 bug

---

## 日志记录规范

### 何时记日志

| 场景 | 是否记日志 | 语义级别 | event_type 示例 |
|------|-----------|---------|----------------|
| 工具执行成功 | ✅ | INFO | `<tool>_success` |
| 用户输入错误（`ValueError`） | ✅ | INFO | `<tool>_user_error` |
| 外部依赖错误（`ToolDependencyError`） | ✅ | WARNING | `<tool>_dependency_error` |
| 预期外错误（`Exception`） | ✅ | ERROR | `<tool>_unexpected_error` |
| 工具执行超时 | ✅ | WARNING | `AFC 执行超时` |

### 日志格式

```python
await logSystemEvent(
    "event_type",
    f"简要描述：{toolName}.{funcName}, 关键参数=..., error={str(e)}"
)
```

**注意**：不要在日志里回显密钥 / 凭据（按 key 名引用）；虽然 `logSystemEvent` 已做了部分的脱敏，但工具自己拼接的消息还需要自己检查。

---

## 测试指南

测试错误处理和测试正常流程是两码事——要验证的不是"它能算对"，而是"它在出岔子时会不会整段垮掉"：对的异常该抛、错的堆栈不该暴露；日志是要记的、格式是要对的……

### 用户输入错误

```python
async def test_invalid_input():
    result = await toolEntry(invalidParam)
    assert result.startswith("错误：")
```

### 外部依赖错误

```python
async def test_dependency_error():
    with patch('...client', side_effect=ToolDependencyError("服务不可用")):
        result = await toolEntry(...)
        assert "错误：" in result
```

### 预期外错误

```python
async def test_unexpected_error():
    with patch('...impl', side_effect=RuntimeError("bug")):
        result = await toolEntry(...)
        # 不暴露内部细节，返回通用消息
        assert result == "错误：工具暂时不可用"
```

### 精确异常断言（工具内部）

```python
def test_specific_error_raised():
    with pytest.raises(SpecificError):
        internalFunction(badInput)
```

---

## 参考

- `utils/afc/errors.py` — 框架级异常类
- `utils/afc/executor.py:execute()` — 工具调用顶层异常处理
- [docs/afc.md](afc.md) — AFC 架构文档
