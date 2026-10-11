"""The Wi-Fi dongle path (local.py) against a fake dongle, and the server's Wi-Fi-first / cloud-fallback switching.
Run from the repo root: python tests/test_local.py   (needs: pip install -r requirements.txt httpx2)

The fake dongle answers with input registers read from the owner's real 6000XP (2026-10-10, while grid charging), and
like the real one it also sends things nobody asked for: a heartbeat, and its replies to the cloud's own reads."""
import asyncio, binascii, os, struct, sys, tempfile, threading, time
from pathlib import Path
REPO = str(Path(__file__).resolve().parent.parent)
os.environ["EG4_DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, REPO)
from aiohttp import web
import eg4, local

INV, DONGLE, MAC = "4512345678", "DJ00000001", "f0:9e:9e:c9:f8:68"
REAL = {0: [20, 3997, 1343, 4344, 533, 48, 0, 94, 166, 0, 5403, 0, 2403, 0, 31745, 5997, 0, 5307, 2220, 0, 2401, 12816, 4178, 5995, 0, 0, 0, 6976, 26, 32, 0, 0, 30, 53, 7, 36, 0, 95, 4345, 3947],
        40: [23507, 0, 48832, 0, 0, 0, 60, 0, 37456, 0, 7123, 1, 2595, 1, 28721, 1, 0, 0, 55055, 0, 0, 0, 0, 0, 31, 27, 31, 29, 1, 51096, 757, 0, 0, 0, 0, 0, 0, 0, 0, 0],
        80: [1050, 1000, 1000, 574, 0, 0, 0, 49155, 0, 0, 12925, 43967, 782, 24816, 15597, 3, 1, 100, 406, 0, 0, 3336, 3332, 276, 274, 539, 58, 541, 0, 0, 0, 0, 0, 260, 0, 13108, 13111, 13367, 14384, 14132],
        120: [2218, 0, 0, 40263, 1280, 31937, 148, 1205, 1203, 0, 0, 0, 0, 23, 13, 64636, 0, 29775, 0, 0, 0, 896, 0, 262, 0, 0, 0, 0, 0, 0, 122, 0, 71, 0, 0, 0, 0, 0, 0, 0],
        160: [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1592, 0, 0, 0, 33024, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1208, 1207, 0, 0, 0, 0, 2658]}
REGS = {b + i: v for b, vals in REAL.items() for i, v in enumerate(vals)}
# holding registers read from the real 6000XP during a 60-minute Quick Charge started at 17:01 (2026-10-10), and what EG4's Stop changed
CHARGING = {72: 0x0111, 73: 0x0112, 120: 0x0092, 159: 590, 68: 0, 69: 0, 70: 0, 71: 0, 21: 897}
STOPPED = {72: 0, 73: 0, 120: 0x0090, 159: 540}
ok = lambda cond, what: print(("PASS " if cond else "FAIL ") + what) or bool(cond)
results = []

# --- 1. framing, against a frame captured from the real dongle (its reply to a holding-register read)
captured = binascii.unhexlify("a11a0500bf0001c2444a3432363030323239b1000103343337333734303834370000a0c11a090034333733373430383437636361611910110100001a0a0a110101010001000000000026000400810341001e003c00a307e2095c12ba13a307e209f401d0077e04550a0a0064000000c80a0000080000005c1250140a00d7000000000000000000000000000000000000000000000000000000000064000000e80364006400640064000000000000000000000000000000000000000000000000000000414e")
frames, rest = local.split_frames(b"junk" + captured[:50])
results.append(ok(frames == [] and rest == captured[:50], "a partial frame waits for the rest"))
frames, rest = local.split_frames(b"junk" + captured + captured[:3])
p = local.parse_frame(frames[0])
results.append(ok(len(frames) == 1 and p["dongle"] == "DJ42600229" and p["inverter"] == "4373740847" and p["fn"] == 3 and len(p["regs"]) == 80,
                  "real frame: dongle DJ42600229, inverter 4373740847, 80 holding registers, checksum good"))
bad = bytearray(captured); bad[-1] ^= 0xFF
results.append(ok("regs" not in local.parse_frame(bytes(bad)), "a frame with a bad checksum is ignored"))
results.append(ok(local.norm_mac("F0-9E-9E-C9-F8-68") == local.norm_mac("f0:9e:9e:c9:f8:68") == local.norm_mac("f0:9e:9e:c9:f8:68".replace("08", "8")) == MAC
                  and local.norm_mac("f0:9e:9e:c9:f8") == "", "MAC addresses in Windows, Linux and macOS styles match"))

# --- 2. units, from the real registers
L, E, B = local.norm_live(REGS), local.norm_energy(REGS), local.norm_bank(REGS)
results.append(ok((L["soc"], L["v_bat"], L["pv"], L["charge"], L["grid"], L["load"], L["v_ac"], L["hz"]) == (48, 53.3, 260, 5403, 6976, 1592, 240.3, 59.97),
                  f"live: 48%, 53.3 V, 260 W solar, charging 5403 W, grid 6976 W, home 1592 W, 240.3 V, 59.97 Hz ({L['status']})"))
results.append(ok([s["n"] for s in L["pv_strings"]] == [1, 2], "the stray third-input voltage doesn't add a third solar input"))
results.append(ok((E["today"]["solar"], E["today"]["imported"], E["today"]["used"], E["total"]["solar"]) == (5.8, 9.5, None, 7233.9),
                  "energy: 5.8 kWh solar today, 9.5 kWh imported, home use left to the cloud, 7233.9 kWh lifetime solar"))
results.append(ok((B["cell_max"], B["cell_min"], B["t_max"], B["cycles"]) == (3.336, 3.332, 27.6, 58), "battery cells 3.332-3.336 V, 27.6 C, 58 cycles"))


# --- 3. a fake dongle
class FakeDongle:
    def __init__(self, port, serial=DONGLE):
        self.port, self.serial, self.server, self.clients, self.requests = port, serial, None, 0, 0
        self.holding, self.writes, self.refuse = dict(CHARGING), [], set()

    def frame(self, data):
        data += struct.pack("<H", local.crc16(data))
        body = bytes([1, 0xC2]) + self.serial.encode() + struct.pack("<H", len(data)) + data
        return b"\xa1\x1a" + struct.pack("<HH", 2, len(body)) + body

    def reply(self, fn, start, count, inverter=INV):
        src = self.holding if fn == 3 else REGS
        vals = b"".join(struct.pack("<H", src.get(start + i, 0)) for i in range(count))
        data = bytes([1, fn]) + inverter.encode() + struct.pack("<H", start) + bytes([len(vals)]) + vals
        data += struct.pack("<H", local.crc16(data))
        body = bytes([1, 0xC2]) + self.serial.encode() + struct.pack("<H", len(data)) + data
        return b"\xa1\x1a" + struct.pack("<HH", 2, len(body)) + body

    async def handle(self, r, w):
        self.clients += 1
        heartbeat = b"\xa1\x1a" + struct.pack("<HH", 2, 13) + bytes([1, 0xC1]) + self.serial.encode() + b"\x00"
        w.write(heartbeat)
        buf = b""
        try:
            while chunk := await r.read(4096):
                frames, buf = local.split_frames(buf + chunk)
                for f in frames:
                    d = f[20:]
                    start, count = struct.unpack("<HH", d[12:16])
                    self.requests += 1
                    if d[1] == 6:   # write single: refuse, or store and echo
                        if start in self.refuse:
                            w.write(self.frame(bytes([1, 0x86]) + d[2:12] + struct.pack("<H", start) + bytes([2])))
                        else:
                            self.holding[start] = count; self.writes.append((start, count))
                            w.write(self.frame(bytes([1, 6]) + d[2:16]))
                        await w.drain(); continue
                    # first the reply to someone else's read (as the dongle passes along the cloud's), and a late 1-register
                    # reply to an earlier request for the same start, then ours, split in two
                    w.write(self.reply(3, 0, 10)); w.write(self.reply(d[1], start, 1, d[2:12].decode()))
                    msg = self.reply(d[1], start, count, d[2:12].decode())
                    w.write(msg[:7]); await w.drain(); w.write(msg[7:])
                    await w.drain()
        except ConnectionError:
            pass
        finally:
            w.close()

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", self.port)

    async def stop(self):
        self.server.close(); await self.server.wait_closed(); self.server = None


async def dongle_tests():
    fake = FakeDongle(18001); await fake.start()
    d = local.Dongle("", INV, ip="127.0.0.1", port=18001)
    regs = await d.read_all()
    results.append(ok(len(regs) == 212 and regs[5] == 48 and d.dongle_sn == DONGLE,
                      "reads 212 registers, skipping a heartbeat and a reply meant for the cloud; learns the dongle serial"))
    regs = await d.read_all()
    results.append(ok(fake.clients == 1, "keeps one connection open between readings (the dongle takes one local client)"))
    await d.close()
    other = FakeDongle(18002, "DJ99999999"); await other.start()
    d2 = local.Dongle("", INV, dongle_sn=DONGLE, ip="127.0.0.1", port=18002)
    try:
        await d2.read_all(); results.append(ok(False, "a different dongle at the address is refused"))
    except local.LocalError as e:
        results.append(ok("different dongle" in str(e), f"a different dongle at the address is refused ({e})"))
    await d2.close(); await other.stop(); await fake.stop()
    # found by MAC: the ARP table says where the MAC is now
    local.arp_table = lambda: {MAC: "127.0.0.1"}
    fake = FakeDongle(18003); await fake.start()
    d3 = local.Dongle(MAC, INV, ip="10.255.255.1", port=18003, timeout=2)   # the old address no longer answers
    regs = await d3.read_all()
    results.append(ok(d3.ip == "127.0.0.1" and regs[5] == 48, "a dongle with a new address is found again by its MAC"))
    await d3.close(); await fake.stop()

    # --- stopping a grid charge over Wi-Fi (the only write)
    try:
        local.write_request(DONGLE, INV, 21, 0); results.append(ok(False, "writes to other registers are refused"))
    except local.LocalError:
        results.append(ok(True, "writes to any register but the four Quick Charge ones are refused before sending"))
    q = local.quick_status(CHARGING, time.mktime((2026, 10, 10, 17, 30, 0, 0, 0, -1)))
    results.append(ok(q["active"] and q["remain_min"] == 31 and q["until"] == "18:01",
                      f"charge window read from the inverter: active, 31 min left at 17:30 ({q})"))
    results.append(ok(local.normal_charge_settings(CHARGING) is None and local.normal_charge_settings({**CHARGING, **STOPPED}) == dict(type=0, end_v=540),
                      "the normal charge settings are only learned while no charge window is set"))
    fake = FakeDongle(18005); await fake.start()
    d4 = local.Dongle("", INV, ip="127.0.0.1", port=18005)
    await d4.read_all()
    changed = await d4.stop_quick_charge()
    results.append(ok({r: fake.holding[r] for r in STOPPED} == STOPPED and [r for r, _ in fake.writes] == [72, 73, 120, 159],
                      f"stop writes exactly what EG4's Stop wrote, window first: {fake.writes}"))
    results.append(ok(fake.holding[120] & ~0x0E == CHARGING[120] & ~0x0E, "the other bits of register 120 are kept"))
    fake.writes.clear(); await d4.stop_quick_charge()
    results.append(ok(fake.writes == [], "stopping when nothing is charging writes nothing"))
    fake.holding.update(CHARGING); fake.refuse = {120}
    try:
        await d4.stop_quick_charge(); results.append(ok(False, "a refused write is reported"))
    except local.LocalError as e:
        results.append(ok("refused" in str(e) and fake.holding[72] == 0, f"a refused write is reported, after the window was already cleared ({e})"))
    await d4.close(); await fake.stop()

asyncio.run(dongle_tests())


# --- 4. the server: Wi-Fi first, the cloud when the dongle drops, back to Wi-Fi when it returns
eg4.EG4_URL = "http://127.0.0.1:8813"
async def eg4_handler(req):
    f = await req.post(); j = lambda **kw: web.json_response(dict(success=True, **kw))
    if req.path.endswith("/login"):
        return j(plants=[{"name": "Home", "plantId": 7, "inverters": [{"serialNum": INV}]}])
    if req.path.endswith("getInverterRuntime"):
        return j(ppv1=1000, ppv2=900, vpv1=3900, vpv2=3800, soc=70, vBat=532, pCharge=0, pDisCharge=300, pinv=0, peps=1200,
                 pToUser=0, pToGrid=0, consumptionPower=1200, vacr=2400, fac=6000, tinner=40, tBat=25, statusText="PV,Discharge", fwCode="FAAB-2525")
    if req.path.endswith("getInverterEnergyInfo"):
        return j(todayYielding=40, todayCharging=10, todayDischarging=5, todayImport=3, todayExport=0, todayUsage=88,
                 totalYielding=1, totalCharging=1, totalDischarging=1, totalImport=1, totalExport=0, totalUsage=999)
    if req.path.endswith("getBatteryInfo"):
        return j(soc=70, vBat=532, batteryArray=[], globalMaxCellVoltage=3300, globalMinCellVoltage=3290, globalMaxCellTemp=250, globalMinCellTemp=240, bmsCycleCnt=57)
    if req.path.endswith("getStatusInfo"):
        return j(hasUnclosedQuickChargeTask=cloud["quick"])
    if req.path.endswith("quickCharge/stop"):
        if cloud["stop_fails"]:
            return web.json_response({"success": False, "msg": "server busy"})
        cloud["quick"] = False; return j()
    if req.path.endswith("event/list"):
        return j(rows=[])
    return web.json_response({"success": False, "msg": "unknown"})

cloud = dict(quick=False, stop_fails=False)
def run_cloud():
    async def main():
        app = web.Application(); app.router.add_post("/{tail:.*}", eg4_handler)
        r = web.AppRunner(app); await r.setup(); await web.TCPSite(r, "127.0.0.1", 8813).start()
        await asyncio.Event().wait()
    asyncio.run(main())
threading.Thread(target=run_cloud, daemon=True).start(); time.sleep(1)

import server
from fastapi.testclient import TestClient
local.PORT = 18004
def wait(c, cond, secs=10):
    end = time.time() + secs
    while time.time() < end:
        s = c.get("/api/state").json()
        if cond(s): return s
        time.sleep(.2)
    return s

with TestClient(server.app, base_url="http://solar.example.org") as c:
    c.post("/api/setup", json={"account": "me", "password": "pw"})
    s = wait(c, lambda s: s["live"] is not None)
    results.append(ok(s["connection"]["source"] == "cloud" and s["live"]["soc"] == 70, "no dongle set up: readings come from the cloud"))
    r = c.post("/api/dongle", json={"mac": MAC}, headers={"origin": "https://solar.example.org"})
    results.append(ok(r.status_code == 400, f"a dongle that isn't there isn't saved ({r.json().get('detail')})"))
    fake = FakeDongle(18004); c.portal.call(fake.start)
    s = c.post("/api/dongle", json={"mac": MAC}, headers={"origin": "https://solar.example.org"}).json()
    s = wait(c, lambda s: s["connection"]["source"] == "local")
    results.append(ok(s["connection"]["source"] == "local" and s["live"]["soc"] == 48 and s["connection"]["dongle_serial"] == DONGLE,
                      "with the dongle: live readings come over Wi-Fi (48%, not the cloud's 70%)"))
    lv = c.get("/api/live").json()
    results.append(ok(lv["source"] == "local" and lv["soc"] == 48 and lv["interval"] == 5 and lv["age"] < 10 and not lv["stale"],
                      f"/api/live for other apps: {lv}"))
    results.append(ok(s["energy"]["today"]["solar"] == 5.8 and s["energy"]["today"]["used"] == 8.8 and s["live"]["fw"] == "FAAB-2525",
                      "the dongle's energy counters are laid over the cloud's; home use and firmware still come from the cloud"))
    c.portal.call(fake.stop); c.portal.call(lambda: server.S.dongle.close())   # the dongle drops off the network
    c.portal.call(server.S.wake.set)
    s = wait(c, lambda s: s["connection"]["source"] == "cloud")
    results.append(ok(s["connection"]["source"] == "cloud" and s["live"]["soc"] == 70 and s["connection"]["local_error"],
                      f"dongle lost: falls back to the cloud ({s['connection']['local_error']})"))
    c.portal.call(fake.start); server.S.local_failed_at = 1   # pretend the minute's wait has passed
    c.portal.call(server.S.wake.set)
    s = wait(c, lambda s: s["connection"]["source"] == "local")
    results.append(ok(s["connection"]["source"] == "local" and s["connection"]["local_error"] is None, "dongle back: switches back to Wi-Fi"))
    # stopping a grid charge: EG4 first; over Wi-Fi when EG4 fails
    H = {"origin": "https://solar.example.org"}
    fake.holding.update(CHARGING); cloud.update(quick=True, stop_fails=True); fake.writes.clear()
    server.S.quick = dict(active=True, remain_min=30, status="")
    s = c.post("/api/charge", json={"start": False}, headers=H).json()
    results.append(ok(fake.holding[72] == 0 and not s["quick"]["active"] and "over Wi-Fi" in s["notices"][0]["msg"],
                      f"EG4 refuses the stop: stopped over Wi-Fi instead ({s['notices'][0]['msg']})"))
    cloud.update(stop_fails=False); fake.holding.update(CHARGING); fake.writes.clear(); server.S.quick = dict(active=True, remain_min=30, status="")
    s = c.post("/api/charge", json={"start": False}, headers=H).json()
    results.append(ok(not cloud["quick"] and fake.writes == [], "EG4 working: the stop goes through EG4, nothing written over Wi-Fi"))
    # no EG4 login at all: charge status from the inverter, Stop and the automatic stop over Wi-Fi
    c.post("/api/forget", headers=H)
    fake.holding.update(CHARGING); server.S.local_failed_at = 0; c.portal.call(server.S.wake.set)
    s = wait(c, lambda s: s["quick"] and s["quick"]["active"])
    results.append(ok(s["configured"] and s["quick"]["active"] and s["quick"]["until"] == "18:01", f"no EG4 login: the charge is seen from the inverter ({s['quick']})"))
    server.S.settings["quick_target"] = 40   # battery is at 48%
    c.portal.call(server.S.wake.set)
    s = wait(c, lambda s: not s["quick"]["active"])
    results.append(ok(fake.holding[72] == 0 and "automatically" in s["notices"][0]["msg"] and "Wi-Fi" in s["notices"][0]["msg"],
                      f"automatic stop at the target level over Wi-Fi ({s['notices'][0]['msg']})"))
    results.append(ok(c.get("/api/months").status_code == 400 and c.get("/api/days").status_code == 400,
                      "no EG4 login: the history pages say they need it (400), not a crash"))
    r = c.post("/api/charge", json={"start": True}, headers=H)
    results.append(ok(r.status_code == 400, f"starting still needs EG4 ({r.json()['detail']})"))
    c.post("/api/setup", json={"account": "me", "password": "pw"})
    s = c.post("/api/dongle", json={"mac": ""}, headers={"origin": "https://solar.example.org"}).json()
    s = wait(c, lambda s: s["connection"]["source"] == "cloud")
    results.append(ok(s["connection"]["dongle_mac"] == "" and s["connection"]["source"] == "cloud", f"turned off: back to the cloud only ({s['connection']}, error {s['error']})"))
    c.portal.call(fake.stop)

print(f"\n{sum(results)} of {len(results)} passed")
sys.exit(0 if all(results) else 1)
