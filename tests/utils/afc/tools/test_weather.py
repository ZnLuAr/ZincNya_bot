"""
tests/utils/afc/tools/test_weather.py

天气查询工具测试套件
"""

import asyncio
import time
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import aiohttp

from utils.afc.tools.weather import getWeather
from utils.afc.tools.weather.cache import clearCache, getCacheKey, getFromCache, saveToCache
from utils.afc.tools.weather.dateParser import parseDate
from utils.afc.tools.weather.formatter import formatWeatherResponse


# ====================================================================================================
# Mock 数据
# ====================================================================================================

MOCK_API_RESPONSE = {
    "location": {
        "name": "北京",
        "region": "北京市",
        "country": "中国",
        "localtime": "2026-06-28 14:30"
    },
    "current": {
        "temp_c": 28,
        "condition": {"text": "晴"},
        "humidity": 45,
        "wind_kph": 12,
        "feelslike_c": 26
    },
    "forecast": {
        "forecastday": [
            {
                "date": "2026-06-28",
                "day": {
                    "maxtemp_c": 30,
                    "mintemp_c": 22,
                    "avgtemp_c": 26,
                    "condition": {"text": "晴"},
                    "daily_chance_of_rain": 10,
                    "maxwind_kph": 15,
                    "avghumidity": 50
                }
            },
            {
                "date": "2026-06-29",
                "day": {
                    "maxtemp_c": 32,
                    "mintemp_c": 24,
                    "condition": {"text": "多云"},
                    "daily_chance_of_rain": 20,
                    "maxwind_kph": 18,
                    "avghumidity": 55
                }
            },
            {
                "date": "2026-06-30",
                "day": {
                    "maxtemp_c": 29,
                    "mintemp_c": 21,
                    "condition": {"text": "小雨"},
                    "daily_chance_of_rain": 60,
                    "maxwind_kph": 20,
                    "avghumidity": 70
                }
            }
        ]
    }
}


# ====================================================================================================
# Fixtures
# ====================================================================================================

@pytest.fixture(autouse=True)
def clearCacheFixture():
    """每个测试前清空缓存"""
    clearCache()
    yield
    clearCache()


@pytest.fixture
def mockApiKey():
    """Mock API key（避免未配置导致测试失败）"""
    with patch('utils.afc.tools.weather.config.WEATHER_API_KEY', 'test_key_12345'):
        yield


@pytest.fixture
def mockSession():
    """Mock aiohttp.ClientSession"""
    mockResp = AsyncMock()
    mockResp.status = 200
    mockResp.json = AsyncMock(return_value=MOCK_API_RESPONSE)
    mockResp.__aenter__ = AsyncMock(return_value=mockResp)
    mockResp.__aexit__ = AsyncMock(return_value=None)

    mockSessionInstance = MagicMock()
    mockSessionInstance.get = MagicMock(return_value=mockResp)
    mockSessionInstance.closed = False

    with patch('utils.afc.tools.weather.client._getSession', return_value=mockSessionInstance):
        yield mockSessionInstance


# ====================================================================================================
# 1. Cache 模块测试（5 个用例）
# ====================================================================================================

def test_cache_key_generation():
    """验证缓存 key 生成逻辑"""
    assert getCacheKey("北京", 0) == "北京:0"
    assert getCacheKey("Beijing", 1) == "beijing:1"
    assert getCacheKey("SHANGHAI", 2) == "shanghai:2"


def test_cache_save_and_get():
    """验证缓存存取"""
    testData = {"location": {"name": "北京"}}
    cacheKey = getCacheKey("北京", 0)

    saveToCache(cacheKey, testData)
    retrieved = getFromCache(cacheKey)

    assert retrieved is not None
    assert retrieved == testData


