"""
天气数据缓存。

基于内存的 TTL 缓存，避免短时间内重复查询同一城市。
"""

import time
from typing import Dict, Tuple, Any, Optional

from . import config


# 缓存存储
_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}




# ==============================================================================
# 缓存操作
# ==============================================================================

def getCacheKey(city: str, targetDateOffset: int) -> str:
    """
    生成缓存 key。

    参数：
        city: 城市名
        targetDateOffset: 目标日期偏移（0=今天, 1=明天, 2=后天）

    返回：
        缓存 key（格式：city:offset）
    """
    return f"{city.lower()}:{targetDateOffset}"


def getFromCache(cacheKey: str) -> Optional[Dict[str, Any]]:
    """
    从缓存获取数据。

    参数：
        cacheKey: 缓存 key

    返回：
        缓存的 API 响应 dict，或 None（未命中或已过期）
    """
    if cacheKey in _cache:
        cachedTs, cachedData = _cache[cacheKey]
        if time.time() - cachedTs < config.WEATHER_CACHE_TTL:
            return cachedData

        # 过期则删除
        del _cache[cacheKey]

    return None


def saveToCache(cacheKey: str, data: Dict[str, Any]) -> None:
    """
    保存数据到缓存。

    参数：
        cacheKey: 缓存 key
        data: API 响应 dict
    """
    _cache[cacheKey] = (time.time(), data)


def clearCache() -> None:
    """
    清空所有缓存（用于测试）。
    """
    _cache.clear()
    