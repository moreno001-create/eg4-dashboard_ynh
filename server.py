"""EG4 solar dashboard - web server.

    python server.py            # then open http://127.0.0.1:8766

Reads an EG4 inverter (built for the 6000XP) in the background, keeps a minute-by-minute history, and serves a dashboard.
With a Wi-Fi dongle set up, live readings come straight from it on the home network every few seconds (local.py), and the
EG4 Monitor cloud takes over whenever the dongle can't be reached. History, alerts and grid charging always use the cloud. Read-only except for grid charging ("Quick Charge"), with an optional automatic stop
at a chosen battery level.

Environment variables (set by the YunoHost package):
    EG4_HOST, EG4_PORT   address to listen on (default 127.0.0.1:8766)
    EG4_DATA_DIR         where settings (incl. the EG4 password, mode 600) and history live (default ./data).
                         Setting it also marks the server as running behind a reverse proxy.
"""
import asyncio
import json
import re
import logging
import os
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

import eg4
import forecast as fc
import local
import storm as sw

HOST = os.environ.get("EG4_HOST", "127.0.0.1")
PORT = int(os.environ.get("EG4_PORT", "8766"))
HOSTED = "EG4_DATA_DIR" in os.environ
BASE = Path(__file__).parent
DATA = Path(os.environ.get("EG4_DATA_DIR") or BASE / "data")
SETTINGS = DATA / "settings.json"
HISTORY = DATA / "history.jsonl"
MONTHS = DATA / "months.json"
LIVE_SECS, SLOW_SECS, EVENT_SECS = 60, 300, 900
LOCAL_SECS = 5            # live readings from the dongle on the home network
LOCAL_RETRY_SECS = 60     # after the dongle drops: the cloud fills in, and the dongle is tried again this often
STALE_SECS = 600          # no fresh reading for this long -> show "not updating"
PV_MAX = 8000             # 6000XP: maximum solar input, for the MPPT card
KEEP_DAYS = 90
log = logging.getLogger("uvicorn.error")


# --------------------------------------------------------------------------- settings
def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def save_json(path, data, private=False):
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    if private:
        tmp.chmod(0o600)
    tmp.replace(path)


class State:
    settings = dict(account="", password="", serial="", demo=False, quick_target=None,
                    dongle_mac="", dongle_serial="", dongle_ip="")   # the Wi-Fi dongle, found by MAC (local.py)
    alerts, alerts_at, alert_error, hazards = [], 0, None, []   # storm watch
    client = None
    demo = eg4.DemoInverter()
    live = energy = battery = quick = None
    events = []
    inverters = []
    error = None
    last_ok = 0
    last_slow = last_events = 0
    last_hist = last_quick = 0
    quick_stale = False   # a charge was just stopped over Wi-Fi: ask the cloud again on the next round
    dongle = None         # local.Dongle while a dongle is set up
    source = None         # where the latest live reading came from: "local" or "cloud"
    local_error = None    # why the dongle couldn't be read (the cloud is filling in)
    local_failed_at = 0
    cloud_extra = {}      # firmware / rating / status from the cloud, shown alongside local readings
    local_energy = local_bank = None
    local_quick = None    # grid-charge status read from the inverter itself (local.quick_status)
    cloud_energy = cloud_battery = None   # the cloud's figures, before the dongle's fresher ones are laid over them
    month_cache = {}      # "YYYY-MM" -> (fetched_at, rows) for the current month
    fc_cache = None       # (fetched_at, key, forecast)
    lock = asyncio.Lock()
    wake = asyncio.Event()
    log = []              # dashboard notices (quick-charge actions)


S = State()
S.settings = dict(S.settings, **load_json(SETTINGS, {}))


def cloud_ok():
    return bool(S.settings["account"] and S.settings["password"])


def local_on():
    """A dongle is set up (and an inverter serial is known, which every request to it names)."""
    return bool((S.settings.get("dongle_mac") or S.settings.get("dongle_serial")) and S.settings["serial"])


def configured():
    return cloud_ok() or local_on()


def get_dongle():
    st = S.settings
    d = S.dongle
    if d is None or d.mac != local.norm_mac(st["dongle_mac"]) or d.inverter_sn != st["serial"] or \
            (st.get("dongle_serial") and d.dongle_sn != st["dongle_serial"]):
        S.dongle = d = local.Dongle(st["dongle_mac"], st["serial"], st.get("dongle_serial", ""), st.get("dongle_ip", ""))
    return d


