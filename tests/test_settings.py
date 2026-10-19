"""Inverter settings: the register map (inverter_settings.py), writing over Wi-Fi (local.Dongle.write_holding), the check
against EG4 Monitor's own decoding, and the server's settings API with the admin-only rule.
Run from the repo root: python tests/test_settings.py   (needs: pip install -r requirements.txt httpx)

A fake dongle serves holding registers; a fake EG4 Monitor answers remoteRead the way the portal does (names and values, decoded
for this model), getting three settings deliberately different: one value, one in raw units, and one it doesn't report."""
import asyncio, json, os, struct, sys, tempfile, threading, time
from pathlib import Path
REPO = str(Path(__file__).resolve().parent.parent)
os.environ["EG4_DATA_DIR"] = tempfile.mkdtemp()
os.environ["EG4_ADMIN_GROUP"] = "admins"
sys.path.insert(0, REPO)
from aiohttp import web
import eg4, local
import inverter_settings as inv

INV, DONGLE = "4512345678", "DJ00000001"
ok = lambda cond, what: print(("PASS " if cond else "FAIL ") + what) or bool(cond)
results = []
B = inv.BY_KEY

# --- 1. the map
regs = inv.demo_registers()
results.append(ok(all(inv.same(B[k], v, inv.decode(B[k], regs)) for k, v in inv.DEMO.items()), "demo values survive encode -> decode"))
results.append(ok(inv.decode(B["HOLD_AC_CHARGE_WINDOW_3"], {72: 9745, 73: 9746}) == "17:38-18:38",
                  "window 3 decodes the owner's real Quick Charge registers (9745/9746) as 17:38-18:38"))
results.append(ok(inv.decode(B["BIT_AC_CHARGE_TYPE"], {120: 146}) == 1 and inv.decode(B["BIT_AC_CHARGE_TYPE"], {120: 144}) == 0,
                  "charge type from register 120 as seen during and after the real Quick Charge (146 -> 1, 144 -> 0)"))
upd = inv.encode(B["FUNC_AC_CHARGE"], False, {21: 0xFFFF})
results.append(ok(upd == {21: 0xFF7F}, "a switch changes only its own bit"))
upd = inv.encode(B["BIT_AC_CHARGE_TYPE"], 2, {120: 0x0090})
results.append(ok(upd == {120: 0x0094}, "a 3-bit choice changes only bits 1-3 (0x90 -> 0x94)"))
results.append(ok(inv.encode(B["HOLD_AC_CHARGE_WINDOW_1"], "1:30-05:00", {}) == {68: 1 | 30 << 8, 69: 5},
                  "a window packs hour | minute << 8 into start and end"))
results.append(ok(inv.encode(B["HOLD_LEAD_ACID_TEMPR_LOWER_LIMIT_DISCHG"], -20, {}) == {106: 0x10000 - 20}
                  and inv.decode(B["HOLD_LEAD_ACID_TEMPR_LOWER_LIMIT_DISCHG"], {106: 0x10000 - 20}) == -20, "signed temperatures"))
results.append(ok(inv.encode(B["HOLD_AC_CHARGE_END_BATTERY_VOLTAGE"], 54.0, {}) == {159: 540}, "54.0 V is 540 (0.1 V units)"))
for key, bad, why in (("HOLD_AC_CHARGE_END_BATTERY_VOLTAGE", 70, "above the range"), ("HOLD_AC_CHARGE_END_BATTERY_VOLTAGE", 54.05, "finer than 0.1 V"),
                      ("HOLD_AC_CHARGE_WINDOW_1", "25:00-01:00", "hour 25"), ("HOLD_OUTPUT_CONFIGURATION", 7, "not a choice"),
                      ("FUNC_AC_CHARGE", "yes", "not on/off"), ("HOLD_DEVICE_TYPE_CODE", 54, "read-only")):
    try:
        inv.encode(B[key], bad, {21: 0}); results.append(ok(False, f"refused: {why}"))
    except ValueError as e:
        results.append(ok(True, f"refused: {why} ({e})"))
results.append(ok(72 in inv.writable_registers() and 19 not in inv.writable_registers(), "read-only registers aren't writable"))


