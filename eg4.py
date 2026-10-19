"""EG4 Monitor cloud client (monitor.eg4electronics.com) and unit conversion.

The calls mirror pylxpweb (https://github.com/joyfulhouse/pylxpweb, MIT), the library behind the EG4 Home Assistant
integration. It isn't used directly because it needs Python 3.12+ and YunoHost 12 ships 3.11.

Raw API units (from pylxpweb's scaling tables): powers in W; energies in 0.1 kWh; vBat, vpv*, vacr and module current in
0.1 V / 0.1 A; module totalVoltage in 0.01 V; cell voltages in mV; module temperatures in 0.1 C; fac in 0.01 Hz;
tinner and tBat in C.
"""
import math
import random
import time
from datetime import date, datetime, timedelta

import aiohttp

EG4_URL = "https://monitor.eg4electronics.com"


class EG4Error(Exception):
    pass


class EG4AuthError(EG4Error):
    pass


class EG4Client:
    LOGIN = "/WManage/api/login"

    def __init__(self, account, password, base_url=None):
        self.account, self.password, self.base = account, password, base_url or EG4_URL
        self.http = None
        self.logged_in = False
        self.inverters = []  # [{serial, plant, plant_id, rating}]

    async def _post(self, path, data, relogin=True):
        if self.http is None or self.http.closed:
            self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25))
        try:
            async with self.http.post(self.base + path, data=data, headers={"Accept": "application/json"}) as r:
                if r.status == 401:
                    raise aiohttp.ContentTypeError(r.request_info, r.history)
                r.raise_for_status()
                j = await r.json()
        except aiohttp.ContentTypeError:  # session expired: the portal answers with its login page
            if relogin:
                await self.login()
                return await self._post(path, data, relogin=False)
            raise EG4AuthError("EG4 rejected the session")
        if isinstance(j, dict) and j.get("success") is False:
            msg = j.get("msg") or j.get("message") or "request failed"
            raise (EG4AuthError if path == self.LOGIN else EG4Error)(f"EG4: {msg}")
        return j

    async def login(self):
        self.logged_in = False
        j = await self._post(self.LOGIN, dict(account=self.account, password=self.password, language="ENGLISH"), relogin=False)
        self.inverters = [dict(serial=i["serialNum"], plant=p.get("name", ""), plant_id=p.get("plantId"), rating=i.get("powerRating"))
                          for p in j.get("plants", []) for i in p.get("inverters", []) if i.get("serialNum")]
        self.logged_in = True
        return self.inverters

    async def call(self, path, data):
        if not self.logged_in:
            await self.login()
        return await self._post(path, data)

    async def runtime(self, sn):
        return await self.call("/WManage/api/inverter/getInverterRuntime", {"serialNum": sn})

    async def energy(self, sn):
        return await self.call("/WManage/api/inverter/getInverterEnergyInfo", {"serialNum": sn})

    async def battery(self, sn):
        return await self.call("/WManage/api/battery/getBatteryInfo", {"serialNum": sn})

    async def month(self, sn, year, month):
        return await self.call("/WManage/api/inverterChart/monthColumn", {"serialNum": sn, "year": year, "month": month})

    async def events(self, sn, plant_id, rows=30):
        return await self.call("/WManage/api/analyze/event/list",
                               {"page": 1, "rows": rows, "plantId": plant_id, "serialNum": sn, "eventText": "_all"})

    async def quick_charge_status(self, sn):
        return await self.call("/WManage/web/config/quickCharge/getStatusInfo", {"inverterSn": sn})

    async def quick_charge(self, sn, start, minute=None):
        """Start/stop charging from the grid. `minute` (newer firmware) charges for that long, then stops."""
        data = {"inverterSn": sn, "clientType": "WEB"}
        if start and minute:
            data["minute"] = int(minute)
        return await self.call("/WManage/web/config/quickCharge/" + ("start" if start else "stop"), data)

    async def read_params(self, sn, start, count):
        """The inverter's settings (holding registers start..start+count-1) as EG4 decodes them for this model: a flat dict of
        EG4's parameter names (HOLD_*, FUNC_*, BIT_*) to values. This is what EG4 Monitor's Remote Set page shows. The portal
        asks the inverter through the dongle, so it takes a few seconds; at most 127 registers per call."""
        j = await self.call("/WManage/web/maintain/remoteRead/read",
                            {"inverterSn": sn, "startRegister": start, "pointNumber": count, "autoRetry": "true"})
        return {k: v for k, v in j.items() if k.isupper() or k.startswith("_")} if isinstance(j, dict) else {}

    async def close(self):
        if self.http and not self.http.closed:
            await self.http.close()


