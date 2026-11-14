"""The POWLAND path (powland.py) against a fake Eybond Wi-Fi stick, and the server with a POWLAND set up.
Run from the repo root: python tests/test_powland.py   (needs: pip install -r requirements.txt httpx)

The fake stick serves registers read from the owner's real inverter (doc/powland: the full dump from 2026-11-14 01:00 and a
daytime sample at 10:44), and behaves like the real one where it matters: it only connects after a UDP redirect, refuses
requests for more than 64 registers (exception 0x0A) and one span (exception 02), and sends frames nobody asked for."""
import asyncio, json, os, re, socket, struct, sys, tempfile, time
from pathlib import Path
REPO = Path(__file__).resolve().parent.parent
os.environ["EG4_DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, str(REPO))
import powland

ok = lambda cond, what: print(("PASS " if cond else "FAIL ") + what) or bool(cond)
results = []
DOC = REPO / "doc" / "powland"
DUMP = {int(a): int(v) for a, v in (l.split() for l in (DOC / "registers-2026-11-14-0100.txt").read_text().splitlines() if l.strip())}
NIGHT = {int(k): v for k, v in json.loads((DOC / "live-2026-11-14.jsonl").read_text().splitlines()[3])["regs"].items()}   # 01:29:17
DAY = {int(k): v for k, v in json.loads((DOC / "day-2026-11-14.jsonl").read_text().splitlines()[-1])["regs"].items()}      # 10:45:25

# --- 1. framing
pdu = powland.read_pdu(30000, 60)
results.append(ok(pdu[:6] == bytes.fromhex("010375300 03c".replace(" ", "")) and powland.crc16(pdu) == b"\0\0",
                  "Modbus read request: slave 1, function 3, 30000 x 60, CRC good"))
f = powland.frame(7, 4, pdu)
results.append(ok(struct.unpack(">HHHBB", f[:8]) == (7, 0, len(pdu) + 2, 1, 4), "Eybond header: transaction id, length counts address and function"))
reply = bytes([1, 3, 4]) + struct.pack(">HH", 529, 116)
results.append(ok(powland.parse_read_reply(reply + powland.crc16(reply), 30000, 2) == {30000: 529, 30001: 116}, "a reply is decoded"))
results.append(ok(powland.parse_read_reply(reply + b"\0\0", 30000, 2) is None and powland.parse_read_reply(bytes([1, 0x83, 2, 0, 0]), 30000, 2) is None,
                  "a bad checksum or a Modbus exception gives None"))
hb = powland.heartbeat_payload(time.mktime((2026, 11, 14, 10, 0, 0, 0, 0, -1)))
results.append(ok(len(hb) == 8 and hb[0] == 26 and struct.unpack(">H", hb[6:])[0] == 60, "heartbeat: year-2000 ... and a 60 s interval"))

# --- 2. decoding, against what SmartValue showed at the same minute (01:29) and the daytime sample
L = powland.norm_live(NIGHT, 1000)
results.append(ok(L["v_bat"] == 52.9 and L["soc"] == 71 and L["a_bat"] == 11.6 and L["battery"] == -614 and L["discharge"] == 614 and L["charge"] == 0,
                  f"night: battery 52.9 V, 71%, discharging 11.6 A = 614 W ({L['v_bat']}, {L['soc']}, {L['a_bat']}, {L['battery']})"))
l1, l2 = L["legs"]
results.append(ok((l1["v"], l1["a"], l1["w"], l1["va"], l1["pct"]) == (120.1, 2.4, 198, 281, 4) and (l2["v"], l2["a"], l2["w"], l2["va"], l2["pct"]) == (120.1, 4.6, 393, 540, 9),
                  f"night: each leg's voltage, current, power, VA, load% (L1 {l1}, L2 {l2})"))
results.append(ok(L["load"] == 594 and L["va"] == 838 and L["v_eps"] == 120.1 and L["v_ac"] == 124.4 and L["hz"] == 60.0 and L["pv"] == 0,
                  f"night: house 594 W / 838 VA, output 120.1 V, grid 124.4 V 60 Hz, no solar ({L['load']}, {L['v_ac']}, {L['hz']})"))
D = powland.norm_live(DAY, 1000)
s1, s2 = D["pv_strings"]
results.append(ok((s1["v"], s1["a"], s1["w"]) == (113.8, 7.5, 859) and (s2["v"], s2["a"], s2["w"]) == (162.4, 3.9, 635) and D["pv"] == 1494,
                  f"day: PV1 113.8 V 7.5 A 859 W, PV2 162.4 V 3.9 A 635 W ({s1}, {s2})"))
results.append(ok(D["a_bat"] == -13.1 and D["charge"] == 697 and D["status"] == "Solar, charging" and D["bus1"] == 250.9 and D["t_inner"] == 31.7 and D["t_rad1"] == 44.9,
                  f"day: charging 13.1 A = 697 W, DC bus 250.9 V, temperatures ({D['a_bat']}, {D['charge']}, {D['status']}, {D['bus1']}, {D['t_inner']})"))
G = powland.norm_live({int(k): v for k, v in json.loads((DOC / "charge-during.jsonl").read_text().splitlines()[-1])["regs"].items()}, 1000)
results.append(ok(3300 < G["grid"] < 3700 and 47 <= G["grid_chg_a"] <= 49 and G["grid_legs"][0]["a"] > 13 and G["rectifier"] > 2400 and "from grid" in G["status"],
                  f"during a grid charge: grid power per leg, grid charging 48 A ({G['grid']} W, {G['grid_legs']}, {G['grid_chg_a']} A, {G['status']})"))
results.append(ok(D["grid"] == 0 and L["grid"] == 0, "no grid power on the solar and battery samples"))
I = powland.ident(DUMP)
results.append(ok(I["brand"] == "SUNGERY" and I["serial"] == "ZL-2608201236-00182" and I["built"] == "Sep 28 2026 18:05:14" and I["hw"] == [405, 308],
                  f"identity: brand, serial, build date, hardware versions ({I})"))
st = {x["reg"]: x for x in powland.norm_settings(DUMP)}
results.append(ok(st[41001]["value"] == 120.0 and st[41007]["value"] == 250.0 and st[41009]["value"] == 48.0 and st[41101]["value"] == 58.4
                  and st[41110]["value"] == 44.8 and st[41116]["value"] == 5 and st[41104]["value"] == 120 and st[41104]["group"] == "charge",
                  "settings decode to what SmartValue shows (120 V, 250 A, 48 A, 58.4 V, 44.8 V, 5%, 120 min)"))

modes = {x["reg"]: x for x in powland.norm_settings(DUMP) if x["kind"] == "select"}
results.append(ok(modes[41000]["value"] == 2 and powland.fmt(modes[41000], 2) == "Battery first (Battery)" and modes[41000]["writable"]
                  and powland.fmt(modes[41006], 2) == "Solar and grid together (SNU)" and len(modes[41006]["option_help"]) == 4,
                  "operating modes: output 2 = Battery first, charging 2 = SNU (as SmartValue shows), with descriptions"))
results.append(ok(all(t != "Hybrid" and "Hybrid" not in t for _v, t in modes[41000]["options"]), "Hybrid (the mode that can export) isn't offered"))
hyb = powland.norm_settings({**DUMP, 41000: 3})[0]
results.append(ok(not hyb["writable"] and powland.fmt(hyb, 3).startswith("Hybrid"), "an inverter already in Hybrid shows it, and the mode can't be changed here"))
def refused(x, v):
    try: powland.encode(x, v); return False
    except ValueError: return True
results.append(ok(refused(modes[41000], 3) and refused(modes[41006], 7) and refused(st[41001], 110) and powland.encode(st[41119], 30) == 30,
                  "only offered values and changeable settings are accepted"))

# --- 3. the energy meter
m = powland.new_meter()
t0 = time.mktime((2026, 11, 14, 23, 58, 0, 0, 0, -1))
live = dict(pv=1000, load=500, grid=0, charge=400, discharge=0)
for i in range(3):
    powland.meter_add(m, dict(live, t=t0 + 60 * i))   # 23:58, 23:59, 00:00 (a new day)
results.append(ok(abs(m["days"]["2026-11-14"]["solar"] - 1000 / 60 / 1000) < 1e-9 and abs(m["today"]["solar"] - 1000 / 60 / 1000) < 1e-9 and m["day"] == "2026-11-15",
                  f"energy is counted from power x time and the day rolls over at midnight ({m['days']}, today {m['today']['solar']:.4f})"))
powland.meter_add(m, dict(live, t=t0 + 120 + 600))
results.append(ok(abs(m["today"]["solar"] - 1000 / 60 / 1000) < 1e-9, "a gap of 10 minutes isn't filled in"))
days = powland.meter_days(m)
results.append(ok([d["date"] for d in days] == ["2026-11-14", "2026-11-15"] and set(days[0]) == {"date", *powland.ENERGY_KEYS}, "daily rows in the EG4 month shape"))


# --- 4. the stick, against a fake one
class FakeStick:
    def __init__(self, regs):
        self.regs, self.udp, self.writer, self.redirects, self.requests = regs, None, None, 0, []
        self.offline = False   # True: ignore redirects (stay disconnected)
        self.mute = lambda start, count: False   # reads to leave unanswered (the real one went silent on 41000x64 in Grid first)

    async def start(self):
        loop = asyncio.get_running_loop()
        fake = self
        class P(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                m = re.fullmatch(rb"set>server=([\d.]+):(\d+);", data)
                if m:
                    fake.redirects += 1
                    if fake.writer is None and not fake.offline:
                        asyncio.ensure_future(fake.connect(m[1].decode(), int(m[2])))
        self.udp, _ = await loop.create_datagram_endpoint(P, local_addr=("127.0.0.1", 0))
        return self.udp.get_extra_info("sockname")[1]

    async def connect(self, host, port):
        reader, self.writer = await asyncio.open_connection(host, port)
        w = self.writer
        w.write(powland.frame(999, 1, powland.heartbeat_payload()))   # unsolicited: its own heartbeat
        try:
            while True:
                tid, _d, length, _a, fc = struct.unpack(">HHHBB", await reader.readexactly(8))
                body = await reader.readexactly(length - 2)
                if fc != 4:
                    continue
                _slave, fn, start, count = struct.unpack(">BBHH", body[:6])
                self.requests.append((fn, start, count) if fn == 6 else (start, count))
                if fn == 3 and self.mute(start, count):
                    continue
                if fn == 6:   # write one register: count is the value; like the real one, 41119 must be 20 above 41118
                    if start == 41119 and count < self.regs.get(41118, 10) + 20:
                        out = bytes([1, 0x86, 0x03])
                    else:
                        self.regs[start] = count
                        out = body[:6]
                elif count > 64:
                    out = bytes([1, 0x83, 0x0A])
                elif start <= 30090 and start + count > 30109:   # a span the real inverter refuses in one request
                    out = bytes([1, 0x83, 0x02])
                else:
                    vals = [self.regs.get(a, 0) for a in range(start, start + count)]
                    out = bytes([1, 3, 2 * count]) + struct.pack(f">{count}H", *vals)
                w.write(powland.frame(tid + 500, 4, b"\x01\x03\x02\x00\x00"))   # a stray reply to someone else's request
                w.write(powland.frame(tid, 4, out + powland.crc16(out)))
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self.writer = None

    def hang_up(self):
        if self.writer:
            self.writer.close()
            self.writer = None

    def stop(self):
        self.hang_up()
        self.udp.close()


ALL = {**DUMP, **DAY}


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


async def stick_tests():
    fake = FakeStick(ALL)
    powland.REDIRECT_PORT, powland.PORT = await fake.start(), free_port()
    powland.REDIRECT_SECS, powland.SETTLE_SECS = 0.3, 0.2
    k = powland.Stick("127.0.0.1", "127.0.0.1")
    try:
        await k.read(30000, 1)
        results.append(ok(False, "reading before the stick connects raises"))
    except powland.PowlandError:
        results.append(ok(True, "reading before the stick connects raises"))
    await k.start()
    results.append(ok(await k.wait_connected(5) and fake.redirects == 1, f"the redirect makes the stick connect here ({fake.redirects} redirect)"))
    regs = await k.read_blocks(powland.LIVE_BLOCKS)
    results.append(ok(regs[30071] == DAY[30071] and regs[30000] == DAY[30000] and regs[39637] == DAY[39637], "live blocks read, stray frames ignored"))
    await asyncio.sleep(1)
    results.append(ok(fake.redirects == 1, "no more redirects while it's connected (they make the real stick hang up)"))
    fake.requests.clear()
    regs = await k.read_blocks([(30060, 50)])
    results.append(ok(len(regs) == 50 and regs[30109] == ALL.get(30109, 0) and (30060, 50) in fake.requests and len(fake.requests) > 1,
                      f"a refused block is split until it reads ({fake.requests})"))
    results.append(ok(await k.read(30000, 70) is None, "more than 64 registers: refused, returns None"))
    powland.REPLY_SECS = 0.5
    fake.mute = lambda start, count: start == 41000 and count > 32
    regs = await k.read_blocks([(41000, 64)])
    results.append(ok(len(regs) == 64 and regs[41001] == 1200, "a long block that gets no answer is split into smaller reads"))
    fake.mute = lambda start, count: start <= 41000 < start + count
    try:
        await k.read_blocks(powland.MODE_BLOCK); results.append(ok(False, "a small read that gets no answer raises"))
    except powland.PowlandTimeout:
        results.append(ok(True, "a small read that gets no answer raises"))
    fake.mute = lambda start, count: False
    await k.write(41006, 3)
    results.append(ok(fake.regs[41006] == 3 and (await k.read(41006, 1)) == {41006: 3}, "a setting is written (function 6) and reads back"))
    try:
        await k.write(41119, 25); results.append(ok(False, "a value the inverter refuses raises"))
    except powland.PowlandError as e:
        results.append(ok("outside the range" in str(e) and fake.regs[41119] == ALL[41119], f"a value the inverter refuses raises ({e})"))
    n = len(fake.requests)
    try:
        await k.write(41001, 1100); results.append(ok(False, "a register outside the allowed list is never sent"))
    except powland.PowlandError:
        results.append(ok(len(fake.requests) == n, "a register outside the allowed list is never sent"))
    fake.regs[41006] = ALL[41006]
    fake.offline = True
    fake.hang_up()
    await asyncio.sleep(0.2)
    try:
        await k.read(30000, 1)
        results.append(ok(False, "a read after the stick hung up raises"))
    except powland.PowlandError as e:
        results.append(ok(True, f"a read after the stick hung up raises ({e})"))
    fake.offline, redirects = False, fake.redirects
    results.append(ok(await k.wait_connected(5) and fake.redirects == redirects + 1 and (await k.read(30002, 1)) == {30002: DAY[30002]},
                      "after it hangs up it's redirected again and reconnects"))
    await k.close()
    fake.stop()

asyncio.run(stick_tests())

# --- 5. the server with a POWLAND set up
import server
from fastapi.testclient import TestClient
H = {"origin": "http://solar.example.org"}


def wait(c, cond, secs=10):
    end = time.time() + secs
    while time.time() < end:
        s = c.get("/api/state").json()
        if cond(s): return s
        time.sleep(.2)
    return s


with TestClient(server.app, base_url="http://solar.example.org") as c:
    fake = FakeStick(ALL)
    powland.REDIRECT_PORT, powland.PORT = c.portal.call(fake.start), free_port()
    s = c.get("/api/state").json()
    results.append(ok(not s["configured"] and s["model"]["kind"] == "eg4", "nothing set up yet"))
    r = c.post("/api/powland", json={"ip": "not an ip"}, headers=H)
    results.append(ok(r.status_code == 400, f"a bad address is refused ({r.json()['detail']})"))
    r = c.post("/api/powland", json={"ip": "127.0.0.1", "advertise": "127.0.0.1"}, headers=H)
    results.append(ok(r.status_code == 200, f"POWLAND set up ({r.status_code} {r.text[:120]})"))
    s = wait(c, lambda s: s["live"] and s["live"].get("bus1"))
    results.append(ok(s["configured"] and s["model"]["kind"] == "powland" and s["model"]["quick_charge"] is True and s["connection"]["powland_connected"]
                      and s["live"]["soc"] == DAY[30002] and s["live"]["legs"][0]["w"] == DAY[30028] and s["pv_max"] == powland.PV_MAX,
                      f"live readings come from the POWLAND ({s['connection']}, error {s['error']})"))
    results.append(ok(s["live"]["serial"] == "ZL-2608201236-00182" and s["model"]["ident"]["brand"] == "SUNGERY", "the serial and build date are read once"))
    results.append(ok(s["energy"]["today"].keys() >= {"solar", "used", "charged"} and s["battery"]["soc"] == DAY[30002], "energy and battery in the dashboard's shapes"))
    inv = c.get("/api/inverter").json()
    results.append(ok(inv["source"] == "powland" and len(inv["settings"]) == 24 and sum(x["writable"] for x in inv["settings"]) == 4,
                      f"inverter settings page: the POWLAND's 24 settings, 4 of them changeable ({len(inv['settings'])})"))
    keys = {x["regs"][0]: x for x in inv["settings"]}
    results.append(ok(inv["can_write"] and keys[41000]["writable"] and keys[41006]["writable"] and keys[41119]["writable"] and not keys[41101]["writable"]
                      and keys[41000]["option_help"] and inv["groups"][0]["id"] == "modes",
                      "operating modes and the two battery levels are changeable; charging voltages aren't"))
    r = c.post("/api/inverter", json={"changes": {keys[41000]["key"]: 0, keys[41119]["key"]: 40}}, headers=H)
    j = r.json()
    results.append(ok(r.status_code == 200 and fake.regs[41000] == 0 and fake.regs[41119] == 40 and len(j["changed"]) == 2
                      and j["changed"][0]["after"] == "Solar first (PV)" and any(e["label"] == keys[41119]["label"] and e["after"] == "40 %" for e in j["changes"]),
                      f"changes are written, read back and logged ({j.get('changed') or j})"))
    r = c.post("/api/inverter", json={"changes": {keys[41119]["key"]: 25}}, headers=H)
    results.append(ok(r.status_code == 400 and "outside the range" in r.json()["detail"] and fake.regs[41119] == 40, f"the inverter's refusal is passed on ({r.json()['detail']})"))
    r = c.post("/api/inverter", json={"changes": {keys[41101]["key"]: 57.6}}, headers=H)
    results.append(ok(r.status_code == 400, f"a read-only setting can't be changed ({r.json()['detail']})"))
    r = c.post("/api/inverter", json={"changes": {keys[41000]["key"]: 3}}, headers=H)
    results.append(ok(r.status_code == 400 and fake.regs[41000] == 0, f"Hybrid can't be chosen ({r.json()['detail']})"))
    results.append(ok(c.get("/api/days").status_code == 200 and c.get("/api/months").status_code == 200, "day and month history come from the meter, not EG4"))
    server.S.last_hist = 0
    c.portal.call(server.S.wake.set)
    time.sleep(1.5)
    rows = c.get("/api/history?hours=1").json()
    results.append(ok(rows and rows[-1].get("l1w") == DAY[30028] and rows[-1].get("l2v") == 120.1 and rows[-1].get("bus1") == 250.9,
                      f"history rows keep each leg and the DC bus ({rows[-1] if rows else None})"))
    # --- grid charging now ("Quick charge"): output "Grid first" for the chosen time, then the owner's settings back
    fake.regs[41000], fake.regs[41006] = 2, 2
    results.append(ok(s["model"]["quick_charge"] and not (s["quick"] or {}).get("active"), "the Quick charge card is offered for the POWLAND"))
    s = c.post("/api/charge", json={"start": True, "minutes": 60, "target": 95}, headers=H).json()
    q = server.S.settings.get("pl_quick") or {}
    results.append(ok(fake.regs[41000] == 1 and fake.regs[41006] == 2 and s["quick"]["active"] and 59 <= s["quick"]["remain_min"] <= 60
                      and q.get("restore") == {"41000": 2} and s["quick_target"] == 95,
                      f"start: the house moves onto the grid for an hour, the old mode is remembered ({s['quick']}, {q.get('restore')})"))
    s = c.post("/api/charge", json={"start": False}, headers=H).json()
    results.append(ok(fake.regs[41000] == 2 and not s["quick"]["active"] and "pl_quick" not in server.S.settings, "stop: Battery first is back"))
    fake.regs[41006] = 3
    c.post("/api/charge", json={"start": True, "minutes": 30}, headers=H)
    results.append(ok(fake.regs[41000] == 1 and fake.regs[41006] == 2, "with 'Solar only' charging, it's set to solar and grid together for the charge"))
    server.S.settings["pl_quick"]["until"] = time.time() - 1   # the time is up
    c.portal.call(server.S.wake.set)
    s = wait(c, lambda s: not s["quick"]["active"])
    results.append(ok(fake.regs[41000] == 2 and fake.regs[41006] == 3 and "finished" in s["notices"][0]["msg"], f"when the time is up both are put back ({s['notices'][0]['msg']})"))
    fake.regs[41006] = 2
    c.post("/api/charge", json={"start": True, "minutes": 30}, headers=H)
    fake.regs[41000] = 0   # changed by hand during the charge
    c.post("/api/charge", json={"start": False}, headers=H)
    results.append(ok(fake.regs[41000] == 0, "a mode changed by hand during the charge is kept, not overwritten"))
    fake.regs[41000] = 2
    c.post("/api/charge", json={"start": True, "minutes": 120, "target": 40}, headers=H)   # the battery is at 47%
    c.portal.call(server.S.wake.set)
    s = wait(c, lambda s: not s["quick"]["active"])
    results.append(ok(fake.regs[41000] == 2 and not s["quick"]["active"] and "automatically" in s["notices"][0]["msg"], f"it stops at the target level ({s['notices'][0]['msg']})"))
    s = c.post("/api/charge", json={"start": True}, headers=H).json()
    q = server.S.settings["pl_quick"]
    results.append(ok(q["full"] and 11.9 * 3600 < q["until"] - time.time() <= 12 * 3600 and s["quick"]["active"], "until full: 12 hours at most"))
    c.post("/api/charge", json={"start": False}, headers=H)
    # the real inverter stopped answering the long settings read while in Grid first: Stop must still work
    powland.REPLY_SECS = 0.5
    fake.mute = lambda start, count: fake.regs.get(41000) == 1 and start <= 41000 and start + count > 41032
    c.post("/api/charge", json={"start": True, "minutes": 30}, headers=H)
    r = c.post("/api/charge", json={"start": False}, headers=H)
    results.append(ok(r.status_code == 200 and fake.regs[41000] == 2 and not r.json()["quick"]["active"],
                      f"Stop works when the inverter won't answer a long settings read in Grid first ({r.status_code} {r.json().get('detail', '')})"))
    c.post("/api/charge", json={"start": True, "minutes": 30}, headers=H)
    fake.mute = lambda start, count: fake.regs.get(41000) == 1 and start <= 41000 < start + count   # no settings read at all
    r = c.post("/api/charge", json={"start": False}, headers=H)
    results.append(ok(r.status_code == 200 and fake.regs[41000] == 2, f"and even with no settings read at all, Battery first is written back ({r.status_code})"))
    fake.mute = lambda start, count: False
    fake.regs[41000] = 3
    r = c.post("/api/charge", json={"start": True, "minutes": 30}, headers=H)
    results.append(ok(r.status_code == 502 and "Hybrid" in r.json()["detail"] and fake.regs[41000] == 3, f"in Hybrid mode it won't start ({r.json()['detail']})"))
    fake.regs[41000] = 2
    s = c.post("/api/powland", json={"ip": ""}, headers=H).json()
    results.append(ok(not s["configured"] and s["model"]["kind"] == "eg4" and server.S.stick is None, "turned off"))
    results.append(ok((Path(os.environ["EG4_DATA_DIR"]) / "energy.json").exists(), "the energy totals are saved when it's turned off"))
    c.portal.call(fake.stop)

print(f"\n{sum(results)} of {len(results)} passed")
sys.exit(0 if all(results) else 1)
