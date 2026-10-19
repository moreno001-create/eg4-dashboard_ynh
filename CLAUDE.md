# Handoff notes for Claude (or any developer)

Read this, then `README.md`. This file records how the code works, why it's built this way, and what's verified or still open.

## START HERE: where we left off (end of 2026-10-10)
Written by the Claude Code session on the owner's Windows PC for the next Claude, most likely on the owner's **Mac**.

### Current state in one glance
- **Working and in daily use with the owner's real EG4 account,** run locally on the Windows PC (`python server.py`, http://127.0.0.1:8766). **Not yet installed on YunoHost.**
- **Verified against the real 6000XP:**
  - live values, battery % and power, the grid charge
  - **timed Quick Charge works**; remaining time is in seconds
  - monthly history (solar = `ePv1Day` + `ePv2Day`)
  - bank-wide battery cell data (no per-module data)
  - no grid export (removed everywhere at the owner's request)
- **Power flow:**
  - Default: the owner's **house illustration** (`house.webp`), with glowing spheres on its wires, live labels, and a **sky that follows the real sun and weather** (night, moon, stars, dusk, clouds, rain).
  - The Sankey diagram stays available in Settings → Power flow.
- **Weather + solar forecast** (Open-Meteo, calibrated on the system's own production; default Downey, CA, with a ZIP code box) is built.
- **Direct Wi-Fi connection: built (2026-10-10, on the Mac).** Wi-Fi first, with the EG4 cloud as automatic fallback. See Task 2 and "Wi-Fi dongle".
- **Public install copy created (2026-10-10):** `moreno001-create/eg4-dashboard_ynh`. The owner OK'd publishing it as-is: serials, MAC, house picture and the Downey default all included.
  **Next:** install it on YunoHost (see "Other open items").

### Task 1: Voltline look (mostly done 2026-10-10, from iPhone screenshots)
- **Voltline** is the owner's own iOS/iPadOS app for this inverter. It runs on the iPad **"iPad Refrigerator"** and their iPhone, and its source is probably **on the Mac**.
- **Done:** `index.html` was rebuilt to look like Voltline, working from 6 iPhone screenshots the owner emailed. It has:
  - a black iOS-style theme, a big title, and a status chip
  - a Sankey power flow, now the "Diagram" option; the house picture is the default
  - cards in Voltline's order: MPPT PV1/PV2, Battery (gradient bar), Solar generation / Consumption / Grid usage sparklines,
    Quick charge (30 min / 1 / 2 / 4 h / until full + optional stop %), Energy independence, Lifetime (MWh), Solar today (stacked PV1/PV2 with peak),
    History (10 min / 1 h / 6 h / 24 h), and Inverter (V / Hz / °C / status)
  - extra cards: Battery modules, Last 30 days, Last 12 months, Alerts
  - Arrange (move up/down, hide), Refresh, and a settings sheet (theme, EG4 login, demo)
  - Light mode as an iOS-light counterpart
- **Left for the Mac session (optional polish):** compare against the real Voltline source and screens, and match exact colors, fonts and spacing.
  - The Forecast card is built, using Open-Meteo rather than EG4's forecast (Voltline's showed 0.0). See "Weather & solar forecast".
  - Not built: **drag**-to-arrange. This version uses up/down buttons.
  - Optionally push Voltline to a private `moreno001-create/voltline` repo with a `design/` folder, and no secrets. Ask the owner first.
- **Colors:** Voltline's blue (home) and cyan (grid) are too close for some colorblind viewers. So in charts, grid is drawn **dashed**, and every series has a legend label and a tooltip. Keep that.
- Quick charge now sends EG4's `minute` parameter for timed charges, the same as Voltline does. The optional stop-% check still runs on the server.

### Task 2: direct Wi-Fi connection to the 6000XP (built and verified on the Mac, 2026-10-10)
- **Done.** `local.py` reads the inverter straight from its Wi-Fi dongle on TCP 8000 (Plan A worked: the port is open). Live readings arrive every 5 s,
  and the **EG4 cloud takes over automatically** when the dongle can't be reached. See "Wi-Fi dongle" below.
- **The owner's dongle:** serial `DJ42600229`, MAC `f0:9e:9e:c9:f8:68`, at 192.168.4.26 on 2026-10-10. Inverter serial `4373740847`.
- **Verified against the real dongle from the Mac:**
  - All 212 input registers read in about 1.6 s.
  - The values matched what the system was doing (grid charging at about 5.4 kW, 48-56%, 240 V, 59.97 Hz, cells 3.33 V).
  - The dashboard showed "Wi-Fi · EG4 6000XP" and refreshed every few seconds.
- **Not verified yet:**
  - Fallback with the real cloud login. Only the fake-server test covers it, because the Mac has no EG4 login saved.
  - The YunoHost install with local.py.
- **Open:**
  - Set a DHCP reservation for the dongle's MAC in the router, as a belt-and-braces measure.
  - Plan B (RS485) isn't needed.
  - **Stop over Wi-Fi: built 2026-10-10 at the owner's request** (see "Stopping a grid charge over Wi-Fi"). Starting is still cloud-only.

### Run it (Mac or anywhere)
```
pip3 install -r requirements.txt        # fastapi, uvicorn, aiohttp (Python 3.11+)
python3 server.py                       # opens http://127.0.0.1:8766, click "Try demo mode"
python3 tests/test_server.py            # needs: pip3 install httpx2; runs against a fake EG4 server
python3 tests/test_local.py             # the Wi-Fi dongle path, against a fake dongle
python3 tests/test_settings.py          # inverter settings: map, writes, the EG4 check, admin rule
python3 tests/test_history.py           # charts data: extra readings, history rows, averaging
```
Open `http://127.0.0.1:8766/#battery` (or `#history`, `#alerts`) to land on a tab. Demo data is simulated, so no EG4 account is needed.

### Other open items (owner's decisions pending)
- **Installing on YunoHost:** the public copy `eg4-dashboard_ynh` now exists. Install it from the web admin (Install custom app → that URL).
  Upgrades must be run over SSH, because the web admin won't upgrade custom apps:
  `sudo yunohost app upgrade eg4solar -u https://github.com/moreno001-create/eg4-dashboard_ynh`.
  Then point the water heater's "Solar server address" at `http://127.0.0.1:8766`.
- **Real-account check:** nobody has run this against the owner's real EG4 account yet. Compare a few numbers with the EG4 app:
  solar, home use, battery %, today's kWh, and 30-day history. See the "Verify" notes under EG4 cloud API.
- **Theme switch for the water-heater app:** offered, but no answer from the owner yet.
- **Sibling project:** `moreno001-create/water-heater-server` (private; public install copy `water-heater-server_ynh`) is installed and working on
  the owner's YunoHost server. It has its own `CLAUDE.md`.

## Charts page (added 2026-10-18)
The owner asked for "a page with all the graphs for voltages from the different systems and wattages". `#charts` view (chart button).
- **History rows** (`server.history_row`, short keys in `HIST_KEYS`): besides t/pv/load/soc/battery/grid/inv/pv1/pv2, now v1/v2 (string
  volts), vb, vac, hz, veps/heps (inverter output = house), bus1/bus2 (DC bus), ti/tr1/tr2/tb (inside, heatsinks, battery), chg/dis/rec
  (charge, discharge, grid charging W), cmax/cmin (cells; from the dongle, else the cloud's battery bank). Older rows lack them.
- **New live readings:** Wi-Fi input regs 20 (EPS V), 23 (EPS Hz), 38/39 (bus, 0.1 V), 65/66 (heatsinks), 101/102 (cells, mV), 81/82 (BMS
  current limits, 0.1 A); checked against the owner's captured registers (240.1 V, 59.95 Hz, 434.5/394.7 V, 27/31 C, 3.336/3.332 V,
  100 A). Cloud: vepsr, feps, vBus1/2, tradiator1/2. Cloud frequencies were being rounded to 0.1 Hz; now 0.01.
- **`GET /api/history?hours=&points=`**: up to 168 h; `points` averages buckets field by field (`downsample`). The page asks for 600.
  Demo history takes `hours` and fills every field.
- **Page:** `lineChart()` (own y-range, not from zero unless `zero`, gaps over max(15 min, 3 buckets), 0 V/Hz treated as missing via
  `nz`), sections Power / Solar / Battery / Grid and output / Inverter (`CHARTS`), a now/low/average/high table per chart, currents
  worked out as W / V (the dongle's own current registers read 0 on this model). Refreshes every minute while open.
- Tests: `tests/test_history.py` (8). Views are routed by `VIEWS` in index.html (`#charts`, `#inverter`).

## Inverter settings (added 2026-10-18)
The owner asked for a self-hosted replacement for monitor.eg4electronics.com's control side, run from Homeport, talking to the
dongle locally, covering **everything EG4 shows** (their choice over "everyday settings only"), with explanations of what each
option does. This supersedes "monitor-only" below.
- **`inverter_settings.py`**: 134 settings in 10 groups (`GROUPS`), each a `Setting` (register, bit/width, div, signed, range,
  options, risk `""|caution|danger`, help text). Kinds: number, switch (one bit), select (whole register or a bit field), window
  (two registers, start/end packed `hour | minute << 8`), plus read-only info.
  - **Map sources:** pylxpweb's live probe of an SNA12K-US (12000XP, same off-grid family, device type 54) through EG4's remoteRead
    (`docs/inverters/SNA12KUS_52XXXXXX68.json`: in a multi-register block the parameter sits at the block's **last** register,
    e.g. `92-99` = reg 99), pylxpweb's holding map for bit positions, and this project's Quick Charge findings. pylxpweb's
    18kPV-style entries for regs 68-73 (separate hour/minute) are **wrong for this family**: they're three packed windows.
  - **Left out on purpose:** bits whose position isn't known (reg 110 `FUNC_GO_TO_OFFGRID`/`BIT_WORKING_MODE`, reg 229/224-226
    bits, `FUNC_SNA_BAT_DISCHARGE_CONTROL`), battery type (HOLD_MODEL bitfield, regs 0-1), reg 22 (also carries LSP bits),
    reg 234 (reads 0 on this model). Output voltage/frequency (90/91) are shown read-only: their raw encoding is unclear.
  - Option labels (output priority, line mode, charge type) come from pylxpweb/EG4 docs; the check proves the raw value matches
    EG4's, not that the label is right. Settings marked `UNDOC` say so in their help.
- **Safety check** (`POST /api/inverter/check`, `run_check()`): reads every settings register over Wi-Fi, asks EG4 for
  remoteRead 0/127/254 (127 each), and `inv.compare()`s this map's decoding with EG4's: `match` (unlocked), `weak` (0/off on both:
  then a one-register remoteRead must report the key at that register to count as a match), `raw` (EG4 reports the raw number:
  our scale is wrong), `mismatch`, `missing`. Only `match` and `verified` (window 3 / regs 68-73, reg 120 bits 1-3, reg 159) are
  writable. Results live in `settings.settings_check` (tied to the serial). Re-run after a firmware update.
- **Writes** (`POST /api/inverter {changes, confirm_danger}`): Wi-Fi only. Fresh read of the touched registers, `inv.encode()`
  (range/step check; bit fields keep the register's other bits), then `Dongle.write_holding()`: only registers in
  `inv.writable_registers()`, only those that differ, each checked against the echo, all read back. Logged to `changes.jsonl`
  (who from `Remote-User`) and as a notice. Danger settings need `confirm_danger`.
- **Who:** `EG4_ADMIN_GROUP` (Homeport catalog sets `admins`) means only that group in `Remote-Groups` may change settings or run
  the check; unset = anyone who can open the app.
- **Without the dongle** `GET /api/inverter` shows EG4's own values (remoteRead), read-only. Demo mode has simulated registers
  (`inv.DEMO`).
- **Page:** sliders button in the top bar → `#inverter` view: safety-check card, search, group chips, one card per group, rows with
  help, badges (verified / checked / locked + why / careful / installer) and Details (EG4 name, register, range). Edits collect in a
  bar; "Review and apply" shows before → after.
- **Not verified on the real inverter yet** (2026-10-18): the Mac was on 192.168.0.x and the dongle (last at 192.168.4.26 on the
  old eero network) didn't answer there, and no EG4 login is saved on the Mac. Tests: `tests/test_settings.py` (37 checks: map,
  fake dongle writes, check statuses, admin rule, cloud fallback). **First thing on the real system: run the safety check and
  look at what stays locked.**

## Docker image (added 2026-10-18, for Homeport)
- `Dockerfile` (python:3.12-slim, runs as uid 1000, data in `/data`, listens on 0.0.0.0:8766) and `.dockerignore` (keeps `data/`, with the
  owner's password, out of the image).
- `.github/workflows/image.yml` builds it in the **public** copy only and publishes `ghcr.io/moreno001-create/eg4-dashboard:latest`
  (amd64 + arm64), after checking the page answers. Publishing the public copy (see above) is what releases a new image.
- In a container the server can't see the home network, so `EG4_NETWORKS` (e.g. `192.168.4.0/22`) names where to sweep for the
  dongle (`local.configured_networks()`); the ARP table is empty there, so the dongle is recognised by its serial after the sweep.
- Homeport's catalog has it as "EG4 Solar" (id `eg4-solar`).

## What this is
A self-hosted dashboard for the owner's **EG4 6000XP** off-grid/hybrid inverter, packaged as a **YunoHost** app (id `eg4solar`,
path `/solar`). Its sibling project is `moreno001-create/water-heater-server`. That one has its own Solar tab and a solar
dump-load controller for the water heater. Both projects have their own copy of the same EG4 client. Single household, one EG4 account.

The owner asked for: live power flow, energy history, battery details, alerts and faults, and otherwise **monitor-only**, with one control:
**start charging the battery from the grid** (EG4 "Quick Charge"). The optional auto-stop at a chosen battery level was added on top.

## Layout
| File | Role |
|---|---|
| `server.py` | FastAPI app: settings, background `poller()`, history log, month cache, auto-stop, REST API, demo switch |
| `forecast.py` | Weather (Open-Meteo) and calibrated solar forecast, ZIP lookup (Zippopotam) |
| `eg4.py` | `EG4Client` (aiohttp), `norm_*` unit conversion, `DemoInverter` (simulated 6000XP + 14.3 kWh bank) |
| `local.py` | Wi-Fi dongle client (TCP 8000), finding it by MAC, `norm_live/energy/bank` in the cloud's shapes, settings reads/writes |
| `inverter_settings.py` | Inverter settings map, explanations, encode/decode, the compare against EG4 (see "Inverter settings") |
| `index.html` | Whole UI, vanilla JS + inline SVG. One page of arrangeable cards in CSS columns (power flow, MPPT, battery, sparklines, quick charge, independence, lifetime, solar today, history, inverter, modules, 30 days, 12 months, alerts) + settings dialog. Charts: `areaChart`, `barChart`; flow: `drawFlow` |
| `manifest.toml`, `scripts/`, `conf/`, `doc/` | YunoHost package (packaging v2, helpers 2.1, YunoHost >= 12). Copied from the water-heater project |
| `tests/test_server.py` | Runs the real code path against a fake EG4 server on port 8812 |
| `tests/test_settings.py` | Inverter settings against a fake dongle and a fake EG4 remoteRead |

## EG4 cloud API (mirrors pylxpweb, MIT, github.com/joyfulhouse/pylxpweb)
pylxpweb can't be used directly: it needs Python >= 3.12, and YunoHost 12 / Debian 12 has 3.11.
All calls are form-encoded POSTs to `https://monitor.eg4electronics.com` and keep a session cookie.
An expired session returns HTML or 401, so the client logs in again once. `success:false` + `msg` means an error.
- `/WManage/api/login` (account, password, language=ENGLISH). The response lists `plants[].{plantId,name,inverters[].serialNum}`.
- `/WManage/api/inverter/getInverterRuntime` (serialNum): live values. Powers in W. `vBat`, `vpv*` and `vacr` are 0.1 V; `fac` is 0.01 Hz; `tinner` and `tBat` are °C.
  - Inverter output = max(`pinv`, `peps`). Home load = `consumptionPower`, or the inverter output when that's 0 (6000XP loads sit on its output). **Verify against the EG4 app.**
- `/WManage/api/inverter/getInverterEnergyInfo` (serialNum): today*/total* in **0.1 kWh** (Yielding, Charging, Discharging, Import, Export, Usage).
- `/WManage/api/battery/getBatteryInfo` (serialNum): bank values + `batteryArray[]`.
  - **On the owner's system `batteryArray` is empty**, and `totalNumber`, `remainCapacity` and `fullCapacity` are 0.
  - It does give bank-wide `globalMax/MinCellVoltage` (mV), `globalMax/MinCellTemp` (0.1 °C) and `bmsCycleCnt`. These feed the "Battery details" card.
- Runtime on the 6000XP reports a stray `vpv3` (~430 V) with `ppv3` = 0, so a third input only shows when it produces power.
  - Module `totalVoltage` is 0.01 V, `current` 0.1 A, cell voltages mV, cell temperatures 0.1 °C.
- `/WManage/api/inverterChart/monthColumn` (serialNum, year, month): one row per day, energies in 0.1 kWh.
  - **Verified on the owner's 6000XP (2026-10-10).** There is **no `ePvDay`**; solar per day = `ePv1Day` + `ePv2Day` (+ `ePv3..8Day`).
  - Present: `eConsumptionDay`, `eToUserDay` (import), `eToGridDay`, `eChgDay`, `eDisChgDay`, `eEpsDay`, `eInvDay`, `eRecDay`, `eLoadDay`, `eGridBuyDay`, `eGridSaleDay`.
- `/WManage/api/analyze/event/list` (page, rows, plantId, serialNum, eventText=_all): `rows[]` with event, eventType, eventText, startTime, status OPEN/CLOSE.
- `/WManage/web/config/quickCharge/{start,stop}` (inverterSn, clientType=WEB) and `.../getStatusInfo` (inverterSn): `hasUnclosedQuickChargeTask`, `remainTimeBeforeQuickChargeStop`.
  - `minute` on start (newer firmware) charges for that long. The UI's 30 min / 1 / 2 / 4 h buttons send it, the same as Voltline does; "Until full" omits it.
    **Confirmed working on the owner's 6000XP (2026-10-10).** `remainTimeBeforeQuickChargeStop` is in **seconds**, and the status
    reads `WAIT_CHARGE` while a timed charge runs.

## Behavior worth knowing
- `poller()` runs whenever an account is saved and demo is off. Timing:
  - Live reading every 60 s, which is also appended to `history.jsonl`.
  - Energy and battery every 5 min. The event log every 15 min.
  - Quick Charge status every 60 s; `auto_stop()` on every tick (every 5 s while the Wi-Fi dongle is live).
- All EG4 calls go through `eg4_call()`, which serializes them with a lock and creates or logs in the client lazily.
- Data stops counting as fresh after 10 minutes without a reading (`stale`). `live.lost` means EG4 has lost the dongle.
- Months that are fully finished (more than 33 days past their start) are cached on disk for good. The current month is cached in memory for 30 minutes.
- The UI uses relative API paths (works under `/solar/`) and reloads if the YunoHost login expired. The origin check trusts the proxy's
  `Host` only when `EG4_DATA_DIR` is set (hosted).

## Wi-Fi dongle (local.py, added 2026-10-10)
- **Protocol:** LuxPower framing (`A1 1A`, protocol, length, `01`, `C2`, dongle serial, data length) around a Modbus RTU frame
  (address 0 = to the inverter / 1 = from it, function, inverter serial, register, count or byte count + values, CRC-16).
  Spec: lxp-bridge wiki; framing and register map ported from pylxpweb (`transports/dongle.py`, `registers/inverter_input.py`).
  **Read-only:** function 4 (input registers) only.
- **Registers used:**
  - **Live:** 1-3 PV V, 7-9 PV W, 4 battery V, 5 SOC (low byte), 10/11 charge/discharge W, 12 grid V, 15 Hz,
    16 inverter W, 17 rectifier W, 24 EPS W, 26/27 to grid / from grid W, 64 inverter temp (signed), 67 battery temp,
    **170 output power = home use** (1592 W while 6976 W came from the grid and 5307 W went into the battery).
  - **Energy:** 28-37 today (0.1 kWh), 40-59 lifetime (32-bit, low word first).
  - **Battery:** 101/102 cell mV, 103/104 cell temps (0.1 °C, signed), 106 cycles.
- **Not trusted on this 6000XP:**
  - 171/172 home-use energy read 0, so they become None and the cloud's figure is kept.
  - 98 BMS current read 39 A while charging at about 100 A, so it isn't shown.
  - Status codes aren't documented, so the status text is described from the power flows ("Solar, charging from grid").
- **Finding it:**
  - By MAC in the ARP table (`/proc/net/arp` on Linux; `arp -a(n)` on Mac/Windows).
  - If the MAC isn't there, it sweeps the local network on port 8000 (which also fills the ARP table) and looks again.
    It sweeps the network's **real size**, read from `/proc/net/route` on Linux and `ifconfig` on Mac, capped at /20.
    The owner's eero network is a **/22** (192.168.4.0-192.168.7.255): the YunoHost server mor1 is at 192.168.6.237 and the dongle at 192.168.4.26,
    so a /24 sweep never found it (fixed 2026-10-10). A full /22 sweep takes about 5 s.
  - Set up by IP (typing the address in the MAC box), the MAC is learned from the ARP table after the first reading.
  - If it's still missing, each open port is asked for its dongle serial (`identify()`).
  - Every frame's dongle serial is checked, so a reused IP is refused.
  - Quirk: Homebrew's Python on the Mac got an **empty** ARP table from `arp` (Apple's python3 got the full table), so the serial fallback matters there.
