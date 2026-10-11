"""Storm watch: top the battery up from the grid before severe weather, in case the grid goes down.

Two sources, both for the dashboard's weather location:
  - National Weather Service active alerts (api.weather.gov, free, no key): watches and warnings for thunderstorms,
    tropical storms, high wind, winter storms, floods, and Red Flag (fire weather, when utilities may cut power).
  - The hourly forecast (Open-Meteo, from forecast.py): thunderstorms, or wind gusts above a chosen speed.
When a hazard is within the chosen lead time and the battery is below the target, the server starts EG4 Quick Charge with an
automatic stop at the target. It charges once per storm and never restarts a charge the owner stopped.
"""
import time
from datetime import datetime

import aiohttp

NWS_URL = "https://api.weather.gov/alerts/active"
NWS_UA = "(eg4-dashboard, self-hosted home solar monitor)"
ALERT_SECS = 600
LOOK_HOURS = 48
DEFAULTS = dict(enabled=False, target=100, lead_hours=12, use_alerts=True, use_thunder=True, use_wind=True, gust_mph=45)
# NWS event names that can take the grid down (watches and warnings; advisories and statements are left out)
STORM_WORDS = ("thunderstorm", "tornado", "hurricane", "tropical storm", "typhoon", "high wind", "extreme wind", "winter storm",
               "blizzard", "ice storm", "red flag", "flood", "storm surge", "dust storm")


class StormError(Exception):
    pass


def _ts(iso):
    try:
        return round(datetime.fromisoformat(iso).timestamp()) if iso else None
    except ValueError:
        return None


async def fetch_alerts(lat, lon):
    headers = {"User-Agent": NWS_UA, "Accept": "application/geo+json"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as http:
        async with http.get(NWS_URL, params={"point": f"{lat:.4f},{lon:.4f}"}, headers=headers) as r:
            if r.status in (400, 404):
                raise StormError("weather alerts are only available for US locations")
            r.raise_for_status()
            j = await r.json(content_type=None)
    out = []
    for f in j.get("features", []):
        p = f.get("properties", {})
        out.append(dict(id=p.get("id") or f.get("id"), event=p.get("event") or "", severity=p.get("severity") or "",
                        headline=p.get("headline") or "", onset=_ts(p.get("onset") or p.get("effective")),
                        ends=_ts(p.get("ends") or p.get("expires"))))
    return out


def relevant_alert(a):
    e = a["event"].lower()
    return any(w in e for w in STORM_WORDS) and (e.endswith("warning") or e.endswith("watch"))


def hazards(alerts, hours, cfg, now):
    """Upcoming hazards: [{key, title, source, start, end}] sorted by start."""
    out = []
    if cfg["use_alerts"]:
        for a in alerts:
            if relevant_alert(a) and (a["ends"] or now + 1) > now:
                out.append(dict(key="nws:" + str(a["id"]), title=a["event"], detail=a["headline"], source="Weather service",
                                start=a["onset"] or now, end=a["ends"] or (a["onset"] or now) + 6 * 3600))
    # forecast: group consecutive stormy hours into one hazard
    run = None
    for h in sorted(hours, key=lambda h: h["t"]):
        if h["t"] < now - 3600 or h["t"] > now + LOOK_HOURS * 3600:
            continue
        reasons = []
        if cfg["use_thunder"] and (h.get("code") or 0) >= 95:
            reasons.append("Thunderstorms")
        if cfg["use_wind"] and (h.get("gust") or 0) >= cfg["gust_mph"]:
            reasons.append(f"Gusts {round(h['gust'])} mph")
        if reasons:
            if run and h["t"] - run["end"] <= 3600:
                run["end"] = h["t"] + 3600
                run["why"].update(reasons)
            else:
                run = dict(start=h["t"] - 3600, end=h["t"] + 3600, why=set(reasons))
                out.append(run)
        elif run and h["t"] - run["end"] > 3600:
            run = None
    for hz in out:
        if "why" in hz:
            why = sorted(hz.pop("why"))
            hz.update(key=f"fc:{hz['start']}", title=" and ".join(why) + " forecast", detail="", source="Forecast")
    return sorted(out, key=lambda h: h["start"])


def next_trigger(hz, cfg, now):
    """The first hazard close enough to act on, or None."""
    for h in hz:
        if h["end"] > now and h["start"] - now <= cfg["lead_hours"] * 3600:
            return h
    return None