def test_cache_expiry():
    """验证缓存 TTL 过期"""
    testData = {"location": {"name": "北京"}}
    cacheKey = getCacheKey("北京", 0)

    with patch('time.time', return_value=1000.0):
        saveToCache(cacheKey, testData)

    # 599 秒后（未过期）
    with patch('time.time', return_value=1599.0):
        assert getFromCache(cacheKey) is not None

    # 601 秒后（已过期）
    with patch('time.time', return_value=1601.0):
        assert getFromCache(cacheKey) is None


def test_cache_miss():
    """验证缓存未命中"""
    cacheKey = getCacheKey("不存在", 0)
    assert getFromCache(cacheKey) is None


def test_cache_clear():
    """验证清空缓存"""
    saveToCache(getCacheKey("北京", 0), {"test": "data1"})
    saveToCache(getCacheKey("上海", 1), {"test": "data2"})

    clearCache()

    assert getFromCache(getCacheKey("北京", 0)) is None
    assert getFromCache(getCacheKey("上海", 1)) is None


# ====================================================================================================
# 2. DateParser 模块测试（7 个用例）
# ====================================================================================================

def test_parse_date_relative_today():
    """解析相对日期：today/今天/今日"""
    assert parseDate("today") == 0
    assert parseDate("今天") == 0
    assert parseDate("今日") == 0


def test_parse_date_relative_tomorrow():
    """解析相对日期：tomorrow/明天/明日"""
    assert parseDate("tomorrow") == 1
    assert parseDate("明天") == 1
    assert parseDate("明日") == 1


def test_parse_date_relative_after_tomorrow():
    """解析相对日期：后天"""
    assert parseDate("后天") == 2
    assert parseDate("aftertomorrow") == 2


def test_parse_date_absolute():
    """解析绝对日期：YYYY-MM-DD"""
    today = datetime.now().date()
    tomorrow = today + timedelta(days=1)
    afterTomorrow = today + timedelta(days=2)

    assert parseDate(today.strftime("%Y-%m-%d")) == 0
    assert parseDate(tomorrow.strftime("%Y-%m-%d")) == 1
    assert parseDate(afterTomorrow.strftime("%Y-%m-%d")) == 2


def test_parse_date_out_of_range():
    """解析超出范围的日期"""
    today = datetime.now().date()
    futureDate = today + timedelta(days=10)

    assert parseDate(futureDate.strftime("%Y-%m-%d")) is None


def test_parse_date_past():
    """解析过去日期"""
    today = datetime.now().date()
    yesterday = today - timedelta(days=1)

    assert parseDate(yesterday.strftime("%Y-%m-%d")) is None


def test_parse_date_invalid_format():
    """解析无效格式"""
    assert parseDate("invalid_date") is None
    assert parseDate("2024-13-01") is None  # 月份超出范围
    assert parseDate("not a date") is None


# ====================================================================================================
# 3. Formatter 模块测试（2 个用例）
# ====================================================================================================

def test_format_today():
    """格式化今日天气（current 字段）"""
    result = formatWeatherResponse(MOCK_API_RESPONSE, 0)

    assert "北京 今天" in result
    assert "晴" in result
    assert "28°C" in result
    assert "26°C" in result  # 体感温度
    assert "45%" in result   # 湿度


def test_format_forecast():
    """格式化未来天气（forecast.day 字段）"""
    result = formatWeatherResponse(MOCK_API_RESPONSE, 1)

    assert "北京 明天" in result
    assert "多云" in result
    assert "24°C ~ 32°C" in result
    assert "55%" in result   # 平均湿度


# ====================================================================================================
# 4. 集成测试（getWeather 主入口）
# ====================================================================================================

@pytest.mark.asyncio
async def test_getWeather_success(mockApiKey, mockSession):
    """正常查询天气"""
    result = await getWeather("北京", "today")
    assert "北京" in result
    assert "晴" in result
    assert "28°C" in result


@pytest.mark.asyncio
async def test_getWeather_cache_hit(mockApiKey, mockSession):
    """缓存命中，不重复调用 API"""
    result1 = await getWeather("北京", "today")
    result2 = await getWeather("北京", "today")

    assert result1 == result2
    assert mockSession.get.call_count == 1


