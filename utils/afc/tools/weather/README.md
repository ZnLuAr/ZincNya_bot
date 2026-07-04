# Weather 工具

## 功能

查询指定城市的实时天气或未来天气预报，支持自然语言日期解析（"明天"、"后天"等）。

## 配置

### 必需：WeatherAPI.com API Key

1. 访问 https://www.weatherapi.com/ 注册免费账号
2. 在控制台获取 API Key（免费档：每月 100 万次调用）
3. 在项目根目录 `.env` 文件中配置：

```bash
WEATHER_API_KEY=your_api_key_here
```
 
若未配置，在调用该工具时，工具会返回错误提示：`"错误：未配置天气 API 密钥（WEATHER_API_KEY）"`，不影响其他 AFC 工具和 bot 功能。

## 触发词

用户消息包含以下任一关键词时，LLM 会收到本工具的 Schema：

- **关键词**：`天气`、`气温`、`下雨`、`晴天`、`阴天`、`温度`
- **通用词**：`帮我`、`帮忙`、`查一下`、`查查`、`告诉我` 等
- **上下文延续**：最近 10 条消息中提到过天气相关词汇

详见 [`triggers.py`](triggers.py)

## 调用示例

LLM 生成的函数调用格式：

```xml
<AFC_ACTION>
{"tool": "weather", "function": "getWeather", "parameters": {"city": "北京", "date": "today"}}
</AFC_ACTION>
```

- `city`（必需）：城市名称（中文/英文/拼音均可，如 "Beijing"、"北京"、"beijing"）
- `date`（可选）：日期参数，支持相对日期（"today"/"今天"/"明天"/"后天"）或绝对日期（"YYYY-MM-DD"），默认 "today"

## 缓存

- **缓存时长**：10 分钟（`WEATHER_CACHE_TTL`）
- **缓存键**：`(city, date)` 二元组
- **实现**：内存缓存（bot 重启后清空）

## 错误处理

| 错误类型 | 异常类 | 返回示例 |
|---------|--------|---------|
| 未配置密钥 | — | `错误：未配置天气 API 密钥（WEATHER_API_KEY）` |
| 城市名为空 | `ValueError` | `错误：城市名称不能为空` |
| 日期格式错误 | `ValueError` | `错误：日期格式不正确` |
| 日期超出范围 | `ValueError` | `错误：日期超出查询范围（仅支持未来 0-2 天）` |
| API 密钥无效 | `ToolDependencyError` | `错误：API 密钥无效或已过期` |
| 城市不存在 | `ValueError` | `错误：请求参数错误（可能是城市名称不存在：xxx）` |
| API 限流 | `ToolDependencyError` | `错误：API 请求频率超限（达到免费档配额）` |
| 服务不可用 | `ToolDependencyError` | `错误：天气服务暂时不可用（HTTP 500）` |
| 网络超时 | `ToolDependencyError` | `错误：请求超时，请稍后重试` |

## 文件结构

```
weather/
├── __init__.py          # 入口函数 getWeather（注册到 AFC registry）
├── triggers.py          # 触发词列表
├── config.py            # 配置常量（API KEY、缓存时长、超时等）
├── client.py            # HTTP 客户端（fetchWeather）
├── cache.py             # 内存缓存（get/save/clear）
├── dateParser.py        # 日期解析（"明天" → date 参数）
├── formatter.py         # 结果格式化（JSON → 中文描述）
└── README.md            # 本文档
```

## 依赖

- `aiohttp`：异步 HTTP 请求
- `utils.core.logger`：日志记录
- `utils.core.resourceManager`：aiohttp session 生命周期管理

## 安全边界

- **超时**：15 秒（工具调用框架统一超时，`executor.py` 中 `_TOOL_TIMEOUT_SECONDS`）
- **输入验证**：城市名非空、dateOffset 范围检查
- **敏感信息**：API Key 不出现在日志和返回内容中（仅在请求参数中使用）
