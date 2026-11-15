# POWLAND 12000W (SUNGERY board) register map

Read with Modbus function 3, slave 1, through the Eybond Wi-Fi Plug Pro (see `live_log.py`). Addresses are decimal.
**Confirmed** = matched against SmartValue's Data Details screenshots taken at 01:29–01:30 on 2026-11-14, while
`live-2026-11-14.jsonl` was logging (samples 01:29:17 and 01:29:43). **Guess** = fits, but no screenshot to confirm it.

## Live (30000–30065)
| Reg | Scale | Meaning | App showed | Log read | |
|---|---|---|---|---|---|
| 30000 | 0.1 V | Battery voltage | 52.9 V | 529 | confirmed |
| 30001 | 0.1 A signed | Battery current: positive discharging, negative charging (−13.1 A at 10:45) | 11.1 A | 116 | confirmed |
| 30002 | 1 % | Battery SOC | 71 % | 71 | confirmed |
| 30003 | 1 W | Battery charge power (0 while discharging) | — | 697 by day | daylight |
| 30004 | 0.1 A | Grid charge current into the battery | — | 480 while grid charging (= the 48 A limit) | grid charge |
| 30005 | 0.1 A | Solar charge current | — | 131 by day | daylight |
| 30011 / 30012 / 30013 | 0.1 V / 0.1 A / 1 W | PV1 voltage, current, power | — | 1138 / 75 / 859 | daylight (V×A = W) |
| 30014 / 30015 / 30016 | 0.1 V / 0.1 A / 1 W | PV2 voltage, current, power | — | 1624 / 39 / 635 | daylight (V×A = W) |
| 30006 / 30007 | 0.1 V | Grid voltage, L1 / L2, second reading (~0.4 V above 30058) | — | 1247 / 1249 | guess |
| 30008 / 30009 | ? | Changes with load, but not 1:1; unknown | — | 810 / 368 | unknown |
| 30024 / 30032 | 0.1 V | Output voltage L1 / L2 | 120.1 / 120.1 V | 1201 / 1201 | confirmed |
| 30025 | 0.1 A | Output current L1 | 2.4 A | 24 | confirmed |
| 30026 | 0.01 Hz | Output frequency | 59.99 Hz | 5999 | confirmed |
| 30028 | 1 W | Output active power L1 | 192 W | 197 | confirmed |
| 30029 | 1 VA | Output apparent power L1 | 288 VA | 281–298 | confirmed |
| 30030 | 1 % | Load L1 | 4 % | 4 | confirmed |
| 30033 | 0.1 A | Output current L2 | 4.5 A | 45 | confirmed |
| 30034 | ? | Follows L2 load (38 → 16 when load dropped) | — | 38 | unknown |
| 30035 | 1 W | Output active power L2 | 410 W | 393–419 | confirmed |
| 30036 | 1 VA | Output apparent power L2 | 548 VA | 540–558 | confirmed |
| 30037 | 1 % | Load L2 | 9 % | 9 | confirmed |
| 30046 | 1 W | Total output power (= house load) | 604 W | 594–600 | confirmed |
| 30047 | 1 VA | Total output apparent power | 839 VA | 838 | confirmed |
| 30048 | 1 % | Total load | — | 4–5 | guess |
| 30049–30053 | signed | About −1,415 each while grid charging, 0 otherwise (per-leg power taken in to charge?) | — | | grid charge |
| 30058 / 30061 / 30063 | 0.1 V | Grid (line) voltage | 124.3 V | 1243 | confirmed |
| 30059 / 30062 | 0.1 A | Grid current L1 / L2 | 0.0 A | 149 / 141 while grid charging | grid charge |
| 30060 | 0.01 Hz | Grid frequency | 59.99 Hz | 5999 | confirmed |

## 30060–30139 (block reads only: single reads of 30066–30104 time out)
| Reg | Scale | Meaning | |
|---|---|---|---|
| 30071 / 30072 | 0.1 V | DC bus (positive / negative half; app BusVoltP 247.8 V) | likely |
| 30073–30076 | 0.1 °C | Four temperatures (31.7 / 28.2 / 45.0 / 29.0 °C at 10:45); which is Temper1/2/3 isn't settled | likely |
| 30087–30089 | 1 °C | Whole-degree copies of temperatures (30 / 46 / 30) | guess |
| 30109 | ? | 958 at night, 1983 by day | unknown |

