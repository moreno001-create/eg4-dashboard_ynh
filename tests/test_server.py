"""Runs the real (non-demo) code path against a fake EG4 Monitor server on port 8812.
Run from the repo root: python tests/test_server.py   (needs: pip install -r requirements.txt httpx2)"""
import asyncio, os, sys, tempfile, threading, time
from pathlib import Path
REPO = str(Path(__file__).resolve().parent.parent)
os.environ["EG4_DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, REPO)
from aiohttp import web
import eg4
eg4.EG4_URL = "http://127.0.0.1:8812"

fake = dict(soc=70, quick=False, logins=0, calls=[])
async def handler(req):
    f = await req.post(); path = req.path; fake["calls"].append(path.rsplit("/", 1)[-1])
    ok = lambda **kw: web.json_response(dict(success=True, **kw))
    if path.endswith("/login"):
        fake["logins"] += 1
        if f["password"] != "pw": return web.json_response({"success": False, "msg": "account or password error"})
        return ok(plants=[{"name": "Home", "plantId": 7, "inverters": [{"serialNum": "4512345678", "powerRating": 6}]}])
    if path.endswith("getInverterRuntime"):
        return ok(serialNum=f["serialNum"], ppv=4200, ppv1=2300, ppv2=1900, vpv1=4012, vpv2=3880, soc=fake["soc"], vBat=532, pCharge=2600, pDisCharge=0,
                  ppv3=0, vpv3=4337,  # the real 6000XP reports a stray third-input voltage
                  pinv=0, peps=1450, pToUser=0, pToGrid=0, consumptionPower=0, vacr=2405, fac=5999, tinner=42, tBat=26, statusText="PV,Charge", fwCode="FAAB-2525")
    if path.endswith("getInverterEnergyInfo"):
        return ok(todayYielding=184, todayCharging=91, todayDischarging=40, todayImport=12, todayExport=0, todayUsage=150,
                  totalYielding=84126, totalCharging=39205, totalDischarging=36108, totalImport=15302, totalExport=0, totalUsage=91203)
    if path.endswith("getBatteryInfo"):
        return ok(soc=fake["soc"], vBat=532, batPower=2600, remainCapacity=196, fullCapacity=280, totalNumber=1, hasCellExtremes=True,
                  globalMaxCellVoltage=3337, globalMinCellVoltage=3326, globalMaxCellTemp=256, globalMinCellTemp=254, bmsCycleCnt=58,
                  batteryArray=[dict(batterySn="LL1", batIndex=0, lost=False, totalVoltage=5321, current=489, soc=fake["soc"], soh=99,
                                     currentRemainCapacity=196, currentFullCapacity=280, batMaxCellTemp=271, batMinCellTemp=255,
                                     batMaxCellVoltage=3331, batMinCellVoltage=3318, cycleCnt=211, fwVersionText="2.17")])
    if path.endswith("monthColumn"):
        # like the real 6000XP: no ePvDay, one total per input
        return ok(data=[dict(day=d, ePv1Day=100 + d, ePv2Day=100, eConsumptionDay=170, eToUserDay=10, eToGridDay=0, eChgDay=80, eDisChgDay=75) for d in range(1, 11)])
    if path.endswith("event/list"):
        assert f["plantId"] == "7", f
        return ok(total=1, rows=[dict(event="E010", eventType="FAULT", eventTypeText="Fault", eventText="Battery communication lost", startTime="2026-10-09 08:00:00", status="OPEN")])
    if path.endswith("quickCharge/start"): fake["quick"] = True; return ok()
    if path.endswith("quickCharge/stop"): fake["quick"] = False; return ok()
    if path.endswith("getStatusInfo"): return ok(hasUnclosedQuickChargeTask=fake["quick"], remainTimeBeforeQuickChargeStop=3428 if fake["quick"] else 0)
    return web.json_response({"success": False, "msg": "unknown " + path})

def run_fake():
    async def main():
        app = web.Application(); app.router.add_post("/{tail:.*}", handler)
        r = web.AppRunner(app); await r.setup(); await web.TCPSite(r, "127.0.0.1", 8812).start()
        await asyncio.Event().wait()
    asyncio.run(main())
threading.Thread(target=run_fake, daemon=True).start(); time.sleep(1)

import server
from fastapi.testclient import TestClient
def wait(cond, secs=8):
    end = time.time() + secs
    while time.time() < end:
        if cond(): return True
        time.sleep(.2)
    return False
with TestClient(server.app, base_url="http://solar.example.org") as c:
    print("bad login ->", c.post("/api/setup", json={"account": "me", "password": "nope"}).json()["detail"])
    s = c.post("/api/setup", json={"account": "me", "password": "pw"}).json()
    print("setup: serial", s["serial"], "| configured", s["configured"])
    wait(lambda: (lambda j: j["live"] is not None and j["battery"] is not None)(c.get("/api/state").json()))
    s = c.get("/api/state").json()
    print("live:", {k: s["live"][k] for k in ("pv", "soc", "v_bat", "battery", "grid", "load", "v_ac", "hz")}, "strings", s["live"]["pv_strings"])
    print("energy today:", s["energy"]["today"])
    print("battery bank:", s["battery"]["bank"])
    print("battery module:", {k: s["battery"]["modules"][0][k] for k in ("v", "a", "cell_min", "cell_max", "t_min", "t_max", "soh")})
    print("events:", s["events"])
    print("history rows:", len(c.get("/api/history").json()), "| days:", c.get("/api/days?n=30").json()[-1])
    print("months:", c.get("/api/months").json()[-1])
    print("password in state?", "pw" in str(s.get("account")) and False, "| settings mode:", oct(os.stat(server.SETTINGS).st_mode)[-3:])
    print("cross-site POST:", c.post("/api/charge", json={"start": True}, headers={"origin": "https://evil.example"}).status_code)
    s = c.post("/api/charge", json={"start": True, "target": 80}, headers={"origin": "https://solar.example.org"}).json()
    print("start charge -> active", s["quick"]["active"], "target", s["quick_target"], "| min left (3428 s):", s["quick"]["remain_min"])
    fake["soc"] = 81; c.portal.call(server.S.wake.set)  # wake the poller from its own event loop
    print("auto-stop at 81%:", wait(lambda: not fake["quick"]), "|", c.get("/api/state").json()["notices"][0]["msg"])
