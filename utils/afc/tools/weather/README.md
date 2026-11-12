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
 
若未配置，调用该工具时这次调用会失败，模型拿到的原因是 `未配置天气 API 密钥（WEATHER_API_KEY）`，不影响其他 AFC 工具和 bot 功能。

## 调用时机

工具说明对模型全量提供，由模型根据对话内容自行决定是否调用。

## 调用示例

LLM 输出的 `tool` 操作：

```json
{"op": "tool", "name": "weather.getWeather", "parameters": {"city": "北京", "date": "today"}}
```

- `city`（必需）：城市名称（中文/英文/拼音均可，如 "Beijing"、"北京"、"beijing"）
- `date`（可选）：日期参数，支持相对日期（"today"/"今天"/"明天"/"后天"）或绝对日期（"YYYY-MM-DD"），默认 "today"

## 缓存

- **缓存时长**：10 分钟（`WEATHER_CACHE_TTL`）
- **缓存键**：`城市名（小写）:日期偏移`（如 `beijing:1`）
- **实现**：内存缓存（bot 重启后清空）

## 错误处理

查询不了时，`getWeather` 抛 `ToolError`（参数问题）或 `ToolDependencyError`（天气服务不可用），消息就是交给模型的失败原因：

| 错误类型 | 抛出的异常 | 交给模型的原因 |
|---------|--------|---------|
| 未配置密钥 | `ToolDependencyError` | `未配置天气 API 密钥（WEATHER_API_KEY）` |
| 城市名为空 | `ToolError` | `城市名不能为空` |
| 日期参数错误 | `ToolError` | `不支持的日期参数（{date}），仅支持 today/明天/后天 或 YYYY-MM-DD 格式（未来 0-2 天）`（合并了格式错误和范围错误两种情况） |
| API 密钥无效 | `ToolDependencyError` | `API 密钥无效或已过期` |
| 城市不存在 | `ToolError` | `请求参数错误（可能是城市名称不存在：xxx）` |
| API 限流 | `ToolDependencyError` | `API 请求频率超限（达到免费档配额）` |
| 服务不可用 | `ToolDependencyError` | `天气服务暂时不可用（HTTP 500）` |
| 网络超时 | `ToolDependencyError` | `请求超时，请稍后重试` |

## 文件结构

```
weather/
├── __init__.py          # 入口函数 getWeather（注册到 AFC registry）
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
- `utils.core.resourceManager`：aiohttp session 生命周期管理（session 清理回调由 `client.py:_registerResources` 注册，它登记在 `modulesRegistry.py` 里 afc 模块的 `initFunctions`，bot 启动时执行）

## 安全边界

- **超时**：15 秒（工具调用框架统一超时，根 `config.py` 的 `AFC_TOOL_TIMEOUT_SECONDS`）
- **输入验证**：城市名非空、dateOffset 范围检查
- **敏感信息**：API Key 不出现在日志和返回内容中（仅在请求参数中使用）
