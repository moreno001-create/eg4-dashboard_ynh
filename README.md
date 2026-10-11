# EG4 Solar Dashboard

Self-hosted web dashboard for an **EG4 solar inverter** (built for the **6000XP**), packaged as a [YunoHost](https://yunohost.org)
app so you can check your system from anywhere behind YunoHost's login. It reads the same data as the EG4 Monitor app, from
EG4's cloud. Unofficial; not affiliated with EG4. The cloud API may change without notice.

- **Styled after the owner's Voltline app**: one scrolling page of cards on a black iOS-style theme (or light), with a Sankey-style
  power flow (solar, battery, grid on the left; home, battery, grid on the right)
- **Cards**: MPPT solar inputs (PV1/PV2 power, voltage, current), battery, solar / consumption / grid sparklines for today,
  quick charge, energy independence, lifetime totals, solar today by string, history (10 min to 24 h), inverter (voltage,
  frequency, temperature, status), battery modules, last 30 days, last 12 months, alerts
- **Weather and solar forecast** for today and tomorrow (default Downey, CA; change it by ZIP code on the Weather card). The solar
  estimate is calibrated on your own last month of production. Data: Open-Meteo and Zippopotam, no account needed
- **Storm watch** (off until you switch it on): when a National Weather Service watch or warning, or forecast thunderstorms or strong
  gusts, is due within your chosen window, it charges the battery from the grid to your target, so it's full if the storm takes the grid down
- **Wi-Fi first**: with the inverter's Wi-Fi dongle set up (Settings → Wi-Fi dongle → Find on network), live readings come straight
  from it on your home network every few seconds, and EG4 Monitor takes over automatically whenever the dongle can't be reached. The
  dongle is found by its MAC address, so a new IP address from the router doesn't break it. Read-only
- **Arrange** moves or hides cards (saved per browser); **Refresh** reads the inverter now; **Settings** has the theme, EG4 login and demo mode
- **Grid charging (Quick Charge)** for 30 min, 1, 2 or 4 hours or until full, optionally stopping early at a battery level. Everything else is read-only.
- **Demo mode** with a simulated 6000XP, no account needed

## How it works
```
                    YunoHost (your home server)
 browser ──https──► nginx + YunoHost login ──► eg4solar service (Python, 127.0.0.1)
                                                 │  reads every minute; Quick Charge on request
                                                 ▼
                                         EG4 Monitor cloud  ◄── inverter's Wi-Fi dongle
```
- **The inverter's Wi-Fi dongle uploads to EG4 Monitor.** This server signs in to EG4 Monitor with your account and reads the inverter about once a
  minute. Battery and energy totals refresh every 5 minutes, and the event log every 15.
- **It keeps its own history.** Each reading goes into `history.jsonl` (90 days), which feeds the 24-hour charts. Daily and monthly
  energy come from EG4's own records. Finished months are cached in `months.json`.
- **Automatic stop for grid charging.** When you start grid charging with a stop level, the server checks the battery on each reading and
  stops the charge once it gets there.
- **Your EG4 password is saved on the server** in `settings.json` (mode 600, readable only by the app), so it can keep reading the inverter
  when nobody has the page open. *Settings → Sign out* removes it.

## Layout
| Path | What it is |
|---|---|
| `server.py`, `index.html`, `requirements.txt` | The web app (FastAPI server + single-page UI) |
| `eg4.py` | EG4 Monitor cloud client, unit conversion, demo inverter |
| `manifest.toml`, `scripts/`, `conf/`, `doc/` | YunoHost package (packaging v2, helpers 2.1, YunoHost 12+) |
| `tests/` | Checks that run against a fake EG4 server, no account needed |
| `CLAUDE.md` | Handoff notes: design, decisions, status and next steps |

## Install on YunoHost
**From the web admin:** go to *Applications → Install*, scroll to **Install custom app**, and paste the URL of this app's public
`_ynh` repository (see `CLAUDE.md`). Accept the third-party warning, pick the domain, keep the path `/solar`, choose who may use it
(**not visitors**), and click **Install**.

**From a terminal on the server** (this repo is private: clone with a GitHub token as the password):
```
git clone https://github.com/moreno001-create/eg4-dashboard.git
sudo yunohost app install ./eg4-dashboard
```
Then open `https://<your domain>/solar` and sign in with your **EG4 Monitor** email and password.

Upgrade: `sudo yunohost app upgrade eg4solar -u https://github.com/moreno001-create/eg4-dashboard_ynh --force` (web install), or
`git pull` in the clone and `sudo yunohost app upgrade eg4solar -f ./ --force`. Logs: `sudo yunohost service log eg4solar`.

## Run locally (Windows/Mac/Linux)
```
pip install -r requirements.txt
python server.py        # opens http://127.0.0.1:8766
```
Settings and history go in `data/` next to `server.py`. Environment variables: `EG4_HOST`, `EG4_PORT`, `EG4_DATA_DIR`.