async def drop_dongle():
    if S.dongle:
        await S.dongle.close()
    S.dongle = None


def notice(msg):
    log.info(msg)
    S.log = (S.log + [dict(t=round(time.time()), msg=msg)])[-20:]


def plant_id():
    return next((i["plant_id"] for i in S.inverters if i["serial"] == S.settings["serial"]), None)


async def eg4_call(fn, *args):
    async with S.lock:
        if S.client is None:
            S.client = eg4.EG4Client(S.settings["account"], S.settings["password"])
        if not S.client.logged_in:
            S.inverters = await S.client.login()
            if not S.settings["serial"] and S.inverters:
                S.settings["serial"] = S.inverters[0]["serial"]
                save_json(SETTINGS, S.settings, private=True)
        return await asyncio.wait_for(fn(S.client, *args), 40)


# --------------------------------------------------------------------------- polling
def append_history(row):
    DATA.mkdir(parents=True, exist_ok=True)
    with HISTORY.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def prune_history():
    cut = time.time() - KEEP_DAYS * 86400
    try:
        lines = HISTORY.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    keep = [l for l in lines if _t(l) >= cut]
    if len(keep) < len(lines):
        HISTORY.write_text("".join(l + "\n" for l in keep), encoding="utf-8")


def _t(line):
    try:
        return json.loads(line)["t"]
    except (ValueError, KeyError, TypeError):
        return 0


async def read_local(now):
    """Live reading from the dongle. Returns False (and the cloud fills in) when it can't be reached."""
    if not local_on() or (S.local_failed_at and now - S.local_failed_at < LOCAL_RETRY_SECS):
        return False
    d = get_dongle()
    try:
        regs = await d.read_all()
    except local.LocalError as e:
        if S.local_error != str(e):
            log.warning("Wi-Fi dongle: %s%s", e, "; using EG4 Monitor until it's back" if cloud_ok() else "")
        S.local_error, S.local_failed_at = str(e), now
        return False
    if S.local_error or S.source == "cloud":
        log.info("Wi-Fi dongle back at %s", d.ip)
    S.local_error, S.local_failed_at = None, 0
    if (d.ip, d.dongle_sn) != (S.settings.get("dongle_ip"), S.settings.get("dongle_serial")):   # a new address (or first contact)
        S.settings.update(dongle_ip=d.ip, dongle_serial=d.dongle_sn)
        save_json(SETTINGS, S.settings, private=True)
    S.live = dict(local.norm_live(regs, now), **S.cloud_extra)
    S.local_energy, S.local_bank = local.norm_energy(regs), local.norm_bank(regs)
    S.local_quick = local.quick_status(d.holding, now)
    normal = local.normal_charge_settings(d.holding)
    if normal and normal != S.settings.get("charge_normal"):   # what a Wi-Fi stop puts back
        S.settings["charge_normal"] = normal
        save_json(SETTINGS, S.settings, private=True)
    return True


def merged_energy(cloud):
    """The dongle's counters (fresher) over the cloud's, keeping the cloud's figure wherever the dongle has none."""
    loc = S.local_energy if S.source == "local" else None
    if not loc:
        return cloud
    base = cloud or dict(today={}, total={})
    return {p: dict(base.get(p) or {}, **{k: v for k, v in loc[p].items() if v is not None}) for p in ("today", "total")}


def merged_battery(cloud):
    if S.source != "local" or not S.local_bank:
        return cloud
    base = cloud or dict(soc=None, v=None, power=None, status="", remain_ah=None, full_ah=None, current="", count=0, modules=[], bank=None)
    bank = dict(base.get("bank") or {}, **{k: v for k, v in S.local_bank.items() if v is not None})
    return dict(base, soc=S.live["soc"], v=S.live["v_bat"], power=S.live["battery"], bank=bank)