- **One local client only:**
  - `Dongle` keeps one connection open.
  - A late reply to an earlier request can arrive on a new connection, so replies must match start **and** register count.
    That bug once made the first reading all zeros.
- **Server:**
  - `read_local()` runs first in `poll_once()`. On failure the cloud fills in, and the dongle is retried every `LOCAL_RETRY_SECS` (60 s).
  - The poller runs every `LOCAL_SECS` (5 s) while local, but history still gets one row a minute.
  - The cloud is still used every 5 min for energy, battery and firmware. Quick Charge status runs every 60 s and events every 15 min.
  - The dongle's counters are laid over the cloud's (`merged_energy` / `merged_battery`).
  - Settings → **Wi-Fi dongle**: Find on network (`GET /api/dongle/find`), Use dongle (`POST /api/dongle {mac|ip}`, read once before
    saving), Turn off (`{mac:""}`).
  - The dongle settings are `dongle_mac`, `dongle_serial` and `dongle_ip` (the last address, updated when it moves).
  - `/api/state.connection` = {source, dongle_*, local_error, cloud}.
  - The page refreshes every 5 s while the source is local, and every 30 s otherwise.
- **`GET /api/live`** (read-only, small stable shape: t, pv, soc, charge, discharge, battery, grid, inv, load, v_bat, status, lost, source, age, stale, interval).
  The water-heater-server dump load reads it (its "Solar server address", `http://127.0.0.1:8766` on the same YunoHost box) instead of polling EG4 itself.
  It's the only way other apps should get dongle data, because the dongle takes one local client.