def cloud_params(holding, start=0, count=400):
    """What EG4's remoteRead reports for registers start..start+count-1: names and decoded values, with three differences."""
    p = {}
    for s in inv.SETTINGS:
        if not all(start <= r < start + count for r in s.regs):
            continue
        v = inv.decode(s, holding)
        if s.kind == "window":
            a, b = v.split("-")
            p.update(zip(s.cloud_keys, (a[:2], a[3:], b[:2], b[3:])))
        elif s.kind == "switch":
            p[s.key] = v
        else:
            p[s.key] = str(v)
    p.pop("FUNC_SPORADIC_CHARGE", None)                                        # EG4 doesn't report it
    if "HOLD_FLOATING_VOLTAGE" in p:
        p["HOLD_FLOATING_VOLTAGE"] = "53.5"                                    # a different value
    if "HOLD_AC_CHARGE_POWER_CMD" in p:
        p["HOLD_AC_CHARGE_POWER_CMD"] = str(holding[66])                       # raw units
    if count <= 2:
        p.pop("HOLD_FORCED_DISCHARGE_START_HOUR_2", None)                      # a 0 that the one-register read doesn't confirm
    return p


res = inv.compare(regs, cloud_params(regs))
st = {k: r["status"] for k, r in res.items()}
results.append(ok(st["HOLD_FLOATING_VOLTAGE"] == "mismatch" and st["HOLD_AC_CHARGE_POWER_CMD"] == "raw" and st["FUNC_SPORADIC_CHARGE"] == "missing"
                  and st["HOLD_AC_CHARGE_END_BATTERY_VOLTAGE"] == "match" and st["FUNC_FORCED_DISCHG_EN"] == "weak",
                  "compare: mismatch, raw units, missing, match, and 0-on-both as weak"))


# --- 2. a fake dongle serving holding registers
class FakeDongle:
    def __init__(self, port, holding):
        self.port, self.holding, self.writes, self.ignore, self.server, self.conns = port, holding, [], set(), None, set()

    def frame(self, data):
        data += struct.pack("<H", local.crc16(data))
        body = bytes([1, 0xC2]) + DONGLE.encode() + struct.pack("<H", len(data)) + data
        return b"\xa1\x1a" + struct.pack("<HH", 2, len(body)) + body

    async def handle(self, r, w):
        self.conns.add(w)
        buf = b""
        try:
            while chunk := await r.read(4096):
                frames, buf = local.split_frames(buf + chunk)
                for f in frames:
                    d = f[20:]
                    start, count = struct.unpack("<HH", d[12:16])
                    if d[1] == 6:
                        if start not in self.ignore:   # an inverter that echoes but doesn't keep the value
                            self.holding[start] = count
                        self.writes.append((start, count))
                        w.write(self.frame(bytes([1, 6]) + d[2:16]))
                    else:
                        src = self.holding if d[1] == 3 else {}
                        vals = b"".join(struct.pack("<H", src.get(start + i, 0)) for i in range(count))
                        w.write(self.frame(bytes([1, d[1]]) + d[2:12] + struct.pack("<H", start) + bytes([len(vals)]) + vals))
                    await w.drain()
        except ConnectionError:
            pass
        finally:
            self.conns.discard(w); w.close()

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", self.port)

    async def stop(self):   # like the dongle leaving the network: its connections go too
        self.server.close()
        for w in list(self.conns):
            w.close()
        await self.server.wait_closed()


async def dongle_tests():
    fake = FakeDongle(18101, dict(regs)); await fake.start()
    d = local.Dongle("", INV, ip="127.0.0.1", port=18101)
    got = await d.read_holding(inv.blocks())
    results.append(ok(all(got[r] == regs[r] for r in regs), f"reads every settings register in {len(inv.blocks())} requests"))
    changed = await d.write_holding({161: 90, 160: regs[160]}, inv.writable_registers())
    results.append(ok(fake.writes == [(161, 90)] and changed[161] == (100, 90), "writes only the register that differs, then reads it back"))
    try:
        await d.write_holding({19: 1}, inv.writable_registers()); results.append(ok(False, "a register outside the map is refused"))
    except local.LocalError:
        results.append(ok(fake.writes == [(161, 90)], "a register outside the map is refused before anything is sent"))
    fake.ignore = {164}
    try:
        await d.write_holding({164: 33}, inv.writable_registers()); results.append(ok(False, "a value the inverter doesn't keep is reported"))
    except local.LocalError as e:
        results.append(ok("didn't keep" in str(e), f"a value the inverter echoes but doesn't keep is reported ({e})"))
    await d.close(); await fake.stop()