# --------------------------------------------------------------------------- units
def _n(d, k, scale=1, nd=1):
    v = d.get(k)
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return round(v / scale, nd) if scale != 1 else (int(v) if v == int(v) else round(v, nd))


def norm_runtime(rt):
    g = lambda k: _n(rt, k) or 0
    # the 6000XP has two MPPT inputs; EG4 also reports a stray voltage on a third, so extra inputs only show when they produce
    pv_strings = [dict(n=i, w=g(f"ppv{i}"), v=_n(rt, f"vpv{i}", 10)) for i in (1, 2, 3)
                  if (rt.get(f"ppv{i}") is not None or rt.get(f"vpv{i}") is not None) and (i <= 2 or g(f"ppv{i}") > 0)]
    pv = g("ppv") or sum(s["w"] for s in pv_strings)
    inv = max(g("pinv"), g("peps"))
    load = g("consumptionPower") or inv  # 6000XP: home loads sit on the inverter output
    return dict(
        t=round(time.time()), pv=pv, pv_strings=pv_strings,
        soc=_n(rt, "soc"), v_bat=_n(rt, "vBat", 10), charge=g("pCharge"), discharge=g("pDisCharge"),
        battery=g("pCharge") - g("pDisCharge"),          # + charging, - discharging
        grid=g("pToUser") - g("pToGrid"),                # + importing, - exporting
        inv=inv, load=load, rectifier=g("prec"),
        v_ac=_n(rt, "vacr", 10), hz=_n(rt, "fac", 100, 2), t_inner=_n(rt, "tinner"), t_bat=_n(rt, "tBat"),
        v_eps=_n(rt, "vepsr", 10), hz_eps=_n(rt, "feps", 100, 2), bus1=_n(rt, "vBus1", 10), bus2=_n(rt, "vBus2", 10),
        t_rad1=_n(rt, "tradiator1"), t_rad2=_n(rt, "tradiator2"),
        status=rt.get("statusText") or "", lost=bool(rt.get("lost")), fw=rt.get("fwCode") or "",
        rating=rt.get("powerRatingText") or "", device_time=rt.get("deviceTime") or "")


ENERGY_KEYS = dict(solar="todayYielding", charged="todayCharging", discharged="todayDischarging",
                   imported="todayImport", exported="todayExport", used="todayUsage")


def norm_energy(e):
    out = dict(today={k: _n(e, v, 10) for k, v in ENERGY_KEYS.items()},
               total={k: _n(e, v.replace("today", "total"), 10) for k, v in ENERGY_KEYS.items()})
    return out


def norm_battery(b):
    mods = []
    for m in b.get("batteryArray") or []:
        mods.append(dict(sn=m.get("batterySn") or m.get("batteryKey") or "", index=m.get("batIndex"), lost=bool(m.get("lost")),
                         soc=_n(m, "soc"), soh=_n(m, "soh"), v=_n(m, "totalVoltage", 100, 2), a=_n(m, "current", 10),
                         cycles=_n(m, "cycleCnt"), cell_max=_n(m, "batMaxCellVoltage", 1000, 3), cell_min=_n(m, "batMinCellVoltage", 1000, 3),
                         t_max=_n(m, "batMaxCellTemp", 10), t_min=_n(m, "batMinCellTemp", 10),
                         remain_ah=_n(m, "currentRemainCapacity"), full_ah=_n(m, "currentFullCapacity"), fw=m.get("fwVersionText") or ""))
    # bank-wide cell extremes: what the owner's batteries report when there's no per-module data
    bank = dict(cell_max=_n(b, "globalMaxCellVoltage", 1000, 3), cell_min=_n(b, "globalMinCellVoltage", 1000, 3),
                t_max=_n(b, "globalMaxCellTemp", 10), t_min=_n(b, "globalMinCellTemp", 10), cycles=_n(b, "bmsCycleCnt"),
                v=_n(b, "totalVoltageText"), a=_n(b, "currentText")) if b.get("hasCellExtremes") or b.get("globalMaxCellVoltage") else None
    return dict(soc=_n(b, "soc"), v=_n(b, "vBat", 10), power=_n(b, "batPower"), status=b.get("batStatus") or "",
                remain_ah=_n(b, "remainCapacity") or None, full_ah=_n(b, "fullCapacity") or None, current=b.get("currentText") or "",
                count=b.get("totalNumber") or len(mods), modules=mods, bank=bank)


MONTH_KEYS = dict(solar="ePvDay", used="eConsumptionDay", imported="eToUserDay", exported="eToGridDay",
                  charged="eChgDay", discharged="eDisChgDay")