- **Try it:** `python3 local.py f0:9e:9e:c9:f8:68 4373740847`.

### Stopping a grid charge over Wi-Fi (the only write, added 2026-10-10)
- **How Quick Charge really works on this 6000XP.** This was found by snapshotting holding registers 0-239 during a charge and after the owner pressed
  Stop in the EG4 app, then diffing. EG4's Quick Charge is **AC-charge window #3** (regs 72/73, low byte hour, high byte minute) set to start..start+duration.
  Bits 1-3 of reg 120 (charge type) go 0 → 1, and reg 159 (AC-charge end voltage, 0.1 V) goes 54.0 → 59.0.
  Stop put them back: 72/73 = 0, type = 0, 159 = 540.
  - pylxpweb's quick-charge bit (reg 233 bit 0) and minutes (reg 234), and input reg 210, all read **0** on this model, even while charging.
- **`Dongle.stop_quick_charge(normal)`** reads those registers, then writes (function 6) only the ones that differ, window first:
  72=0, 73=0, 120 with bits 1-3 = normal type (other bits kept), 159 = normal end voltage. Every write must match the inverter's echo,
  and everything is read back and compared. `write_request()` refuses any register outside `WRITABLE` = {72, 73, 120, 159}.
- **"Normal" values:** they're learned from the inverter whenever no window is set (`normal_charge_settings`, saved as `settings.charge_normal`).
  The default is type 0 / 54.0 V, which is what EG4's Stop wrote.