async def poll_once():
    """One round: live reading (Wi-Fi dongle first, else the cloud), then the slower cloud data when it's due."""
    sn, now = S.settings["serial"], time.time()
    if await read_local(now):
        S.source = "local"
    elif cloud_ok():
        rt = await eg4_call(lambda c: c.runtime(S.settings["serial"]))
        S.live = dict(eg4.norm_runtime(rt), source="cloud")
        S.cloud_extra = {k: S.live[k] for k in ("fw", "rating", "device_time") if S.live.get(k)}
        S.source = "cloud"
    else:
        raise local.LocalError(f"Couldn't read the Wi-Fi dongle: {S.local_error}")
    sn = S.settings["serial"]
    S.last_ok, S.error = now, None
    if now - S.last_hist >= LIVE_SECS - 1:   # the history keeps one row a minute, however often the dongle is read
        S.last_hist = now
        strings = {f"pv{s['n']}": s["w"] for s in S.live["pv_strings"]}
        append_history(dict({k: S.live[k] for k in ("t", "pv", "load", "soc", "battery", "grid", "inv")}, **strings))
    if cloud_ok():
        if now - S.last_slow >= SLOW_SECS or S.cloud_energy is None:
            S.last_slow = now
            S.cloud_energy = eg4.norm_energy(await eg4_call(lambda c: c.energy(sn)))
            S.cloud_battery = eg4.norm_battery(await eg4_call(lambda c: c.battery(sn)))
            if S.source == "local":   # firmware and rating only come from the cloud
                rt = eg4.norm_runtime(await eg4_call(lambda c: c.runtime(sn)))
                S.cloud_extra = {k: rt[k] for k in ("fw", "rating", "device_time") if rt.get(k)}
        if now - S.last_quick >= LIVE_SECS - 1 or S.quick is None or S.quick_stale:
            S.last_quick = now
            S.quick = eg4.norm_quick(await eg4_call(lambda c: c.quick_charge_status(sn)))
        if now - S.last_events >= EVENT_SECS:
            S.last_events = now
            try:
                S.events = eg4.norm_events(await eg4_call(lambda c: c.events(sn, plant_id())))
            except eg4.EG4Error as e:
                log.warning("Event list failed: %s", e)
    elif S.source == "local" and S.local_quick:   # no EG4 login: the inverter's own charge window says whether it's charging
        S.quick = S.local_quick
    S.quick_stale = False
    S.energy = merged_energy(S.cloud_energy)
    S.battery = merged_battery(S.cloud_battery)
    await auto_stop()


async def stop_grid_charge():
    """Stop grid charging: through EG4 Monitor when there's a login, over Wi-Fi when there isn't or EG4 fails.
    Returns how it went ("EG4 Monitor" or "Wi-Fi"); raises if neither worked."""
    errors = []
    if cloud_ok():
        try:
            await eg4_call(lambda c: c.quick_charge(S.settings["serial"], False))
            return "EG4 Monitor"
        except Exception as e:
            errors.append(f"EG4 Monitor: {e or type(e).__name__}")
            log.warning("Stopping the charge through EG4 failed (%s)%s", e, "; trying Wi-Fi" if local_on() else "")
    if local_on():
        try:
            changed = await get_dongle().stop_quick_charge(S.settings.get("charge_normal"))
            log.info("Grid charge stopped over Wi-Fi: %s", {r: f"{a} -> {b}" for r, (a, b) in changed.items() if a != b})
            S.local_quick = dict(active=False, remain_min=0, status="", until="")
            S.quick_stale = True
            return "Wi-Fi"
        except local.LocalError as e:
            errors.append(f"Wi-Fi: {e}")
    raise HTTPException(502, "Couldn't stop the charge. " + "; ".join(errors) if errors else
                        "Grid charging needs the EG4 login or the Wi-Fi dongle")


async def auto_stop():
    """Stop grid charging once the battery reaches the chosen level."""
    target = S.settings.get("quick_target")
    if target and (cloud_ok() or local_on()) and S.quick and S.quick["active"] and S.live and (S.live["soc"] or 0) >= target:
        try:
            how = await stop_grid_charge()
        except HTTPException as e:
            log.warning("Automatic stop at %s%%: %s", target, e.detail)
            return
        S.quick = dict(S.quick, active=False)
        S.settings["quick_target"] = None
        save_json(SETTINGS, S.settings, private=True)
        notice(f"Grid charging stopped automatically at {S.live['soc']}% (target {target}%)" + (" over Wi-Fi" if how == "Wi-Fi" else ""))


async def safe_storm_tick():
    try:
        await storm_tick()
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.warning("Storm watch: %r", e)


