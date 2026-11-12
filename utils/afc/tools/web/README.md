# web 工具

让 LLM 读取一个公开网页或文本文件的文字内容。

## 功能

### readURL - 读取网页

```python
await readURL("https://example.com/article")
# 地址：https://example.com/article
# 标题：Example Article
#
# 正文文字……
```

**参数**：
- `url` (str): 要读取的完整地址，必须以 `http://` 或 `https://` 开头

**返回**：实际读取的地址（跟随 redirect 之后）、标题和正文文字。正文超过长度上限或响应体超过字节上限时只保留开头部分，并在正文前标注。

**失败**：抛 `ToolError`，消息里是原因（地址被拒绝、HTTP 错误状态、非文本内容、网络错误等）。

## 调用时机

工具说明对模型全量提供，由模型根据对话内容自行决定是否调用。

## 默认停用

本工具会让 bot 所在的服务器向模型给出的地址发起请求，所以在 `data/afcToolsBuiltin.json` 里声明了 `"defaultEnabled": false`，需要 ops 显式启用后才会出现在工具说明里。

## 安全限制

- 只接受 `http` / `https`，不接受带 userinfo 的地址。
- 拒绝黑名单 hostname（`config.py` 的 `WEB_FETCH_BLOCKED_HOSTS`）及其子域。
- 拒绝私有、回环、链路本地、组播、保留地址；DNS 解析结果在连接时再校验一次。
- redirect 手动跟随，每一跳都重新校验，跳数有上限。
- 只读取文本类内容，响应体按字节上限截断，疑似二进制的响应直接拒绝。
- 不携带 cookie，不使用环境变量里的代理设置。

## 配置

全部在 `config.py`，没有敏感项，也不读环境变量：

| 常量 | 含义 |
| --- | --- |
| `WEB_FETCH_TIMEOUT_SECONDS` | 单次 HTTP 请求超时 |
| `WEB_FETCH_MAX_RETRIES` / `WEB_FETCH_RETRY_BACKOFF_SECONDS` | 网络错误时的重试次数与退避 |
| `WEB_FETCH_REDIRECT_LIMIT` | 最多跟随的 redirect 跳数 |
| `WEB_FETCH_MAX_BYTES` | 响应体读取上限 |
| `WEB_FETCH_MAX_CHARS` | 交给模型的正文长度上限 |
| `WEB_FETCH_BLOCKED_HOSTS` | 拒绝访问的 hostname |

## 文件结构

```
web/
├── __init__.py   # 工具入口 readURL
├── config.py     # 工具配置
├── fetcher.py    # 抓取、地址安全校验、正文提取
└── README.md
```