asyncio.run(dongle_tests())


# --- 3. the server
eg4.EG4_URL = "http://127.0.0.1:8913"
dongle_holding = dict(regs)
remote_reads = []
async def eg4_handler(req):
    f = await req.post(); j = lambda **kw: web.json_response(dict(success=True, **kw))
    if req.path.endswith("/login"):
        return j(plants=[{"name": "Home", "plantId": 7, "inverters": [{"serialNum": INV}]}])
    if req.path.endswith("remoteRead/read"):
        start, count = int(f["startRegister"]), int(f["pointNumber"])
        remote_reads.append((start, count))
        return j(inverterSn=INV, startRegister=start, pointNumber=count, **cloud_params(dongle_holding, start, count))
    if req.path.endswith("getInverterRuntime"):
        return j(soc=70, vBat=532, statusText="PV")
    if req.path.endswith("getStatusInfo"):
        return j(hasUnclosedQuickChargeTask=False)
    if req.path.endswith("event/list"):
        return j(rows=[])
    return j()

def run_cloud():
    async def main():
        app = web.Application(); app.router.add_post("/{tail:.*}", eg4_handler)
        r = web.AppRunner(app); await r.setup(); await web.TCPSite(r, "127.0.0.1", 8913).start()
        await asyncio.Event().wait()
    asyncio.run(main())
threading.Thread(target=run_cloud, daemon=True).start(); time.sleep(1)

import server
from fastapi.testclient import TestClient
local.PORT = 18102
ADMIN = {"origin": "https://solar.example.org", "remote-user": "anthony", "remote-groups": "family,admins"}
FAMILY = {"origin": "https://solar.example.org", "remote-user": "kid", "remote-groups": "family"}
row = lambda p, k: next(r for r in p["settings"] if r["key"] == k)