async def poller():
    last_prune, storm_task = 0, None
    while True:
        S.wake.clear()   # before reading: a "read now" that arrives during this round still wakes the next one
        if configured() and not S.settings["demo"]:
            try:
                await poll_once()
            except eg4.EG4AuthError as e:
                S.error = f"{e}. Check the EG4 login in Settings."
                await drop_client()
            except local.LocalError as e:
                S.error = str(e)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                S.error = f"Couldn't reach EG4 Monitor: {e or type(e).__name__}"
                log.warning(S.error)
        # storm watch runs on its own, so a slow weather service never delays reading the inverter
        if (configured() or S.settings["demo"]) and (storm_task is None or storm_task.done()):
            storm_task = asyncio.create_task(safe_storm_tick())
        if time.time() - last_prune > 86400:
            last_prune = time.time()
            prune_history()
        try:
            await asyncio.wait_for(S.wake.wait(), LOCAL_SECS if S.source == "local" else LIVE_SECS)
        except asyncio.TimeoutError:
            pass


async def drop_client():
    if S.client:
        await S.client.close()
    S.client = None


# --------------------------------------------------------------------------- history
def read_history(hours):
    cut, rows = time.time() - hours * 3600, []
    try:
        with HISTORY.open(encoding="utf-8") as f:
            for line in f:
                if _t(line) >= cut:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        pass
    except OSError:
        pass
    return rows


async def month_rows(y, m):
    """Daily energy for one month; finished months are cached on disk for good."""
    key = f"{y:04d}-{m:02d}"
    today = date.today()
    finished = (y, m) < (today.year, today.month)
    stored = load_json(MONTHS, {})
    if finished and key in stored and all(r.get("solar") is not None for r in stored[key]):  # older caches lacked solar
        return stored[key]
    hit = S.month_cache.get(key)
    if hit and time.time() - hit[0] < 1800:
        return hit[1]
    rows = eg4.norm_month(await eg4_call(lambda c: c.month(S.settings["serial"], y, m)), y, m)
    if finished and (today - date(y, m, 1)).days > 33:  # well past the month: its numbers won't change
        stored[key] = rows
        save_json(MONTHS, stored)
    S.month_cache[key] = (time.time(), rows)
    return rows


def months_back(n):
    d, out = date.today().replace(day=1), []
    for _ in range(n):
        out.append((d.year, d.month))
        d = (d - timedelta(days=1)).replace(day=1)
    return out[::-1]


# --------------------------------------------------------------------------- app
@asynccontextmanager
async def lifespan(_app):
    task = asyncio.create_task(poller())
    yield
    task.cancel()
    await drop_client()
    await drop_dongle()


app = FastAPI(title="EG4 dashboard", lifespan=lifespan)


@app.middleware("http")
async def same_origin_only(request: Request, call_next):
    """Block other websites from driving this API."""
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        allowed = {f"{HOST}:{PORT}", f"localhost:{PORT}"}
        if HOSTED:  # behind a reverse proxy, which only forwards its own domains
            allowed.add(request.headers.get("host"))
        if origin and urlparse(origin).netloc not in allowed:
            return JSONResponse({"detail": "Cross-origin request blocked"}, status_code=403)
    return await call_next(request)


class Login(BaseModel):
    account: str = ""
    password: str | None = None  # None keeps the saved password
    serial: str = ""


class Charge(BaseModel):
    start: bool
    target: int | None = None   # stop at this battery %
    minutes: int | None = None  # or charge for this long (newer firmware)


class Demo(BaseModel):
    on: bool


class Location(BaseModel):
    zip: str


@app.get("/")
async def index():
    return FileResponse(BASE / "index.html")


@app.get("/house.webp")
async def house_picture():
    return FileResponse(BASE / "house.webp", media_type="image/webp", headers={"Cache-Control": "max-age=86400"})


