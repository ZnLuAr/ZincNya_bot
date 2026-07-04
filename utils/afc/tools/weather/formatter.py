"""
天气数据格式化。

将 API 原始响应格式化为用户友好的文本。
"""

from typing import Dict, Any




# ==============================================================================
# 响应格式化
# ==============================================================================

def formatWeatherResponse(data: Dict[str, Any], targetDateOffset: int) -> str:
    """
    格式化天气 API 响应为用户可读文本。

    参数：
        data: API 原始响应 dict
        targetDateOffset: 目标日期偏移（0=今天, 1=明天, 2=后天）

    返回：
        格式化后的天气信息字符串

    示例：
        北京 今天
        天气：晴
        温度：15°C
        体感温度：13°C
        湿度：45%
        风速：12 km/h
    """
    location = data['location']
    cityName = location.get('name', '未知城市')

    # 日期标签
    dateLabels = ['今天', '明天', '后天']
    dateLabel = dateLabels[targetDateOffset] if targetDateOffset < len(dateLabels) else f"+{targetDateOffset}天"

    # 今日用 current 实时数据，未来日用 forecast.day 预报数据
    if targetDateOffset == 0:
        current = data['current']
        condition = current.get('condition', {}).get('text', '未知')
        temp = current.get('temp_c', 'N/A')
        feelsLike = current.get('feelslike_c', 'N/A')
        humidity = current.get('humidity', 'N/A')
        windKph = current.get('wind_kph', 'N/A')

        return (
            f"{cityName} {dateLabel}\n"
            f"天气：{condition}\n"
            f"温度：{temp}°C\n"
            f"体感温度：{feelsLike}°C\n"
            f"湿度：{humidity}%\n"
            f"风速：{windKph} km/h"
        )
    else:
        forecastDay = data['forecast']['forecastday'][targetDateOffset]
        day = forecastDay.get('day', {})
        condition = day.get('condition', {}).get('text', '未知')
        maxTemp = day.get('maxtemp_c', 'N/A')
        minTemp = day.get('mintemp_c', 'N/A')
        avgHumidity = day.get('avghumidity', 'N/A')
        maxWind = day.get('maxwind_kph', 'N/A')

        return (
            f"{cityName} {dateLabel}\n"
            f"天气：{condition}\n"
            f"温度：{minTemp}°C ~ {maxTemp}°C\n"
            f"平均湿度：{avgHumidity}%\n"
            f"最大风速：{maxWind} km/h"
        )
