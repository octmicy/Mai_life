"""时间与天气环境服务。

天气只使用配置中的城市名。城市会先通过 Open-Meteo 地理编码转换为坐标，
坐标仅作为运行时缓存，不暴露给用户配置。
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

_WEATHER_CODES = {
    0: "晴朗", 1: "大致晴朗", 2: "局部多云", 3: "阴天", 45: "有雾", 48: "雾凇",
    51: "小毛毛雨", 53: "毛毛雨", 55: "较强毛毛雨", 61: "小雨", 63: "中雨", 65: "大雨",
    71: "小雪", 73: "中雪", 75: "大雪", 80: "阵雨", 81: "较强阵雨", 82: "强阵雨",
    95: "雷雨", 96: "雷雨伴小冰雹", 99: "雷雨伴冰雹",
}


def _fetch_json(url: str, params: dict[str, Any]) -> dict[str, Any]:
    """在线程中执行同步 HTTP 请求，避免阻塞插件事件循环。"""
    request = Request(
        f"{url}?{urlencode(params)}",
        headers={"User-Agent": "MaiLife/1.0"},
    )
    with urlopen(request, timeout=8) as response:
        return json.loads(response.read().decode("utf-8"))


class EnvironmentService:
    """提供带时区的当前时间和后台天气缓存。"""

    def __init__(self, store: Any, config: Any, logger: Any) -> None:
        self.store = store
        self.config = config
        self.logger = logger
        self._resolved_city_key = ""
        self._resolved_location_name = ""
        self._resolved_latitude: float | None = None
        self._resolved_longitude: float | None = None

    def update_config(self, config: Any) -> None:
        """更新配置；城市变化时清除内存中的地理编码结果。"""
        old_city = str(self.config.environment.city or "").strip().casefold()
        new_city = str(config.environment.city or "").strip().casefold()
        self.config = config
        if old_city != new_city:
            self._clear_resolved_location()

    def _clear_resolved_location(self) -> None:
        self._resolved_city_key = ""
        self._resolved_location_name = ""
        self._resolved_latitude = None
        self._resolved_longitude = None

    def now(self) -> datetime:
        """返回配置时区的当前时间；系统缺少 tzdata 时回退到 UTC+8。"""
        try:
            return datetime.now(ZoneInfo(self.config.environment.timezone))
        except Exception:
            return datetime.now(timezone(timedelta(hours=8)))

    async def _resolve_city(self, city: str) -> tuple[str, float, float]:
        """把城市名解析为坐标，并在 Runner 生命周期内复用结果。"""
        city_key = city.casefold()
        if (
            self._resolved_city_key == city_key
            and self._resolved_latitude is not None
            and self._resolved_longitude is not None
        ):
            return (
                self._resolved_location_name or city,
                self._resolved_latitude,
                self._resolved_longitude,
            )

        geo = await asyncio.to_thread(
            _fetch_json,
            "https://geocoding-api.open-meteo.com/v1/search",
            {"name": city, "count": 1, "language": "zh", "format": "json"},
        )
        results = geo.get("results") or []
        if not results:
            raise ValueError(f"找不到天气城市：{city}")

        result = results[0]
        location_name = str(result.get("name") or city)
        latitude = float(result["latitude"])
        longitude = float(result["longitude"])
        self._resolved_city_key = city_key
        self._resolved_location_name = location_name
        self._resolved_latitude = latitude
        self._resolved_longitude = longitude
        return location_name, latitude, longitude

    # 天气只在后台维护；被动回复读取缓存，避免网络请求阻塞聊天。
    async def refresh_weather(self, force: bool = False) -> dict[str, Any]:
        cached = await self.store.get_weather()
        refresh_seconds = self.config.environment.weather_refresh_minutes * 60
        if (
            not force
            and cached
            and time.time() - float(cached.get("fetched_at", 0)) < refresh_seconds
        ):
            return cached

        city = str(self.config.environment.city or "").strip()
        if not city:
            self.logger.warning("[MaiLife] 未配置天气城市，使用已有天气缓存")
            return cached or {
                "description": "天气未知",
                "location_name": "未配置城市",
                "fetched_at": 0,
            }

        try:
            location_name, latitude, longitude = await self._resolve_city(city)
            raw = await asyncio.to_thread(
                _fetch_json,
                "https://api.open-meteo.com/v1/forecast",
                {
                    "latitude": latitude,
                    "longitude": longitude,
                    "current": "temperature_2m,weather_code",
                    "timezone": self.config.environment.timezone,
                },
            )
            current = raw.get("current") or {}
            code = int(current.get("weather_code", -1))
            data = {
                "fetched_at": time.time(),
                "location_name": location_name,
                "latitude": latitude,
                "longitude": longitude,
                "temperature": current.get("temperature_2m"),
                "weather_code": code,
                "description": _WEATHER_CODES.get(code, "天气状况未知"),
                "raw_json": raw,
            }
            await self.store.save_weather(data)
            return data
        except Exception as exc:
            self.logger.warning(f"[MaiLife] 天气刷新失败，使用缓存: {exc}")
            return cached or {
                "description": "天气未知",
                "location_name": city,
                "fetched_at": 0,
            }

    @staticmethod
    def weather_text(weather: dict[str, Any]) -> str:
        """把天气缓存压缩成适合 Prompt 和状态命令的短文本。"""
        description = str(weather.get("description") or "天气未知")
        temperature_value = weather.get("temperature")
        location_name = str(weather.get("location_name") or "")
        temperature_text = (
            f"，{temperature_value}℃" if temperature_value is not None else ""
        )
        return f"{location_name} {description}{temperature_text}".strip()
