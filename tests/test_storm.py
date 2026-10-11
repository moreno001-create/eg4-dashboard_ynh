"""Storm watch rules with fake alerts and forecasts (no network). Run from the repo root: python tests/test_storm.py"""
import asyncio, os, sys, tempfile, time
from pathlib import Path
REPO = str(Path(__file__).resolve().parent.parent)
os.environ["EG4_DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, REPO)
import server, storm as sw

now = time.time()
H = lambda h: round(now + h * 3600)
cfg = dict(sw.DEFAULTS, enabled=True)
# 1. which alerts and forecast hours count
alerts = [dict(id="a1", event="Severe Thunderstorm Watch", severity="Severe", headline="until 9 PM", onset=H(2), ends=H(8)),
          dict(id="a2", event="Beach Hazards Statement", severity="Moderate", headline="", onset=H(0), ends=H(10)),
          dict(id="a3", event="Wind Advisory", severity="Moderate", headline="", onset=H(1), ends=H(5))]
hours = [dict(t=H(h), code=95 if h in (20, 21) else 3, gust=50 if h == 30 else 10) for h in range(0, 40)]
hz = sw.hazards(alerts, hours, cfg, now)
print("hazards:", [(h["title"], round((h["start"] - now) / 3600, 1)) for h in hz])
print("trigger (12 h lead):", sw.next_trigger(hz, cfg, now)["title"])
print("trigger (1 h lead):", sw.next_trigger(hz, dict(cfg, lead_hours=1), now))

# 2. the controller, in demo mode with a fake weather feed
S = server.S
S.settings.update(demo=True, storm=dict(cfg))
soc = {"v": 60}
server.current_soc = lambda: soc["v"]
async def fake_alerts(lat, lon): return [alerts[0]]
async def fake_fc(): return {"hours": []}
sw.fetch_alerts, server.get_forecast = fake_alerts, fake_fc
async def run():
    S.alerts_at = 0
    await server.storm_tick(); print("tick 1 -> charging:", S.demo.quick, "| target:", S.settings["quick_target"], "|", S.log[-1]["msg"])
    S.demo.quick = False; soc["v"] = 100
    await server.storm_tick(); print("tick 2 (reached target) ->", S.log[-1]["msg"])
    await server.storm_tick(); print("tick 3 (same storm) -> charging again?", S.demo.quick)
    # a new storm; the owner stops the charge at 70%
    S.alerts, S.alerts_at = [], time.time()
    S.settings["storm"]["use_alerts"] = False
    sw_hours = [dict(t=H(3), code=96, gust=10)]
    async def fc2(): return {"hours": sw_hours}
    server.get_forecast = fc2
    soc["v"] = 55
    await server.storm_tick(); print("tick 4 (forecast storm) -> charging:", S.demo.quick, "|", S.log[-1]["msg"])
    S.demo.quick = False; soc["v"] = 70
    await server.storm_tick(); print("tick 5 (owner stopped) ->", S.log[-1]["msg"])
    await server.storm_tick(); print("tick 6 -> restarted?", S.demo.quick)
    # battery already full when a storm comes
    sw_hours[:] = [dict(t=H(5), code=99, gust=10)]; soc["v"] = 99
    await server.storm_tick(); print("tick 7 (already full) -> charging:", S.demo.quick, "|", S.log[-1]["msg"])
    # far-off storm: outside the 12 h lead
    sw_hours[:] = [dict(t=H(30), code=95, gust=10)]; soc["v"] = 40
    await server.storm_tick(); print("tick 8 (storm in 30 h) -> charging:", S.demo.quick, "| status:", server.storm_payload()["status"])
asyncio.run(run())
