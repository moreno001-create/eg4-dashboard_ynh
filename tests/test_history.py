"""The Charts page's data: the extra readings (output voltage, DC bus, heatsinks, cells), the history row that keeps them,
averaging long ranges down, and /api/history. Run from the repo root: python tests/test_history.py"""
import os, sys, tempfile, time
from pathlib import Path
os.environ["EG4_DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import eg4, local, server
from fastapi.testclient import TestClient

ok = lambda cond, what: print(("PASS " if cond else "FAIL ") + what) or bool(cond)
results = []

# input registers read from the owner's real 6000XP on 2026-10-10 (the ones the new readings use)
REGS = {1: 3997, 2: 1343, 4: 533, 5: 48, 7: 94, 8: 166, 10: 5403, 12: 2403, 15: 5997, 17: 5307, 20: 2401, 23: 5995, 27: 6976,
        38: 4345, 39: 3947, 64: 31, 65: 27, 66: 31, 67: 29, 81: 1000, 82: 1000, 101: 3336, 102: 3332, 170: 1592}
L = local.norm_live(REGS)
results.append(ok((L["v_eps"], L["hz_eps"], L["bus1"], L["bus2"], L["t_rad1"], L["t_rad2"], L["cell_max"], L["cell_min"], L["bms_chg_a"])
                  == (240.1, 59.95, 434.5, 394.7, 27, 31, 3.336, 3.332, 100.0),
                  "Wi-Fi: output 240.1 V / 59.95 Hz, DC bus 434.5 / 394.7 V, heatsinks 27 / 31 C, cells 3.336 / 3.332 V, BMS 100 A"))
rt = eg4.norm_runtime(dict(vepsr=2401, feps=5995, vBus1=4345, vBus2=3947, tradiator1=27, tradiator2=31, vacr=2403, fac=5997))
results.append(ok((rt["v_eps"], rt["hz_eps"], rt["bus1"], rt["t_rad2"]) == (240.1, 59.95, 434.5, 31), "the cloud's runtime gives the same readings"))

row = server.history_row(L)
results.append(ok(row["v1"] == 399.7 and row["v2"] == 134.3 and row["pv1"] == 94 and row["vb"] == 53.3 and row["veps"] == 240.1
                  and row["bus1"] == 434.5 and row["cmax"] == 3.336 and row["rec"] == 5307 and row["chg"] == 5403,
                  f"the history row keeps string voltages, battery, output, bus, cells and grid charging ({len(row)} fields)"))
server.S.battery = dict(bank=dict(cell_max=3.4, cell_min=3.39))
row = server.history_row(dict(L, cell_max=None, cell_min=None))
results.append(ok(row["cmax"] == 3.4 and row["cmin"] == 3.39, "without the dongle's cell readings, the cloud's battery data fills them in"))

rows = [dict(t=1000 + 60 * i, pv=i, vb=50 + (i % 2)) for i in range(1440)] + [dict(t=1000 + 60 * 1440, pv=0)]
ds = server.downsample(rows, 100)
results.append(ok(90 <= len(ds) <= 110 and abs(ds[0]["vb"] - 50.5) < .05 and "vb" not in ds[-1], f"a day of minutes averaged to {len(ds)} rows, fields averaged on their own"))
results.append(ok(server.downsample(rows[:50], 100) == rows[:50], "short ranges are returned as they are"))

with TestClient(server.app) as c:
    c.post("/api/demo", json={"on": True})
    week = c.get("/api/history?hours=168&points=600").json()
    hour = c.get("/api/history?hours=1").json()
    results.append(ok(550 <= len(week) <= 610 and week[0]["t"] < time.time() - 6.5 * 86400 and "bus1" in week[-1],
                      f"demo: 7 days averaged to {len(week)} rows with every reading"))
    results.append(ok(50 <= len(hour) <= 62 and all(r["t"] >= time.time() - 3700 for r in hour), f"demo: the last hour, every minute ({len(hour)} rows)"))

print(f"\n{sum(results)} of {len(results)} passed")
sys.exit(0 if all(results) else 1)