@pytest.mark.asyncio
async def test_getWeather_different_dates(mockApiKey, mockSession):
    """不同日期独立调用 API"""
    result1 = await getWeather("北京", "today")
    result2 = await getWeather("北京", "tomorrow")

    assert "今天" in result1
    assert "明天" in result2
    assert mockSession.get.call_count == 2


@pytest.mark.asyncio
async def test_getWeather_city_empty(mockApiKey):
    """城市名为空"""
    result = await getWeather("", "today")
    assert "错误：城市名不能为空" in result


@pytest.mark.asyncio
async def test_getWeather_city_none(mockApiKey):
    """城市名为 None"""
    result = await getWeather(None, "today")
    assert "错误：城市名不能为空" in result


@pytest.mark.asyncio
async def test_getWeather_api_key_missing():
    """API key 未配置"""
    with patch('utils.afc.tools.weather.config.WEATHER_API_KEY', None):
        result = await getWeather("北京", "today")
        assert "错误：未配置天气 API 密钥" in result


@pytest.mark.asyncio
async def test_getWeather_api_key_empty():
    """API key 为空字符串"""
    with patch('utils.afc.tools.weather.config.WEATHER_API_KEY', '   '):
        result = await getWeather("北京", "today")
        assert "错误：未配置天气 API 密钥" in result


@pytest.mark.asyncio
async def test_getWeather_invalid_date(mockApiKey, mockSession):
    """无效日期参数"""
    result = await getWeather("北京", "invalid_date")
    assert "错误：不支持的日期参数" in result


@pytest.mark.asyncio
async def test_getWeather_date_out_of_range(mockApiKey, mockSession):
    """日期超出范围（>2 天）"""
    today = datetime.now().date()
    futureDate = today + timedelta(days=10)

    result = await getWeather("北京", futureDate.strftime("%Y-%m-%d"))
    assert "错误：不支持的日期参数" in result


@pytest.mark.asyncio
async def test_getWeather_api_401(mockApiKey):
    """API 返回 401（密钥无效）"""
    mockResp = AsyncMock()
    mockResp.status = 401
    mockResp.__aenter__ = AsyncMock(return_value=mockResp)
    mockResp.__aexit__ = AsyncMock(return_value=None)

    mockSessionInstance = MagicMock()
    mockSessionInstance.get = MagicMock(return_value=mockResp)

    with patch('utils.afc.tools.weather.client._getSession', return_value=mockSessionInstance):
        result = await getWeather("北京", "today")
        assert "错误：API 密钥无效" in result


@pytest.mark.asyncio
async def test_getWeather_api_400(mockApiKey):
    """API 返回 400（城市不存在）"""
    mockResp = AsyncMock()
    mockResp.status = 400
    mockResp.__aenter__ = AsyncMock(return_value=mockResp)
    mockResp.__aexit__ = AsyncMock(return_value=None)

    mockSessionInstance = MagicMock()
    mockSessionInstance.get = MagicMock(return_value=mockResp)

    with patch('utils.afc.tools.weather.client._getSession', return_value=mockSessionInstance):
        result = await getWeather("InvalidCity12345", "today")
        assert "错误：请求参数错误" in result


@pytest.mark.asyncio
async def test_getWeather_api_429(mockApiKey):
    """API 返回 429（频率超限）"""
    mockResp = AsyncMock()
    mockResp.status = 429
    mockResp.__aenter__ = AsyncMock(return_value=mockResp)
    mockResp.__aexit__ = AsyncMock(return_value=None)

    mockSessionInstance = MagicMock()
    mockSessionInstance.get = MagicMock(return_value=mockResp)

    with patch('utils.afc.tools.weather.client._getSession', return_value=mockSessionInstance):
        result = await getWeather("北京", "today")
        assert "错误：API 请求频率超限" in result