@app.get("/api/state")
async def state():
    now = time.time()
    if S.settings["demo"]:
        d = S.demo
        target = S.settings.get("quick_target")
        if d.quick and target and d.live(now)["soc"] >= target:  # same automatic stop as for a real inverter
            d.quick, S.settings["quick_target"] = False, None
            notice(f"Grid charging stopped automatically at {d.live(now)['soc']}% (target {target}%) (demo)")
        if d.quick and d.quick_until and now >= d.quick_until:
            d.quick = False
        quick = dict(active=d.quick, remain_min=max(0, round((d.quick_until - now) / 60)) if d.quick and d.quick_until else 0, status="")
        return dict(configured=True, demo=True, account="demo", serial="DEMO6000XP", inverters=[], error=None, stale=False,
                    live=d.live(now), energy=d.energy(now), battery=d.battery(now), quick=quick,
                    quick_target=S.settings.get("quick_target"), events=d.events(now), notices=S.log[::-1], hosted=HOSTED, pv_max=PV_MAX, storm=storm_payload())
    stale = bool(S.last_ok) and now - S.last_ok > STALE_SECS
    return dict(configured=configured(), demo=False, account=S.settings["account"], serial=S.settings["serial"],
                inverters=S.inverters, error=S.error, stale=stale, last_ok=S.last_ok or None,
                live=S.live, energy=S.energy, battery=S.battery, quick=S.quick, quick_target=S.settings.get("quick_target"),
                events=S.events, notices=S.log[::-1], hosted=HOSTED, pv_max=PV_MAX, storm=storm_payload(),
                connection=connection_payload())


@app.get("/api/live")
async def live_api():
    """The latest reading in a small, stable shape for other apps on this server (the water heater's solar dump load
    reads it instead of polling EG4 itself). Read-only. On YunoHost this service listens on 127.0.0.1 only, so other apps
    on the same machine reach it directly, without the YunoHost login."""
    now = time.time()
    if S.settings["demo"]:
        L, src, ok = S.demo.live(now), "demo", now
    else:
        L, src, ok = S.live, S.source, S.last_ok
    if not L:
        raise HTTPException(503, S.error or "No reading yet")
    keys = ("t", "pv", "soc", "charge", "discharge", "battery", "grid", "inv", "load", "v_bat", "status", "lost")
    return dict({k: L.get(k) for k in keys}, source=src, age=round(now - (ok or L["t"])),
                stale=bool(ok) and now - ok > STALE_SECS, interval=LOCAL_SECS if src == "local" else LIVE_SECS)


def connection_payload():
    st = S.settings
    return dict(source=S.source, dongle_mac=st.get("dongle_mac", ""), dongle_ip=st.get("dongle_ip", ""),
                dongle_serial=st.get("dongle_serial", ""), local_error=S.local_error, cloud=cloud_ok())


@app.get("/api/history")
async def history(hours: int = 24):
    hours = max(1, min(hours, 24 * 7))
    if S.settings["demo"]:
        return S.demo.history(time.time())
    return read_history(hours)


@app.get("/api/days")
async def days(n: int = 30):
    n = max(7, min(n, 62))
    if S.settings["demo"]:
        return S.demo.days(time.time(), n)
    if not cloud_ok():
        raise HTTPException(400, "History comes from EG4 Monitor: add the EG4 login in Settings")
    cut = (date.today() - timedelta(days=n - 1)).isoformat()
    rows = []
    for y, m in months_back(3):
        rows += [r for r in await month_rows(y, m) if r["date"] >= cut and r["date"] <= date.today().isoformat()]
    return rows


@app.get("/api/months")
async def months():
    if S.settings["demo"]:
        rows = S.demo.days(time.time(), 365)
        by = {}
        for r in rows:
            by.setdefault(r["date"][:7], []).append(r)
    else:
        if not cloud_ok():
            raise HTTPException(400, "History comes from EG4 Monitor: add the EG4 login in Settings")
        by = {f"{y:04d}-{m:02d}": await month_rows(y, m) for y, m in months_back(12)}
    out = []
    for key, rows in by.items():
        tot = {k: round(sum(r[k] or 0 for r in rows), 1) for k in eg4.MONTH_KEYS}
        out.append(dict(month=key, days=len(rows), **tot))
    return sorted(out, key=lambda r: r["month"])[-12:]


# --------------------------------------------------------------------------- weather & solar forecast
async def calibration_days():
    """Recent daily solar production, to calibrate the solar forecast against this system."""
    if S.settings["demo"]:
        return S.demo.days(time.time(), 31)
    if not cloud_ok():
        return []
    cut = (date.today() - timedelta(days=31)).isoformat()
    rows = []
    for y, m in months_back(2):
        rows += [r for r in await month_rows(y, m) if cut <= r["date"] < date.today().isoformat()]
    return rows