| 30065 / 30066 | 1 W signed | Grid power L1 / L2 (positive = from the grid) | 0 W | 1793 / 1699 while grid charging | grid charge |
| 30068 / 30069 | 1 W | A second grid power reading per leg (1766 / 1696); maybe the "external sampling" (CT) figure | | | grid charge |
| 30109 / 30110 | flags | Change with the mode (30110: 80 → 2128 while grid charging) | | | unknown |

Grid registers were found on 2026-11-14 by logging before and during a 30-minute grid charge (`charge-before.jsonl`,
`charge-during.jsonl`). 30027 and 30034 also jumped (35 → 118, 18 → 118), so 30027 is not a temperature.

## Request limits
At most 64 registers per request (more: Modbus exception 0x0A). Some spans are refused outright (exception 02):
30060×50, 30066×39, 30090×20. Accepted: 30000×60, 30060×40, 30100×40, 39620×50, 41000×64, 41098×25, 51000×40, 51040×32.
`powland.read_blocks` splits a refused block in halves and retries.

## 39620–39669
| Reg | Scale | Meaning | |
|---|---|---|---|
| 39637 | 10 W | Battery power (59–60 while the app showed 587 W) | confirmed |
| 39647 / 39648 | signed | Small negative values (−25 to −47 / −7 to −14); maybe CT grid power | unknown |
| 39662–39666 | ? | 39663 and 39666 follow load; 39662 and 39665 barely move | unknown |

**Not found yet:** the energy counters (today's battery charge 0.9 kWh, discharge 3.1 kWh, used 3.6 kWh at 01:30). Until
they are, the dashboard counts energy itself from the live power readings (`powland.meter_add`, saved in `energy.json`).

## Settings (41000–41209), all confirmed against the app's Default Group
| Reg | Scale | Setting | Value |
|---|---|---|---|
| 41001 | 0.1 V | OutVoltSet | 120 V |
| 41002 | 0.01 Hz | OutFreqSet | 60 Hz |
| 41007 | 0.1 A | MaxChgCurrSet | 250.0 A |
| 41008 | 0.1 A | ChgCurrByPvSet | 80.0 A |
| 41009 | 0.1 A | ChgCurrByLineSet (manual: "Max AC charger current", 0–160 A, default 120 A; changeable on the settings page, 10–160 A) | 48.0 A |
| 41101 | 0.1 V | CvVolt | 58.4 V |
| 41102 | 0.1 V | FloatVolt | 58.4 V |
| 41103 | 0.1 V | (58.5 V, probably battery over-voltage) | guess |
| 41104 | min | CvChgTimeSet | 120 |
| 41105 | 0.1 V | CvChgBackVolt | 53.0 V |
| 41106 | 0.1 V | Inv2LineVolt (switch to grid below) | 47.2 V |
| 41107 | 0.1 V | LineBack2InvVolt (back to battery above) | 57.6 V |
| 41108 | 0.1 V | BattLowVolt | 47.2 V |
| 41109 | 0.1 V | BattDelayOffVolt | 45.2 V |
| 41110 | 0.1 V | BattEodVolt (cut-off) | 44.8 V |
| 41111 | s | BattDelayOffTime | 30 |
| 41112 | 0.1 V | BattEodVolt (second; probably restart voltage) | 51.2 V |
| 41115 | % | BatSocLowAlarm | 15 |
| 41116 | % | BatStopSOC | 5 |
| 41117 | % | BattFullSOC | 100 |
| 41118 | % | Inv2LineSOC | 10 |
| 41119 | % | LineBack2InvSOC (owner set 30 on 2026-11-14; 25 refused with exception 03, 30 is the lowest accepted with 41118 = 10) | 30 |
| 41120 | 0/1 | OnTimeLineChargeEn | Disable |

Selects:
| Reg | Setting | Values |
|---|---|---|
| 41000 | OutPriority (AC output mode) | 2 = Batt (seen). By the SRNE layout: 0 = PV, 1 = Mains, 3 = Hybrid (not confirmed) |
| 41006 | ChgPriority (charger priority) | 2 = SNU (seen). By the SRNE layout: 0 = PV priority, 1 = Mains priority, 3 = PV only (not confirmed) |

Writes use function 6 on the same address (echoed back). Line range "UPS", battery type "L16" and BMS "CAN-BMS" are
still unmapped.

## Identity (51000+)
Text is one ASCII character per register. 51000 = MachCode 45; 51002–51013 = "SUNGERY"; 51014–51032 = serial
ZL-2608201236-00182; 51044 / 51045 = hardware versions 405 / 308; 51052–51071 = build date "Sep 28 2026 18:05:14";
51124 = rated 12000 W.