- **Server:**
  - `stop_grid_charge()` tries EG4 first when there's a login, and **Wi-Fi when there's no login or EG4 fails**.
  - It's used by the Stop button, the automatic stop at the target %, and storm watch.
  - Without a login, `S.quick` comes from the inverter's own window (`quick_status`: active, remain_min, until).
  - Starting a charge still needs the EG4 login.
- **Verified live 2026-10-10:** the owner started a Quick Charge in the EG4 app (window 17:38-18:38) and pressed Stop on the dashboard, which had no EG4 login.
  It wrote 72: 9745→0, 73: 9746→0, 120: 146→144, 159: 590→540, the same as EG4's own Stop. Charging, rectifier and grid all went to 0 W. Tests: `tests/test_local.py` (fake dongle serving the real registers, plus the fallback switching).

## Power-flow picture (added 2026-10-10)
- **`house.webp`** comes from the owner's own illustration (`house.png.png` on their Desktop). It's cropped to the house scene (1312×860, legend panels removed)
  and compressed to WebP (187 KB). The server serves it at `/house.webp`, and `scripts/_common.sh` copies it on install.
- **`drawHouse()`:**
  - `HOUSE_WIRES` traces the picture's own wires in its 1312×860 pixel space: sunlight, PV DC, battery in/out DC, AC to the home, and grid pole → meter → inverter.
  - Each wire lights up, with glowing spheres, only when that flow is active. Speed and number of spheres scale with power.
  - Opaque live "chips" cover the picture's printed label boxes (positions in %):
    - Solar panels, Utility grid, Home, Battery
    - Inverter: temperature + status, because the 6000XP reports 0 W output while passing grid power through
    - the Meter box shows Independence %
  - On screens ≥ 1100 px the card spans all columns (`column-span:all`), with the picture capped at 58vh tall.