@app.get("/api/forecast")
async def get_forecast():
    loc = S.settings.get("location") or fc.DEFAULT_LOCATION
    key = (loc["lat"], loc["lon"], S.settings["demo"], S.settings["serial"])
    if S.fc_cache and S.fc_cache[1] == key and time.time() - S.fc_cache[0] < fc.CACHE_SECS:
        return S.fc_cache[2]
    try:
        om = await fc.fetch(loc["lat"], loc["lon"])
    except Exception as e:
        raise HTTPException(502, f"Couldn't get the weather forecast: {e or type(e).__name__}")
    try:
        days = await calibration_days()
    except Exception as e:  # no history yet: the forecast falls back to a typical system
        log.warning("Solar forecast calibration skipped: %r", e)
        days = []
    data = fc.build(om, loc, days)
    S.fc_cache = (time.time(), key, data)
    return data


@app.post("/api/location")
async def set_location(body: Location):
    z = body.zip.strip()
    if not re.fullmatch(r"[0-9]{5}", z):
        raise HTTPException(400, "Enter a 5-digit US ZIP code")
    try:
        loc = await fc.geocode_zip(z)
    except fc.ForecastError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"Couldn't look up that ZIP code: {e or type(e).__name__}")
    S.settings["location"] = loc
    save_json(SETTINGS, S.settings, private=True)
    S.fc_cache = None
    return await get_forecast()


@app.post("/api/connect")
async def connect(body: Login):
    """Log in with the details on screen and list the account's inverters (nothing is saved)."""
    c = eg4.EG4Client(body.account.strip() or S.settings["account"], body.password or S.settings["password"])
    try:
        return dict(inverters=await c.login())
    except eg4.EG4AuthError as e:
        raise HTTPException(400, f"{e}. Use the email and password of the EG4 Monitor app.")
    except Exception as e:
        raise HTTPException(502, f"Couldn't reach EG4 Monitor: {e or type(e).__name__}")
    finally:
        await c.close()


@app.post("/api/setup")
async def setup(body: Login):
    account = body.account.strip()
    password = body.password or (S.settings["password"] if account == S.settings["account"] else "")
    if not account or not password:
        raise HTTPException(400, "Enter the EG4 Monitor email and password")
    c = eg4.EG4Client(account, password)
    try:
        inverters = await c.login()  # validates the login before saving it
    except eg4.EG4AuthError as e:
        raise HTTPException(400, f"{e}. Use the email and password of the EG4 Monitor app.")
    except Exception as e:
        raise HTTPException(502, f"Couldn't reach EG4 Monitor: {e or type(e).__name__}")
    finally:
        await c.close()
    if not inverters:
        raise HTTPException(400, "That EG4 account has no inverter")
    serial = body.serial if any(i["serial"] == body.serial for i in inverters) else inverters[0]["serial"]
    if serial != S.settings["serial"]:
        S.live = S.energy = S.battery = S.quick = S.cloud_energy = S.cloud_battery = None
        S.events, S.month_cache = [], {}
        MONTHS.unlink(missing_ok=True)
    S.settings.update(account=account, password=password, serial=serial, demo=False)
    save_json(SETTINGS, S.settings, private=True)
    S.inverters, S.error, S.last_slow, S.last_events = inverters, None, 0, 0
    await drop_client()
    S.wake.set()
    return await state()


class DongleIn(BaseModel):
    mac: str = ""     # empty turns the Wi-Fi connection off
    ip: str = ""      # optional: the MAC is looked up from this address


@app.get("/api/dongle/find")
async def find_dongles():
    """Devices on the home network that answer on the dongle's port, with their MAC and (when they say) dongle serial."""
    ips = await local.sweep(local.local_networks())
    arp = {ip: mac for mac, ip in local.arp_table().items()}
    out = []
    for ip in ips:
        sn = await local.identify(ip, S.settings["serial"])
        out.append(dict(ip=ip, mac=arp.get(ip, ""), serial=sn))
    return out


