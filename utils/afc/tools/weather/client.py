"""
天气 API 客户端。

负责 HTTP session 管理和 API 调用。
"""

import asyncio
from typing import Optional, Dict, Any

import aiohttp

from utils.core import resourceManager
from utils.core.logger import logSystemEvent
from utils.afc.errors import ToolDependencyError

from . import config


# ==============================================================================
# Session 管理
# ==============================================================================

_session: Optional[aiohttp.ClientSession] = None
_sessionLock = asyncio.Lock()


async def _getSession() -> aiohttp.ClientSession:
    """
    获取或创建全局 HTTP session（单例模式）。

    使用 asyncio.Lock 防止并发创建多个 session。

    返回：
        全局 aiohttp.ClientSession 实例
    """
    global _session

    async with _sessionLock:
        if _session is None or _session.closed:
            timeout = aiohttp.ClientTimeout(total=config.WEATHER_REQUEST_TIMEOUT)
            _session = aiohttp.ClientSession(timeout=timeout)

            await logSystemEvent(
                "weather_session_created",
                "天气 API session 已创建"
            )

    return _session


async def _closeSession() -> None:
    """
    关闭全局 HTTP session。

    由 resourceManager 在应用关闭时调用。
    """
    global _session

    async with _sessionLock:
        if _session is not None and not _session.closed:
            await _session.close()
            _session = None

            await logSystemEvent(
                "weather_session_closed",
                "天气 API session 已关闭"
            )


def _registerResources() -> None:
    """
    注册资源清理回调（内部函数，避免被 AFC registry 扫描）。
    """
    resourceManager.getResourceManager().register(
        "weather_http_session",
        _closeSession,
        priority=10
    )




# ==============================================================================
# API 调用
# ==============================================================================

async def fetchWeather(city: str, targetDateOffset: int) -> Dict[str, Any]:
    """
    调用 WeatherAPI.com 获取天气数据。

    参数：
        city: 城市名（中文或英文）
        targetDateOffset: 目标日期偏移（0=今天, 1=明天, 2=后天）

    返回：
        API 原始响应 dict（包含 location / current / forecast 字段）

    异常：
        ValueError: 用户输入错误（城市名不存在、参数错误等）
        ToolDependencyError: 外部依赖错误（API 不可用、网络超时等）
    """
    session = await _getSession()

    url = f"{config.WEATHER_API_BASE}/forecast.json"
    params = {
        'key': config.WEATHER_API_KEY,
        'q': city,
        'days': config.WEATHER_FORECAST_DAYS,
        'aqi': 'no',
        'alerts': 'no',
        'lang': 'zh'
    }

    # 可选代理
    proxy = config.WEATHER_HTTP_PROXY if config.WEATHER_HTTP_PROXY else None

    try:
        async with session.get(url, params=params, proxy=proxy) as response:
            # 错误码分类处理
            if response.status == 401:
                raise ToolDependencyError("API 密钥无效或已过期")
            elif response.status == 400:
                raise ValueError(f"请求参数错误（可能是城市名称不存在：{city}）")
            elif response.status == 429:
                raise ToolDependencyError("API 请求频率超限（达到免费档配额）")
            elif response.status >= 500:
                raise ToolDependencyError(f"天气服务暂时不可用（HTTP {response.status}）")
            elif response.status != 200:
                raise ToolDependencyError(f"API 请求失败（HTTP {response.status}）")

            # 解析 JSON
            try:
                data = await response.json()
            except Exception as e:
                raise ToolDependencyError(f"API 响应解析失败：{e}")

            # 校验必需字段
            if 'location' not in data or 'current' not in data or 'forecast' not in data:
                raise ToolDependencyError("API 响应缺少必需字段（location/current/forecast）")

            # 校验 forecast.forecastday 数组
            if 'forecastday' not in data['forecast']:
                raise ToolDependencyError("API 响应缺少 forecast.forecastday 字段")

            forecastDays = data['forecast']['forecastday']
            if not isinstance(forecastDays, list) or len(forecastDays) < targetDateOffset + 1:
                raise ToolDependencyError(f"API 响应的预报天数不足（需要 {targetDateOffset + 1} 天，实际 {len(forecastDays)} 天）")

            return data

    except asyncio.TimeoutError:
        raise ToolDependencyError("请求超时，请稍后重试")
    except aiohttp.ClientError as e:
        raise ToolDependencyError(f"网络请求失败：{e}")