- **Dynamic sky** (`computeSky()` and `drawSky()`) follows the real sun and weather at the forecast location:
  - Phases come from today's sunrise and sunset (Open-Meteo), with ±40 min counted as dawn or dusk. Without a forecast it falls back to solar > 50 W or the clock.
  - **Night:** darker photo, a navy multiply tint, the sun swapped for a crescent moon, twinkling stars, and the window and porch lamp glowing (screen blend).
  - **Dawn/dusk:** a warm multiply tint.
  - **Clouds:** drifting SVG clouds by current cloud cover (25/50/80%). Overcast hides the sun behind a cloud. Rain or storm codes add falling rain.
  - The badge in the card header says Sunny / Partly cloudy / Overcast / Sunrise / Sunset / Night.
  - Preview any look with `?sky=night|dawn|dusk|day&clouds=0-100&rain=1`.
  - It only redraws when the look changes, so clouds keep drifting smoothly.
  - Clouds are clipped to the open sky (`#skyclip`, a polygon above the roof edge, ridge and gable in house.webp pixels), so they pass behind the house. If the picture changes, retrace that polygon.
- **Settings → Power flow:** Picture (default) or Diagram (the Sankey `drawDiagram`), saved per browser.
- If the repo is ever published (the `_ynh` copy), `house.webp` goes with it. Check with the owner first.