@app.post("/api/dongle")
async def set_dongle(body: DongleIn):
    """Set up (or turn off) the Wi-Fi dongle. It's tried before saving, so a wrong MAC is caught here."""
    mac, ip = local.norm_mac(body.mac), body.ip.strip()
    if not mac and ip:   # by address: its MAC from the ARP table, or (where that can't be read) its dongle serial
        mac = local.mac_for_ip(ip)
    if not mac and not ip:
        if body.mac.strip():
            raise HTTPException(400, "That doesn't look like a MAC address (like f0:9e:9e:c9:f8:68)")
        S.settings.update(dongle_mac="", dongle_serial="", dongle_ip="")
        await drop_dongle()
        S.source, S.local_error = None, None
    else:
        if not S.settings["serial"]:
            raise HTTPException(400, "Sign in to EG4 Monitor first, so the inverter's serial number is known")
        d = local.Dongle(mac, S.settings["serial"], ip=ip)
        try:
            await d.read_all()
        except local.LocalError as e:
            raise HTTPException(400, f"Couldn't read the inverter through the dongle: {e}")
        finally:
            await d.close()
        await drop_dongle()
        S.settings.update(dongle_mac=mac, dongle_serial=d.dongle_sn, dongle_ip=d.ip)
        S.local_error, S.local_failed_at = None, 0
    save_json(SETTINGS, S.settings, private=True)
    S.wake.set()
    await asyncio.sleep(0.3)
    return await state()


@app.post("/api/refresh")
async def refresh():
    """Read the inverter now instead of waiting for the next minute."""
    S.wake.set()
    return dict(ok=True)


@app.post("/api/forget")
async def forget():
    """Remove the saved EG4 login (history is kept)."""
    S.settings.update(account="", password="", demo=False, quick_target=None)
    save_json(SETTINGS, S.settings, private=True)
    S.live = S.energy = S.battery = S.quick = S.cloud_energy = S.cloud_battery = None
    S.error = None
    await drop_client()
    return await state()


@app.post("/api/demo")
async def demo(body: Demo):
    S.settings["demo"] = body.on
    if not body.on:
        S.demo.quick = False
    save_json(SETTINGS, S.settings, private=True)
    S.wake.set()
    return await state()


async def quick(start, minutes=None, target=None, why=""):
    """Start or stop EG4 Quick Charge (grid charging), with an optional automatic stop at `target` %."""
    S.settings["quick_target"] = target if start else None
    if S.settings["demo"]:
        S.demo.quick, S.demo.quick_since = start, time.time()
        S.demo.quick_until = time.time() + minutes * 60 if minutes else 0
    elif not start:   # stopping also works over Wi-Fi
        how = await stop_grid_charge()
        if how == "EG4 Monitor":
            try:
                S.quick = eg4.norm_quick(await eg4_call(lambda c: c.quick_charge_status(S.settings["serial"])))
            except eg4.EG4Error:
                S.quick = dict(S.quick or {}, active=False)
        else:
            S.quick = dict(S.quick or {}, active=False, remain_min=0)
            why = why or "Grid charging stopped over Wi-Fi"
    else:
        if not cloud_ok() or not S.settings["serial"]:
            raise HTTPException(400, "Starting a grid charge goes through EG4 Monitor: set up the EG4 login first")
        try:
            await eg4_call(lambda c: c.quick_charge(S.settings["serial"], start, minutes))
            S.quick = eg4.norm_quick(await eg4_call(lambda c: c.quick_charge_status(S.settings["serial"])))
        except eg4.EG4Error as e:
            raise HTTPException(502, str(e))
    save_json(SETTINGS, S.settings, private=True)
    notice(why or ((("Grid charging started" + (f" for {minutes} min" if minutes else "") + (f", stops at {target}%" if target else ""))
                    if start else "Grid charging stopped") + (" (demo)" if S.settings["demo"] else "")))


# --------------------------------------------------------------------------- storm watch
def storm_cfg():
    return dict(sw.DEFAULTS, **(S.settings.get("storm") or {}))


def quick_active():
    return S.demo.quick if S.settings["demo"] else bool(S.quick and S.quick["active"])


def current_soc():
    L = S.demo.live(time.time()) if S.settings["demo"] else S.live
    return L["soc"] if L else None


