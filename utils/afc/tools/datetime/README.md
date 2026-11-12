# datetime 工具

为 LLM 提供时间感知能力的日期时间工具。

## 功能

### 1. getCurrentTime - 获取当前时间

```python
await getCurrentTime("full")     # "2024-03-15 14:30:00 星期五 UTC+8"
await getCurrentTime("date")     # "2024-03-15"
await getCurrentTime("time")     # "14:30:00"
await getCurrentTime("weekday")  # "星期五"
await getCurrentTime("iso")      # "2024-03-15T14:30:00.123456+08:00"
```

**参数**：
- `fmt` (str, 可选): 输出格式
  - `"full"` - 完整时间（默认）
  - `"datetime"` - 日期 + 时间
  - `"date"` - 仅日期
  - `"time"` - 仅时间
  - `"weekday"` - 仅星期
  - `"iso"` - ISO 8601 格式

### 2. calculateDateDiff - 计算日期差

```python
await calculateDateDiff("2024-12-31")           # "距离 2024-12-31 还有 291 天"
await calculateDateDiff("昨天", "今天")         # "昨天 距今 1 天"
await calculateDateDiff("明天", "2024-12-31")   # "明天 距今 290 天"
```

**参数**：
- `date1` (str): 目标日期
  - 绝对日期：`"YYYY-MM-DD"`（如 `"2024-12-31"`）
  - 相对日期：`today`/`今天`, `tomorrow`/`明天`, `yesterday`/`昨天`, `后天`, `前天`
- `date2` (str, 可选): 参考日期（默认 `"today"`），格式同 `date1`

### 3. getFutureDate - 计算未来/过去日期

```python
await getFutureDate(3)         # "3 天后是 2024-03-18"
await getFutureDate(-2)        # "2 天前是 2024-03-13"
await getFutureDate(0)         # "今天是 2024-03-15"
await getFutureDate(7, "full") # "7 天后是 2024-03-22 10:30:00 星期五 UTC+8"
```

**参数**：
- `days` (int): 天数偏移量
  - 正数：未来（如 `3` 表示 "3 天后"）
  - 负数：过去（如 `-2` 表示 "2 天前"）
  - `0`：今天
- `fmt` (str, 可选): 输出格式（同 `getCurrentTime`，默认 `"date"`）

## 调用时机

工具说明对模型全量提供，由模型根据对话内容自行决定是否调用。

## 错误处理

参数不合法时抛 `ToolError`，消息就是交给模型的失败原因：

```python
await getCurrentTime("invalid")       # ToolError: 不支持的格式：invalid
await calculateDateDiff("2024-99-99") # ToolError: 无法解析日期：2024-99-99
await calculateDateDiff("")           # ToolError: 日期不能为空
```

## 设计特点

- ✅ **纯本地实现**：仅依赖标准库 `datetime`/`re`，无外部依赖
- ✅ **时区感知**：使用系统本地时区（`datetime.now().astimezone()`）
- ✅ **友好输出**：中文星期、描述性前缀（"3 天后是"）
- ✅ **稳定格式化**：时区显示为 `UTC+8`（不依赖本地化时区名）

## 限制

- ❌ 不支持自然语言日期（如 "下周五"）
- ❌ 不支持月份/年份计算
- ❌ 不支持时区转换
- ✅ 专注于"当前时刻 + 简单日期计算"