PV_DAY_KEYS = [f"ePv{i}Day" for i in range(1, 9)]  # the 6000XP has no ePvDay, only one total per input


def norm_month(j, year, month):
    rows = j.get("data") if isinstance(j, dict) else None
    out = []
    for i, row in enumerate(rows or []):
        if not isinstance(row, dict):
            continue
        day = row.get("day") if isinstance(row.get("day"), int) and 1 <= row.get("day") <= 31 else i + 1
        vals = {k: _n(row, v, 10) for k, v in MONTH_KEYS.items()}
        if vals["solar"] is None:
            vals["solar"] = round(sum(_n(row, k, 10) or 0 for k in PV_DAY_KEYS), 1)
        out.append(dict(date=date(year, month, day).isoformat(), **vals))
    return out


def norm_events(j):
    out = []
    for e in (j.get("rows") if isinstance(j, dict) else None) or []:
        out.append(dict(code=e.get("event") or "", type=(e.get("eventType") or "").upper(), type_text=e.get("eventTypeText") or "",
                        text=e.get("eventText") or "", start=e.get("startTime") or "", end=e.get("endTime") or "",
                        open=(e.get("status") or "").upper() == "OPEN", hours=e.get("faultDuration")))
    return out


def norm_quick(q):
    # remainTimeBeforeQuickChargeStop is in SECONDS (seen on the owner's 6000XP: 3428 right after starting a 60-minute charge)
    secs = q.get("remainTimeBeforeQuickChargeStop") or 0
    return dict(active=bool(q.get("hasUnclosedQuickChargeTask")), remain_min=round(secs / 60) if secs else 0,
                status=q.get("unclosedQuickChargeTaskStatus") or "")