@pytest.mark.asyncio
async def test_getWeather_api_500(mockApiKey):
    """API 返回 500（服务器错误）"""
    mockResp = AsyncMock()
    mockResp.status = 500
    mockResp.__aenter__ = AsyncMock(return_value=mockResp)
    mockResp.__aexit__ = AsyncMock(return_value=None)

    mockSessionInstance = MagicMock()
    mockSessionInstance.get = MagicMock(return_value=mockResp)

    with patch('utils.afc.tools.weather.client._getSession', return_value=mockSessionInstance):
        result = await getWeather("北京", "today")
        assert "错误：天气服务暂时不可用" in result


@pytest.mark.asyncio
async def test_getWeather_network_error(mockApiKey):
    """网络错误"""
    mockSessionInstance = MagicMock()
    mockSessionInstance.get = MagicMock(side_effect=aiohttp.ClientError("Network error"))

    with patch('utils.afc.tools.weather.client._getSession', return_value=mockSessionInstance):
        result = await getWeather("北京", "today")
        assert "错误：网络请求失败" in result


@pytest.mark.asyncio
async def test_getWeather_json_parse_error(mockApiKey):
    """JSON 解析错误"""
    mockResp = AsyncMock()
    mockResp.status = 200
    mockResp.json = AsyncMock(side_effect=Exception("Invalid JSON"))
    mockResp.__aenter__ = AsyncMock(return_value=mockResp)
    mockResp.__aexit__ = AsyncMock(return_value=None)

    mockSessionInstance = MagicMock()
    mockSessionInstance.get = MagicMock(return_value=mockResp)

    with patch('utils.afc.tools.weather.client._getSession', return_value=mockSessionInstance):
        result = await getWeather("北京", "today")
        assert "错误：API 响应解析失败" in result


@pytest.mark.asyncio
async def test_getWeather_missing_fields(mockApiKey):
    """响应缺少必需字段"""
    incompleteResponse = {"location": {"name": "北京"}}  # 缺 current 和 forecast

    mockResp = AsyncMock()
    mockResp.status = 200
    mockResp.json = AsyncMock(return_value=incompleteResponse)
    mockResp.__aenter__ = AsyncMock(return_value=mockResp)
    mockResp.__aexit__ = AsyncMock(return_value=None)

    mockSessionInstance = MagicMock()
    mockSessionInstance.get = MagicMock(return_value=mockResp)

    with patch('utils.afc.tools.weather.client._getSession', return_value=mockSessionInstance):
        result = await getWeather("北京", "today")
        assert "错误：API 响应缺少必需字段" in result


@pytest.mark.asyncio
async def test_getWeather_insufficient_forecast_days(mockApiKey):
    """预报天数不足"""
    insufficientResponse = MOCK_API_RESPONSE.copy()
    insufficientResponse['forecast'] = {"forecastday": []}  # 空数组

    mockResp = AsyncMock()
    mockResp.status = 200
    mockResp.json = AsyncMock(return_value=insufficientResponse)
    mockResp.__aenter__ = AsyncMock(return_value=mockResp)
    mockResp.__aexit__ = AsyncMock(return_value=None)

    mockSessionInstance = MagicMock()
    mockSessionInstance.get = MagicMock(return_value=mockResp)

    with patch('utils.afc.tools.weather.client._getSession', return_value=mockSessionInstance):
        result = await getWeather("北京", "today")
        assert "错误：API 响应的预报天数不足" in result


@pytest.mark.asyncio
async def test_getWeather_timeout(mockApiKey):
    """超时错误"""
    mockSessionInstance = MagicMock()
    mockSessionInstance.get = MagicMock(side_effect=asyncio.TimeoutError())

    with patch('utils.afc.tools.weather.client._getSession', return_value=mockSessionInstance):
        result = await getWeather("北京", "today")
        assert "错误：请求超时" in result
