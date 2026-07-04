"""
utils/afc/tools/weather/__init__.py

天气查询工具（AFC 集成）
"""

from utils.core.logger import logSystemEvent
from utils.afc.errors import ToolDependencyError

from . import cache
from . import client
from . import config
from . import dateParser
from . import formatter


# ==============================================================================
# 主入口
# ==============================================================================

async def getWeather(city: str, date: str = "today") -> str:
    """
    查询指定城市的天气信息。

    参数：
        city: 城市名（中文或英文，如 "北京" / "Beijing"）
        date: 日期参数，支持：
            - 相对日期：today/今天/今日, tomorrow/明天/明日, 后天
            - 绝对日期：YYYY-MM-DD（如 2024-03-15，仅支持未来 0-2 天）
            默认值：today

    返回：
        格式化的天气信息字符串
        错误时返回 "错误：<错误消息>"

    示例：
        >>> await getWeather("北京", "today")
        "北京 今天\\n天气：晴\\n温度：15°C\\n..."

        >>> await getWeather("上海", "明天")
        "上海 明天\\n天气：多云\\n温度：10°C ~ 18°C\\n..."
    """
    # 参数校验
    if not city or city is None:
        return "错误：城市名不能为空"

    if not config.WEATHER_API_KEY or config.WEATHER_API_KEY.strip() == "":
        return "错误：未配置天气 API 密钥（WEATHER_API_KEY）"

    # 解析日期参数
    targetDateOffset = dateParser.parseDate(date)
    if targetDateOffset is None:
        return f"错误：不支持的日期参数（{date}），仅支持 today/明天/后天 或 YYYY-MM-DD 格式（未来 0-2 天）"

    # 检查缓存
    cacheKey = cache.getCacheKey(city, targetDateOffset)
    cachedData = cache.getFromCache(cacheKey)
    if cachedData is not None:
        await logSystemEvent(
            "weather_cache_hit",
            f"天气缓存命中：{city}, offset={targetDateOffset}"
        )
        return formatter.formatWeatherResponse(cachedData, targetDateOffset)

    # 调用 API
    try:
        data = await client.fetchWeather(city, targetDateOffset)

        # 保存到缓存
        cache.saveToCache(cacheKey, data)

        await logSystemEvent(
            "weather_api_success",
            f"天气查询成功：{city}, offset={targetDateOffset}"
        )

        return formatter.formatWeatherResponse(data, targetDateOffset)

    except ValueError as e:
        # 用户输入错误（城市名不存在、日期参数错误等）
        await logSystemEvent(
            "weather_input_error",
            f"天气查询参数错误：{city}, error={str(e)}"
        )
        return f"错误：{e}"

    except ToolDependencyError as e:
        # 外部依赖错误（API 不可用、网络超时等）
        await logSystemEvent(
            "weather_dependency_error",
            f"天气服务依赖错误：{city}, error={str(e)}"
        )
        return f"错误：{e}"

    except Exception as e:
        # 预期外错误
        await logSystemEvent(
            "weather_unexpected_error",
            f"天气查询意外错误：{city}, error={str(e)}"
        )
        return f"错误：天气服务暂时不可用（{type(e).__name__}）"


__all__ = ["getWeather"]
