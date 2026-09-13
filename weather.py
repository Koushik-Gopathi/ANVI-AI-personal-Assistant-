"""Weather for ANVI via Open-Meteo (free, no API key)."""

import os

from net import http

ANVI_LOCATION = os.getenv("ANVI_LOCATION", "").strip()  # e.g. "Hyderabad"; blank = detect from IP

_location_cache: dict | None = None

WEATHER_CODES = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog", 51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "heavy freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "violent showers", 85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "severe thunderstorm with hail",
}


def home_location() -> dict:
    """User's default location: ANVI_LOCATION from .env, else a one-time IP lookup."""
    global _location_cache
    if _location_cache:
        return _location_cache
    if ANVI_LOCATION:
        _location_cache = geocode(ANVI_LOCATION)
    else:
        try:
            ip = http.get("http://ip-api.com/json/?fields=city,regionName,country,lat,lon", timeout=8).json()
            _location_cache = {
                "name": ", ".join(p for p in (ip.get("city"), ip.get("country")) if p),
                "lat": ip["lat"],
                "lon": ip["lon"],
            }
        except Exception as e:  # noqa: BLE001
            print("Location lookup failed:", e)
            _location_cache = {}
    return _location_cache


def geocode(place: str) -> dict:
    r = http.get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": place, "count": 1, "language": "en"},
        timeout=10,
    )
    r.raise_for_status()
    results = r.json().get("results") or []
    if not results:
        return {}
    g = results[0]
    name = ", ".join(p for p in (g.get("name"), g.get("admin1"), g.get("country")) if p)
    return {"name": name, "lat": g["latitude"], "lon": g["longitude"]}


def get_weather(location: str = "") -> dict:
    loc = geocode(location) if location else home_location()
    if not loc:
        return {"error": f"Could not find location '{location}'" if location else "Location unknown; ask the user for a city."}

    r = http.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": loc["lat"],
            "longitude": loc["lon"],
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
            "hourly": "temperature_2m,precipitation_probability,weather_code",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset",
            "timezone": "auto",
            "forecast_days": 3,
        },
        timeout=10,
    )
    r.raise_for_status()
    d = r.json()
    cur = d["current"]
    now = cur["time"]
    hourly = d["hourly"]
    start = next((i for i, t in enumerate(hourly["time"]) if t >= now[:13]), 0)
    next_hours = [
        {
            "time": hourly["time"][i][11:16],
            "temp_c": hourly["temperature_2m"][i],
            "rain_chance": hourly["precipitation_probability"][i],
            "sky": WEATHER_CODES.get(hourly["weather_code"][i], "unknown"),
        }
        for i in range(start, min(start + 24, len(hourly["time"])), 3)
    ]
    daily = d["daily"]
    days = [
        {
            "date": daily["time"][i],
            "sky": WEATHER_CODES.get(daily["weather_code"][i], "unknown"),
            "high_c": daily["temperature_2m_max"][i],
            "low_c": daily["temperature_2m_min"][i],
            "rain_chance": daily["precipitation_probability_max"][i],
            "sunset": daily["sunset"][i][11:16],
        }
        for i in range(len(daily["time"]))
    ]
    return {
        "location": loc["name"],
        "local_time": now,
        "current": {
            "temp_c": cur["temperature_2m"],
            "feels_like_c": cur["apparent_temperature"],
            "humidity": cur["relative_humidity_2m"],
            "wind_kmh": cur["wind_speed_10m"],
            "sky": WEATHER_CODES.get(cur["weather_code"], "unknown"),
        },
        "next_24h_every_3h": next_hours,
        "daily": days,
    }