async def storm_tick():
    """Refresh alerts and hazards; top up the battery before a storm (runs every minute)."""
    cfg, now = storm_cfg(), time.time()
    loc = S.settings.get("location") or fc.DEFAULT_LOCATION
    if cfg["use_alerts"] and now - S.alerts_at > sw.ALERT_SECS:
        S.alerts_at = now
        try:
            S.alerts, S.alert_error = await sw.fetch_alerts(loc["lat"], loc["lon"]), None
        except Exception as e:
            S.alert_error = f"Couldn't get weather alerts: {e or type(e).__name__}"
    try:
        hours = (await get_forecast())["hours"]
    except Exception:
        hours = []
    S.hazards = sw.hazards(S.alerts if cfg["use_alerts"] else [], hours, cfg, now)
    st = S.settings.setdefault("storm_state", dict(charging=None, handled=[]))
    if st["charging"]:                         # a storm charge we started: watch it finish
        if not quick_active():
            soc = current_soc()
            if soc is not None and soc >= cfg["target"] - 1:
                notice(f"Storm watch: battery at {soc}%, ready for the storm")
            else:
                notice("Storm watch: the charge was stopped before the target; it won't restart for this storm")
            st["handled"] = (st["handled"] + [st["charging"]])[-50:]
            st["charging"] = None
            save_json(SETTINGS, S.settings, private=True)
        return
    trig = sw.next_trigger(S.hazards, cfg, now) if cfg["enabled"] else None
    if not trig or trig["key"] in st["handled"]:
        return
    soc = current_soc()
    if soc is None or quick_active():          # no reading yet, or the owner is already charging
        return
    when = "now" if trig["start"] <= now else "at " + datetime.fromtimestamp(trig["start"]).strftime("%I:%M %p").lstrip("0")
    if soc >= cfg["target"] - 2:
        st["handled"] = (st["handled"] + [trig["key"]])[-50:]
        save_json(SETTINGS, S.settings, private=True)
        return notice(f"Storm watch: {trig['title']} {when}; battery already at {soc}%")
    await quick(True, None, cfg["target"], f"Storm watch: {trig['title']} {when}. Charging the battery to {cfg['target']}% from the grid")
    st["charging"] = trig["key"]
    save_json(SETTINGS, S.settings, private=True)


def storm_payload():
    cfg, now, st = storm_cfg(), time.time(), S.settings.get("storm_state") or {}
    trig = sw.next_trigger(S.hazards, cfg, now)
    hz = [dict(h, soon=bool(trig and h["key"] == trig["key"])) for h in S.hazards[:6]]
    if not cfg["enabled"]:
        status = "Off"
    elif st.get("charging"):
        status = f"Charging to {cfg['target']}% before the storm"
    elif trig and trig["key"] in (st.get("handled") or []):
        status = "Ready: battery topped up for the coming storm"
    elif trig:
        status = "Storm ahead: getting ready"
    elif S.hazards:
        status = f"Watching: {S.hazards[0]['title']}"
    else:
        status = "No storms ahead"
    return dict(settings=cfg, status=status, hazards=hz, charging=bool(st.get("charging")), alert_error=S.alert_error,
                checked=round(S.alerts_at) or None)


class StormIn(BaseModel):
    enabled: bool | None = None
    target: int | None = None
    lead_hours: int | None = None
    use_alerts: bool | None = None
    use_thunder: bool | None = None
    use_wind: bool | None = None
    gust_mph: int | None = None


@app.post("/api/storm")
async def storm_settings(body: StormIn):
    new = {k: v for k, v in body.model_dump().items() if v is not None}
    for k, lo, hi in (("target", 50, 100), ("lead_hours", 1, 48), ("gust_mph", 20, 120)):
        if k in new and not lo <= new[k] <= hi:
            raise HTTPException(400, f"{k} must be between {lo} and {hi}")
    S.settings["storm"] = dict(storm_cfg(), **new)
    save_json(SETTINGS, S.settings, private=True)
    if "enabled" in new:
        notice("Storm watch on" if new["enabled"] else "Storm watch off")
    try:
        await storm_tick()
    except Exception as e:
        log.warning("Storm watch: %r", e)
    return await state()


@app.post("/api/charge")
async def charge(body: Charge):
    """Start or stop charging the battery from the grid (EG4 "Quick Charge")."""
    if body.target is not None and not 10 <= body.target <= 100:
        raise HTTPException(400, "Stop level must be between 10 and 100%")
    if body.minutes is not None and not 1 <= body.minutes <= 720:
        raise HTTPException(400, "Duration must be between 1 minute and 12 hours")
    await quick(body.start, body.minutes if body.start else None, body.target if body.start else None)
    return await state()


if __name__ == "__main__":
    if not HOSTED:  # running on the desktop
        threading.Timer(1.2, lambda: webbrowser.open(f"http://{HOST}:{PORT}")).start()
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
