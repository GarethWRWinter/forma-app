"""Weather for rides and briefings, via OpenWeatherMap One Call 3.0.

Two directions: what the sky DID (historical conditions stamped on a ride)
and what it WILL do (today's forecast for the pre-ride briefing). Dormant
until OPENWEATHER_API_KEY exists; every caller must survive a None.

Provider choice (5 Aug 2026): One Call 3.0 covers both directions in one
API, the free tier (1,000 calls/day) permits commercial use, and it scales
pay-per-call. Open-Meteo was rejected: its free tier is non-commercial and
Forma is a paid product. We don't build on terms we're breaking.
"""

import logging
from datetime import datetime, timezone

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

BASE = "https://api.openweathermap.org/data/3.0/onecall"


def is_configured() -> bool:
    return bool(settings.openweather_api_key)


def _wind_kph(ms: float | None) -> float | None:
    return round(ms * 3.6, 1) if ms is not None else None


def _compass(deg: float | None) -> str | None:
    if deg is None:
        return None
    points = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return points[round(deg / 45) % 8]


def ride_conditions(lat: float, lon: float, when: datetime) -> dict | None:
    """Conditions at a point in time and space (the ride's start).

    Synchronous by design: called from the import pipeline. Returns a compact
    dict for the ride's weather column, or None when unavailable.
    """
    if not is_configured():
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(
                f"{BASE}/timemachine",
                params={
                    "lat": lat,
                    "lon": lon,
                    "dt": int(when.timestamp()),
                    "units": "metric",
                    "appid": settings.openweather_api_key,
                },
            )
            resp.raise_for_status()
            data = (resp.json().get("data") or [None])[0]
        if not data:
            return None
        weather = (data.get("weather") or [{}])[0]
        return {
            "temp_c": round(data.get("temp"), 1) if data.get("temp") is not None else None,
            "feels_c": round(data.get("feels_like"), 1) if data.get("feels_like") is not None else None,
            "humidity": data.get("humidity"),
            "wind_kph": _wind_kph(data.get("wind_speed")),
            "gust_kph": _wind_kph(data.get("wind_gust")),
            "wind_deg": data.get("wind_deg"),
            "wind_dir": _compass(data.get("wind_deg")),
            "precip_mm": (data.get("rain") or {}).get("1h") or (data.get("snow") or {}).get("1h"),
            "condition": weather.get("main"),
            "description": weather.get("description"),
        }
    except httpx.HTTPError:
        logger.warning("Weather lookup failed for (%s, %s @ %s)", lat, lon, when)
        return None


async def forecast_today(lat: float, lon: float) -> dict | None:
    """Today's outlook for the briefing: current, the next 12 hours, and
    the day's shape. Returns None when unavailable."""
    if not is_configured():
        return None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                BASE,
                params={
                    "lat": lat,
                    "lon": lon,
                    "exclude": "minutely,alerts",
                    "units": "metric",
                    "appid": settings.openweather_api_key,
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError:
        logger.warning("Forecast lookup failed for (%s, %s)", lat, lon)
        return None

    def compact(h: dict) -> dict:
        weather = (h.get("weather") or [{}])[0]
        return {
            "at": datetime.fromtimestamp(h["dt"], tz=timezone.utc).strftime("%H:%M"),
            "temp_c": round(h.get("temp"), 1) if isinstance(h.get("temp"), (int, float)) else None,
            "wind_kph": _wind_kph(h.get("wind_speed")),
            "gust_kph": _wind_kph(h.get("wind_gust")),
            "wind_dir": _compass(h.get("wind_deg")),
            "rain_chance": round((h.get("pop") or 0) * 100),
            "condition": weather.get("description"),
        }

    current = data.get("current") or {}
    daily = (data.get("daily") or [{}])[0]
    return {
        "now": compact(current) if current else None,
        "hours": [compact(h) for h in (data.get("hourly") or [])[:12]],
        "day": {
            "min_c": round(((daily.get("temp") or {}).get("min")), 1) if (daily.get("temp") or {}).get("min") is not None else None,
            "max_c": round(((daily.get("temp") or {}).get("max")), 1) if (daily.get("temp") or {}).get("max") is not None else None,
            "rain_chance": round((daily.get("pop") or 0) * 100),
            "summary": daily.get("summary"),
            "sunrise": datetime.fromtimestamp(daily["sunrise"], tz=timezone.utc).strftime("%H:%M") if daily.get("sunrise") else None,
            "sunset": datetime.fromtimestamp(daily["sunset"], tz=timezone.utc).strftime("%H:%M") if daily.get("sunset") else None,
        },
    }
