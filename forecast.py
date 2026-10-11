"""Weather and solar-yield forecast for the dashboard.

Weather comes from Open-Meteo (free, no key; https://open-meteo.com), US ZIP codes are looked up with Zippopotam
(https://zippopotam.us). The solar estimate is calibrated against the owner's own production: the last month of daily
solar kWh is compared with that month's measured sunlight (shortwave radiation) at the same spot, which gives this system's
kWh per kWh/m² of sunlight - panel size, angles, shading and losses included. A high percentile of the daily ratios is used
because on days the battery is full the inverter holds solar back, which would otherwise drag the estimate down.
"""
import statistics
import time
from datetime import datetime, timezone

import aiohttp

DEFAULT_LOCATION = dict(zip="90241", name="Downey, CA", lat=33.9416, lon=-118.1306)
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
CACHE_SECS = 1800
DEFAULT_FACTOR = 3.2   # kWh per kWh/m² (~4 kWp at 80%) until there's history to calibrate from
MIN_CAL_DAYS = 5

# WMO weather codes -> (text, icon)
CODES = {0: ("Clear", "sun"), 1: ("Mostly clear", "sun"), 2: ("Partly cloudy", "partly"), 3: ("Cloudy", "cloud"),
         45: ("Fog", "fog"), 48: ("Fog", "fog"), 51: ("Light drizzle", "rain"), 53: ("Drizzle", "rain"), 55: ("Drizzle", "rain"),
         56: ("Freezing drizzle", "rain"), 57: ("Freezing drizzle", "rain"), 61: ("Light rain", "rain"), 63: ("Rain", "rain"),
         65: ("Heavy rain", "rain"), 66: ("Freezing rain", "rain"), 67: ("Freezing rain", "rain"), 71: ("Light snow", "snow"),
         73: ("Snow", "snow"), 75: ("Heavy snow", "snow"), 77: ("Snow grains", "snow"), 80: ("Showers", "rain"),
         81: ("Showers", "rain"), 82: ("Heavy showers", "rain"), 85: ("Snow showers", "snow"), 86: ("Snow showers", "snow"),
         95: ("Thunderstorm", "storm"), 96: ("Thunderstorm", "storm"), 99: ("Thunderstorm", "storm")}


class ForecastError(Exception):
    pass


async def _get_json(url, params=None):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as http:
        async with http.get(url, params=params) as r:
            if r.status == 404:
                raise ForecastError("not found")
            r.raise_for_status()
            return await r.json(content_type=None)


async def geocode_zip(zipcode):
    try:
        j = await _get_json(f"https://api.zippopotam.us/us/{zipcode}")
    except ForecastError:
        raise ForecastError(f"Couldn't find US ZIP code {zipcode}")
    p = (j.get("places") or [None])[0]
    if not p:
        raise ForecastError(f"Couldn't find US ZIP code {zipcode}")
    return dict(zip=zipcode, name=f"{p['place name']}, {p['state abbreviation']}", lat=float(p["latitude"]), lon=float(p["longitude"]))


async def fetch(lat, lon):
    return await _get_json(OPEN_METEO, dict(
        latitude=lat, longitude=lon, timezone="auto", forecast_days=2, past_days=31,
        temperature_unit="fahrenheit", wind_speed_unit="mph", precipitation_unit="inch",
        current="temperature_2m,apparent_temperature,weather_code,cloud_cover,is_day,wind_speed_10m,relative_humidity_2m",
        hourly="temperature_2m,weather_code,cloud_cover,shortwave_radiation,precipitation_probability,is_day,wind_gusts_10m",
        daily="weather_code,temperature_2m_max,temperature_2m_min,sunrise,sunset,shortwave_radiation_sum,precipitation_probability_max,uv_index_max"))


def _epoch(local_iso, offset):
    return round(datetime.fromisoformat(local_iso).replace(tzinfo=timezone.utc).timestamp() - offset)


def calibrate(daily_sun, actual_days):
    """daily_sun: {date: kWh/m²}, actual_days: [{date, solar}] -> (factor, days used)."""
    ratios = [d["solar"] / daily_sun[d["date"]] for d in actual_days
              if d.get("solar") and daily_sun.get(d["date"], 0) > 1 and d["solar"] > .5]
    if len(ratios) < MIN_CAL_DAYS:
        return DEFAULT_FACTOR, 0
    ratios.sort()
    return round(ratios[min(len(ratios) - 1, int(len(ratios) * .8))], 3), len(ratios)


def build(om, location, actual_days):
    off = om.get("utc_offset_seconds", 0)
    D, H, C = om["daily"], om["hourly"], om.get("current", {})
    sun = {d: (s or 0) / 3.6 for d, s in zip(D["time"], D["shortwave_radiation_sum"])}  # MJ/m² -> kWh/m²
    factor, cal_days = calibrate(sun, actual_days)
    today = D["time"][-2]
    days = []
    for i in (-2, -1):
        code = D["weather_code"][i]
        days.append(dict(date=D["time"][i], code=code, text=CODES.get(code, ("", "cloud"))[0], icon=CODES.get(code, ("", "cloud"))[1],
                         hi=D["temperature_2m_max"][i], lo=D["temperature_2m_min"][i], rain=D["precipitation_probability_max"][i],
                         uv=D["uv_index_max"][i], sunrise=_epoch(D["sunrise"][i], off), sunset=_epoch(D["sunset"][i], off),
                         sun_kwh_m2=round(sun[D["time"][i]], 2), solar_kwh=round(factor * sun[D["time"][i]], 1)))
    hours = []
    for i, ts in enumerate(H["time"]):
        if ts[:10] < today:
            continue
        code = H["weather_code"][i]
        hours.append(dict(t=_epoch(ts, off), temp=H["temperature_2m"][i], code=code, icon=CODES.get(code, ("", "cloud"))[1],
                          cloud=H["cloud_cover"][i], rain=H["precipitation_probability"][i], ghi=H["shortwave_radiation"][i],
                          solar_w=round(factor * (H["shortwave_radiation"][i] or 0)),  # W/m² x factor = W
                          day=bool(H["is_day"][i]), gust=(H.get("wind_gusts_10m") or [None] * len(H["time"]))[i]))
    cur_code = C.get("weather_code")
    current = dict(temp=C.get("temperature_2m"), feels=C.get("apparent_temperature"), code=cur_code,
                   text=CODES.get(cur_code, ("", "cloud"))[0], icon=CODES.get(cur_code, ("", "cloud"))[1],
                   cloud=C.get("cloud_cover"), wind=C.get("wind_speed_10m"), humidity=C.get("relative_humidity_2m"), is_day=bool(C.get("is_day", 1)))
    return dict(location=location, current=current, days=days, hours=hours, factor=factor, calibrated_days=cal_days,
                fetched=round(time.time()), source="Open-Meteo")