## Storm watch (added 2026-10-10)
- **`storm.py` + `storm_tick()` in server.py.** It runs every poll (60 s), in demo mode too. It tops the battery up from the grid before severe weather, in case the grid goes down.
- **Hazard sources, for the weather location:**
  1. **NWS active alerts** (`api.weather.gov/alerts/active?point=lat,lon`, needs a User-Agent; refreshed every 10 min). Only *Watch*/*Warning* events whose name contains
     thunderstorm, tornado, hurricane, tropical storm, high/extreme wind, winter storm, blizzard, ice storm, **red flag** (fire weather / PSPS power shutoffs), flood, storm surge or dust storm.
     Advisories and statements are ignored.
  2. **The Open-Meteo hourly forecast** (next 48 h): thunderstorm codes ≥ 95, or `wind_gusts_10m` ≥ the chosen mph. Consecutive hours are grouped into one hazard.
- **Action:** when a hazard starts within `lead_hours` (default 12) and SOC < target − 2, it starts Quick Charge "until full", with `quick_target` = target (default 100), so the existing `auto_stop()` ends it.
  - Each storm is handled once: `settings.storm_state.handled` holds the keys and survives restarts.
  - If the charge stops before the target (the owner pressed Stop), it is **not** restarted for that storm.
  - A battery already at the target is just noted.
  - A charge the owner started themselves is left alone.
- **Off by default.** The switch and settings are on the Storm watch card. `POST /api/storm` accepts {enabled, target, lead_hours, use_alerts, use_thunder, use_wind, gust_mph}. Status rides along in `/api/state` as `storm`.
- `tests/test_storm.py` covers the rules (alert filtering, grouping, lead time) and the controller (start, ready, no repeat, owner stop, already full, too far off).

## Weather & solar forecast (added 2026-10-10)
- `forecast.py`: **Open-Meteo** (free, no key) gives current, daily and hourly weather, plus sunlight (`shortwave_radiation`), for today and tomorrow.
  `past_days=31` gives the last month's sunlight for calibration. **Zippopotam** turns a US ZIP code into coordinates.
- **Solar estimate** = sunlight (kWh/m²) × a factor learned from the owner's own last 31 days of daily solar. The factor is the 80th percentile
  of daily `solar_kWh / sunlight_kWh_m2`, which skips days when a full battery made the inverter hold solar back. The first fit on the real system gave ≈ 2.95.
  Hourly: W = factor × W/m². With fewer than 5 days of history it falls back to `DEFAULT_FACTOR` 3.2.
- **Location:** the default is Downey, CA (ZIP 90241). It's changed from the Weather card's location button, saved server-side in
  `settings.json` (`location`), so it's shared by all viewers. Forecasts are cached for 30 minutes.
- Endpoints: `GET /api/forecast`, `POST /api/location {zip}`. The server needs outbound HTTPS to api.open-meteo.com and api.zippopotam.us.

## Status
- **Verified locally:** `tests/test_server.py` passes. That covers bad login, setup, live/energy/battery units, events, history,
  days and months, cross-site block, grid-charge start, and auto-stop at the target. Demo mode was screenshot-checked in light and dark, desktop and ~520 px.
- **Not yet verified:**
  - A real EG4 account. Check the field names and units, especially the monthColumn fields and the home-load formula, and whether the 6000XP reports battery modules.
  - Quick Charge on the owner's firmware.
  - The YunoHost install of *this* package. The water-heater package installed fine with the same scripts, so this is low risk.

## Next steps / ideas
- **Voltline extras not built yet:** a Forecast card (EG4 forecast endpoints, see pylxpweb `endpoints/forecasting.py`) and drag-to-arrange.
- Possibly merge with the water-heater app's Solar tab, or have that app read from this one, so EG4 is polled only once.

## Conventions
- **No grid export:** the owner's 6000XP model can't send power to the grid, so the power flow has no right-hand "Grid" node and no solar/battery-to-grid lines (owner's request, 2026-10-10). Grid appears only as a source: home, and the battery when grid charging.
- **The visual style follows the owner's Voltline app** (see Task 1). Black iOS-style theme by default, plus an iOS-light theme and Automatic.
  - Tokens: `--bg --card --card2 --ink --dim --dim2 --line`.
  - Series colors: `--solar --pv2 --home --bat --gridIn --gridOut --life`.
  - Accents: `--orange --ok --bad --warn`.
  - Earlier it matched the water-heater app's style; that is superseded for this app.
- **Layout is fluid:** `#cards{columns:390px 5}` and `clamp()` type. `drawFlow` picks a narrower viewBox when its card is under 520 px wide.
  - Flow animation: **glowing spheres** (radial-gradient circles, SMIL `animateMotion` along each band). Count = min(5, 2 + W/1500); time to cross = max(0.55, 1.35 − W/5000) s, so more power means more, faster spheres. Reduced-motion users get one static sphere per band. The owner asked for faster, spherical dots (2026-10-10).
- No build step, no CDN.
  - Solar, home and grid passed the colorblind check together. Battery teal and grid magenta are too close for some colorblind viewers, so keep battery on its own chart or directly labeled.
- One y-axis per chart, and a legend for 2+ series.
- Plain, friendly UI text. Anything that changes the inverter needs a confirmation dialog.
- A new `.py` file must be added to the `cp` list in `scripts/_common.sh`. Bump `version` in `manifest.toml` for each release.
- Commits end with a `Co-Authored-By:` line. This repo is private.
  - The public copy `moreno001-create/eg4-dashboard_ynh` (remote `public`) exists only so the YunoHost web admin can install it. It gets snapshot commits
    under the owner's noreply email, never the private history. To publish the current `main`, after pushing to the private repo:
    ```
    git fetch public
    git push public "$(GIT_AUTHOR_NAME='Anthony Moreno' GIT_AUTHOR_EMAIL='337951492+moreno001-create@users.noreply.github.com' GIT_COMMITTER_NAME='Anthony Moreno' GIT_COMMITTER_EMAIL='337951492+moreno001-create@users.noreply.github.com' git commit-tree HEAD^{tree} -p public/main -m "Update to $(git rev-parse --short HEAD): <summary>")":refs/heads/main
    ```