# --------------------------------------------------------------------------- demo
class DemoInverter:
    """A plausible 6000XP with a 14.3 kWh battery bank, for trying the dashboard without an account."""

    def __init__(self):
        self.quick = False
        self.quick_since = 0
        self.quick_until = 0

    @staticmethod
    def _sun(t):
        d = datetime.fromtimestamp(t)
        h = d.hour + d.minute / 60
        rnd = random.Random(int(t // 600))
        return max(0.0, math.sin(math.pi * (h - 6.5) / 13)) ** 1.3 * rnd.uniform(.75, 1) if 6.5 < h < 19.5 else 0.0

    def history(self, now, hours=24):
        """`hours` of samples (every 5 min, every minute for the last 2 h) with a simple battery model, plus the voltages,
        temperatures and other readings the real history keeps (server.history_row)."""
        rows, soc = [], 55.0
        t0 = int(now // 300 * 300) - int(hours * 3600)
        times = list(range(t0, int(now) - 7200, 300)) + list(range(int((now - 7200) // 60 * 60), int(now) + 1, 60))
        for i, t in enumerate(times):
            step = (t - times[i - 1]) if i else 300
            h = datetime.fromtimestamp(t).hour
            load = 520 + 900 * (17 <= h <= 21) + 300 * (6 <= h <= 8) + random.Random(t).uniform(-120, 200)
            pv_avail = 7400 * self._sun(t)
            room = soc < 99.5
            charge = min(max(0.0, pv_avail - load), 5000) if room else 0.0
            pv = min(pv_avail, load + charge)
            discharge = max(0.0, load - pv) if soc > 20 else 0.0
            grid = max(0.0, load - pv - discharge)
            if self.quick and t >= self.quick_since:
                gcharge = min(4000.0, (100 - soc) * 200) if soc < 100 else 0.0
                charge, grid, discharge = charge + gcharge, grid + gcharge + (discharge if discharge else 0), 0.0
            soc = max(10.0, min(100.0, soc + (charge - discharge) * step / 3600 / 14336 * 100))
            rnd, sun = random.Random(t * 7), self._sun(t)
            bat = charge - discharge
            vb = round(51.4 + soc * .045 + bat / 5000 * .8, 1)
            vac = round(240 + 2.2 * math.sin(t / 5400) + rnd.uniform(-.6, .6), 1)
            hz = round(60 + rnd.uniform(-.03, .03), 2)
            ti = round(29 + load / 450 + pv / 900 + abs(bat) / 1500)
            rows.append(dict(t=t, pv=round(pv), pv1=round(pv * .45), pv2=round(pv) - round(pv * .45), load=round(load), soc=round(soc, 1),
                             battery=round(bat), grid=round(grid), inv=round(load),
                             v1=round(372 + 22 * sun + rnd.uniform(-3, 3), 1) if pv else round(8 * sun, 1),
                             v2=round(138 + 9 * sun + rnd.uniform(-2, 2), 1) if pv else round(3 * sun, 1),
                             vb=vb, vac=vac, hz=hz, veps=round(vac - .4, 1), heps=hz,
                             bus1=round(431 + rnd.uniform(-2.5, 2.5), 1), bus2=round(394 + rnd.uniform(-2.5, 2.5), 1),
                             ti=ti, tr1=ti - 3, tr2=ti + 1, tb=round(25 + abs(bat) / 1800 + 2 * math.sin(t / 43200), 1),
                             cmax=round(vb / 16 + .004, 3), cmin=round(vb / 16 - .003, 3),
                             chg=round(max(0, bat)), dis=round(max(0, -bat)), rec=round(4000 if self.quick and t >= self.quick_since and soc < 100 else 0)))
        return rows

    def live(self, now):
        r = self.history(now)[-1]
        p1, p2 = r["pv1"], r["pv2"]
        strings = [dict(n=1, w=p1, v=r["v1"]), dict(n=2, w=p2, v=r["v2"])]
        return dict(t=round(now), pv=r["pv"], pv_strings=strings,
                    soc=round(r["soc"]), v_bat=r["vb"], charge=max(0, r["battery"]), discharge=max(0, -r["battery"]),
                    battery=r["battery"], grid=r["grid"], inv=r["load"], load=r["load"], rectifier=r["rec"], v_ac=r["vac"], hz=r["hz"],
                    t_inner=r["ti"], t_bat=r["tb"], v_eps=r["veps"], hz_eps=r["heps"], bus1=r["bus1"], bus2=r["bus2"],
                    t_rad1=r["tr1"], t_rad2=r["tr2"], cell_max=r["cmax"], cell_min=r["cmin"], bms_chg_a=100.0, bms_dis_a=100.0, status="Charging from grid (demo)" if self.quick else "PV, Battery (demo)",
                    lost=False, fw="FAAB-2525 (demo)", rating="6kW", device_time=datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S"))

    def energy(self, now):
        rows = self.history(now)
        today = date.fromtimestamp(now)
        day = [r for r in rows if date.fromtimestamp(r["t"]) == today]
        kwh = lambda f: round(sum(f(r) * (r2["t"] - r["t"]) for r, r2 in zip(day, day[1:])) / 3600 / 1000, 1)
        t = dict(solar=kwh(lambda r: r["pv"]), used=kwh(lambda r: r["load"]), imported=kwh(lambda r: r["grid"]), exported=0.0,
                 charged=kwh(lambda r: max(0, r["battery"])), discharged=kwh(lambda r: max(0, -r["battery"])))
        return dict(today=t, total=dict(solar=8412.6, used=9120.3, imported=1530.2, exported=0.0, charged=3920.5, discharged=3610.8))

    def battery(self, now):
        soc = self.live(now)["soc"]
        mods = [dict(sn=f"DEMO-LL-{i}", index=i, lost=False, soc=soc, soh=100 - i, v=round(52.6 + soc / 50, 2), a=round(self.live(now)["battery"] / 53 / 3, 1),
                     cycles=212 + 7 * i, cell_max=round(3.30 + soc / 1500, 3), cell_min=round(3.29 + soc / 1500, 3), t_max=27.5 + i, t_min=25.0 + i,
                     remain_ah=round(280 * soc / 100), full_ah=280, fw="2.17") for i in range(3)]
        return dict(soc=soc, v=round(52.6 + soc / 50, 1), power=self.live(now)["battery"], status="Normal", remain_ah=sum(m["remain_ah"] for m in mods),
                    full_ah=840, current="", count=3, modules=mods)

    def days(self, now, n):
        out = []
        for i in range(n - 1, -1, -1):
            d = date.fromtimestamp(now) - timedelta(days=i)
            rnd = random.Random(d.toordinal())
            solar = round(rnd.uniform(14, 34) * (1 + 0.3 * math.cos((d.month - 6) / 6 * math.pi)), 1)  # more sun in summer
            used = round(rnd.uniform(17, 26), 1)
            imp = round(max(0.0, used - solar * .92) + rnd.uniform(0, 1.5), 1)
            out.append(dict(date=d.isoformat(), solar=solar, used=used, imported=imp, exported=0.0,
                            charged=round(min(solar * .45, 13), 1), discharged=round(min(solar * .42, 12.5), 1)))
        return out

    def events(self, now):
        f = lambda days: (datetime.fromtimestamp(now) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        return [dict(code="W018", type="WARNING", type_text="Warning", text="AC input frequency out of range (demo)", start=f(2), end=f(1.99), open=False, hours="0.2"),
                dict(code="E010", type="FAULT", type_text="Fault", text="Battery communication lost (demo)", start=f(9), end=f(8.9), open=False, hours="2.4")]
