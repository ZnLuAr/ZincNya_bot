"""
天气工具配置。

AFC 工具自带配置：敏感项（API key / 代理）从环境变量读取，
其余常量在此定义默认值。不写入项目根 config.py，保持工具自由配置。

# 环境变量配置说明

使用天气工具前，需在 .env 文件中配置：

## WEATHER_API_KEY（必需）
WeatherAPI.com API 密钥，从 https://www.weatherapi.com/signup.aspx 免费注册获取。
示例：
    WEATHER_API_KEY=your_api_key_here

## WEATHER_HTTP_PROXY（可选）
如果访问 WeatherAPI.com 较慢，可配置 HTTP 代理。
格式：http://host:port 或 socks5://host:port
示例：
    WEATHER_HTTP_PROXY=http://127.0.0.1:7897

# 其他配置

非敏感配置（API base URL、预报天数、超时、缓存 TTL）在此文件定义默认值，
如需自定义可直接修改本文件常量（不建议通过环境变量覆盖）。
"""

import os


# ==============================================================================
# API 配置
# ==============================================================================

# WeatherAPI.com API key（从环境变量读取，未配置则为 None）
# 从 https://www.weatherapi.com/signup.aspx 免费注册获取
WEATHER_API_KEY = os.getenv("WEATHER_API_KEY", None)

WEATHER_API_BASE = "https://api.weatherapi.com/v1"

# 预报天数（WeatherAPI 免费档上限 3 天）
WEATHER_FORECAST_DAYS = 3

# 请求超时（秒）
WEATHER_REQUEST_TIMEOUT = 30

# HTTP 代理（可选，格式：http://host:port），访问 WeatherAPI.com 较慢时配置
WEATHER_HTTP_PROXY = os.getenv("WEATHER_HTTP_PROXY", None)


# ==============================================================================
# 缓存配置
# ==============================================================================

# 缓存 TTL（秒），默认 10 分钟，避免短时间重复查询同一城市
WEATHER_CACHE_TTL = 600