with TestClient(server.app, base_url="http://solar.example.org") as c:
    # demo mode: everything writable, changes logged
    c.post("/api/demo", json={"on": True}, headers=ADMIN)
    p = c.get("/api/inverter", headers=ADMIN).json()
    results.append(ok(p["source"] == "demo" and len(p["settings"]) == len(inv.SETTINGS) and p["can_write"] and row(p, "FUNC_AC_CHARGE")["unlocked"],
                      f"demo: {len(p['settings'])} settings, all writable for an admin"))
    results.append(ok(not c.get("/api/inverter", headers=FAMILY).json()["can_write"], "someone outside the admin group sees them read-only"))
    r = c.post("/api/inverter", json={"changes": {"FUNC_AC_CHARGE": False}}, headers=FAMILY)
    results.append(ok(r.status_code == 403, f"and can't change them ({r.json()['detail']})"))
    p = c.post("/api/inverter", json={"changes": {"HOLD_AC_CHARGE_WINDOW_2": "22:00-06:00", "FUNC_AC_CHARGE": False,
                                                  "HOLD_AC_CHARGE_END_BATTERY_SOC": 95}}, headers=ADMIN).json()
    results.append(ok(row(p, "HOLD_AC_CHARGE_WINDOW_2")["value"] == "22:00-06:00" and row(p, "FUNC_AC_CHARGE")["value"] is False
                      and row(p, "HOLD_AC_CHARGE_END_BATTERY_SOC")["value"] == 95 and len(p["changed"]) == 3,
                      f"demo: three changes applied ({[c['label'] + ': ' + c['before'] + ' -> ' + c['after'] for c in p['changed']]})"))
    results.append(ok(p["changes"][0]["user"] == "anthony" and p["changes"][0]["demo"], "the change log records who"))
    r = c.post("/api/inverter", json={"changes": {"FUNC_SET_TO_STANDBY": False}}, headers=ADMIN)
    results.append(ok(r.status_code == 400 and "confirmation" in r.json()["detail"], "a danger setting needs the extra confirmation"))
    r = c.post("/api/inverter", json={"changes": {"HOLD_AC_CHARGE_END_BATTERY_SOC": 150}}, headers=ADMIN)
    results.append(ok(r.status_code == 400 and "between" in r.json()["detail"], f"an out-of-range value is refused ({r.json()['detail']})"))
    c.post("/api/demo", json={"on": False}, headers=ADMIN)

    # the real path: EG4 login + dongle
    c.post("/api/setup", json={"account": "me", "password": "pw"}, headers=ADMIN)
    fake = FakeDongle(18102, dongle_holding); c.portal.call(fake.start)
    c.post("/api/dongle", json={"ip": "127.0.0.1"}, headers=ADMIN)
    p = c.get("/api/inverter?fresh=1", headers=ADMIN).json()
    results.append(ok(p["source"] == "wifi" and row(p, "HOLD_AC_CHARGE_END_BATTERY_SOC")["value"] == 100,
                      "over Wi-Fi: values come straight from the inverter"))
    results.append(ok(row(p, "HOLD_AC_CHARGE_WINDOW_3")["unlocked"] and row(p, "HOLD_AC_CHARGE_END_BATTERY_VOLTAGE")["unlocked"]
                      and not row(p, "HOLD_AC_CHARGE_END_BATTERY_SOC")["unlocked"],
                      "before the check only the settings verified on the owner's inverter are unlocked"))
    r = c.post("/api/inverter", json={"changes": {"HOLD_AC_CHARGE_END_BATTERY_SOC": 90}}, headers=ADMIN)
    results.append(ok(r.status_code == 400 and "locked" in r.json()["detail"], f"an unchecked setting can't be written ({r.json()['detail']})"))
    c.post("/api/inverter/check", headers=ADMIN)
    end = time.time() + 20
    while time.time() < end and (p := c.get("/api/inverter", headers=ADMIN).json())["check"]["running"]:
        time.sleep(.2)
    cnt = p["check"]["counts"]
    results.append(ok(p["check"]["at"] and not p["check"]["error"] and cnt.get("match", 0) > 100, f"check finished: {cnt}"))
    st = {r["key"]: (r["status"], r["unlocked"]) for r in p["settings"]}
    results.append(ok(st["HOLD_AC_CHARGE_END_BATTERY_SOC"] == ("match", True) and st["HOLD_FLOATING_VOLTAGE"] == ("mismatch", False)
                      and st["HOLD_AC_CHARGE_POWER_CMD"] == ("raw", False) and st["FUNC_SPORADIC_CHARGE"] == ("missing", False),
                      "after the check: matches unlocked; a different value, raw units and a missing setting stay locked"))
    results.append(ok(st["FUNC_FORCED_DISCHG_EN"] == ("match", True) and st["HOLD_FORCED_DISCHARGE_WINDOW_3"] == ("weak", False),
                      "a 0 confirmed by a one-register read unlocks; one EG4 doesn't confirm stays locked"))
    results.append(ok(any(n == 1 for _, n in remote_reads) and remote_reads[:3] == [(0, 127), (127, 127), (254, 127)],
                      f"EG4 was asked for 3 blocks, then {sum(n <= 2 for _, n in remote_reads)} single registers"))
    r120 = dongle_holding[120]
    fake.writes.clear()
    p = c.post("/api/inverter", json={"changes": {"HOLD_AC_CHARGE_END_BATTERY_SOC": 90, "BIT_ON_GRID_EOD_TYPE": 0}}, headers=ADMIN).json()
    results.append(ok(dongle_holding[161] == 90 and dongle_holding[120] == r120 & ~0x40 and sorted(fake.writes) == [(120, r120 & ~0x40), (161, 90)],
                      f"over Wi-Fi: two settings written, register 120's other bits kept ({fake.writes})"))
    results.append(ok(row(p, "HOLD_AC_CHARGE_END_BATTERY_SOC")["value"] == 90 and p["changes"][0]["user"] == "anthony"
                      and json.loads(server.CHANGES.read_text().splitlines()[-1])["after"] in ("90 %", "Voltage"), "read back and logged"))
    # the dongle drops: EG4's decoding is shown read-only
    c.portal.call(fake.stop); c.portal.call(lambda: server.S.dongle.close()); c.portal.call(server.S.wake.set)
    end = time.time() + 15
    while time.time() < end and c.get("/api/state").json()["connection"]["source"] != "cloud":
        time.sleep(.2)
    p = c.get("/api/inverter?fresh=1", headers=ADMIN).json()
    results.append(ok(p["source"] == "cloud" and not p["can_write"] and row(p, "HOLD_AC_CHARGE_END_BATTERY_SOC")["value"] == 90 and p["error"],
                      f"no dongle: EG4 Monitor's values, read-only ({p['error']})"))

print(f"\n{sum(results)} of {len(results)} passed")
sys.exit(0 if all(results) else 1)
