"""POWLAND 12000W split-phase hybrid (SUNGERY control board) over its Eybond "Wi-Fi Plug Pro" stick.

The stick doesn't listen for connections: it connects *out* to a server. A UDP message to the stick on port 58899,
`set>server=<this server's IP>:<port>;`, makes it also connect here over TCP, and the cloud app (SmartValue) keeps working.
Over that connection every message has an 8-byte Eybond header, big-endian:
    [transaction id:2][device code:2][length:2 = bytes after this field][device address:1][function:1] + payload
Function 1 is the heartbeat (payload: year-2000, month, day, hour, minute, second, interval:2); function 4 forwards a
raw Modbus RTU frame to the inverter (slave 1, function 3, CRC-16) and carries its reply back.

Quirks found on the owner's stick (2026-11-14):
- Sending the redirect again while it's connected makes it hang up and reconnect, so the redirect only goes out while
  it isn't connected. A heartbeat right after it connects keeps it from hanging up after a few seconds.
- Some addresses (30066-30104) time out when read one at a time but answer in a block read.

Writes: only the registers in WRITABLE (the operating modes, the two battery-level switch points
and the grid charge current), one at a time with
Modbus function 6, each checked against the inverter's echo and read back. The register map is in doc/powland/REGISTERS.md.
"""
import asyncio
import logging
import os
import socket
import struct
import time
from datetime import date, datetime, timezone

log = logging.getLogger("eg4.powland")

PORT = int(os.environ.get("POWLAND_PORT", "8899"))
REDIRECT_PORT = 58899
# At most 64 registers per request (longer ones get exception 0x0A), and some spans are refused outright (30060x50,
# 30066x39, 30090x20), so these are shapes the inverter was seen to accept. A refused block is split and retried.
LIVE_BLOCKS = [(30000, 60), (30060, 40), (30100, 40), (39620, 50)]
SETTING_BLOCKS = [(41000, 32), (41032, 32), (41098, 25)]   # 41000x64 timed out every time while in Grid first (2026-11-14)
MODE_BLOCK = [(41000, 8)]   # just the operating modes (41000 output, 41006 charging): what a grid charge needs
IDENT_BLOCKS = [(51000, 40), (51040, 32)]
MIN_SPLIT = 10
REPLY_SECS = 6
HEARTBEAT_SECS = 30
SETTLE_SECS = 1.5
REDIRECT_SECS = 10
NAME = "POWLAND 12000W"
PV_MAX = 12000   # for the MPPT card's bars: the inverter's rating (two 200 A chargers could take more; the panels limit it)


class PowlandError(Exception):
    pass


class PowlandTimeout(PowlandError):
    """The inverter didn't answer a request in time (the link itself is still up)."""


def crc16(data):
    c = 0xFFFF
    for x in data:
        c ^= x
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
    return struct.pack("<H", c)


def frame(tid, fc, payload, devcode=0, devaddr=1):
    return struct.pack(">HHHBB", tid, devcode, len(payload) + 2, devaddr, fc) + payload


def heartbeat_payload(now=None, interval=60):
    t = datetime.fromtimestamp(now or time.time(), timezone.utc)
    return bytes([t.year - 2000, t.month, t.day, t.hour, t.minute, t.second]) + struct.pack(">H", interval)


def read_pdu(start, count, slave=1):
    pdu = bytes([slave, 3]) + struct.pack(">HH", start, count)
    return pdu + crc16(pdu)


def parse_read_reply(body, start, count):
    """The Modbus reply inside a function-4 frame -> {register: value}, or None for an exception / wrong length."""
    if len(body) >= 5 + 2 * count and body[1] == 3 and body[2] == 2 * count and crc16(body[:3 + 2 * count]) == body[3 + 2 * count:5 + 2 * count]:
        return dict(zip(range(start, start + count), struct.unpack(f">{count}H", body[3:3 + 2 * count])))
    return None


def own_address(stick_ip):
    """This machine's address on the stick's network (where the stick should connect)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((stick_ip, REDIRECT_PORT))
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()


class Stick:
    """Listens for the stick, keeps it pointed here, and reads registers through it (one request at a time)."""

    def __init__(self, stick_ip, advertise="", port=None, redirect_port=None):
        self.stick_ip = stick_ip.strip()
        self.port, self.redirect_port = port or PORT, redirect_port or REDIRECT_PORT   # read at run time, so tests can change them
        self.advertise = advertise.strip()
        self.server = None
        self.reader = self.writer = None
        self.peer = None
        self.tid = 0
        self.waiting = {}            # transaction id -> future for its reply
        self.lock = asyncio.Lock()   # one Modbus request at a time
        self.tasks = []
        self.connected_at = 0
        self.last_redirect = 0
        self.error = None

    @property
    def connected(self):
        return self.writer is not None and not self.writer.is_closing()

    def address(self):
        return self.advertise or own_address(self.stick_ip)

    async def start(self):
        if self.server:
            return
        try:
            self.server = await asyncio.start_server(self._accept, "0.0.0.0", self.port)
        except OSError as e:
            raise PowlandError(f"Couldn't listen on port {self.port} for the Wi-Fi stick: {e.strerror or e}")
        self.tasks = [asyncio.create_task(self._redirect_loop()), asyncio.create_task(self._heartbeat_loop())]

    async def close(self):
        for t in self.tasks:
            t.cancel()
        self.tasks = []
        self._drop()
        if self.server:
            self.server.close()
            try:
                await asyncio.wait_for(self.server.wait_closed(), 2)
            except (asyncio.TimeoutError, Exception):
                pass
            self.server = None

    def _drop(self):
        if self.writer:
            self.writer.close()
        self.reader = self.writer = None
        for f in self.waiting.values():
            if not f.done():
                f.set_exception(PowlandError("The Wi-Fi stick hung up"))
        self.waiting = {}

    def redirect(self):
        """Ask the stick to connect here. Only while it isn't connected: a repeat makes it hang up."""
        me = self.address()
        if not me or not self.stick_ip:
            self.error = "Couldn't tell which address the Wi-Fi stick should connect to"
            return
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.sendto(f"set>server={me}:{self.port};".encode(), (self.stick_ip, self.redirect_port))
        except OSError as e:
            self.error = f"Couldn't reach the Wi-Fi stick at {self.stick_ip}: {e.strerror or e}"
        finally:
            s.close()
        self.last_redirect = time.time()

    async def _redirect_loop(self):
        while True:
            if not self.connected:
                self.redirect()
            await asyncio.sleep(REDIRECT_SECS)

    async def _heartbeat_loop(self):
        while True:
            await asyncio.sleep(HEARTBEAT_SECS)
            if self.connected:
                try:
                    await self._send(1, heartbeat_payload())
                except (OSError, ConnectionError):
                    self._drop()

    async def _accept(self, reader, writer):
        peer = writer.get_extra_info("peername")
        if self.stick_ip and peer and peer[0] != self.stick_ip:   # behind Docker's port publishing it can arrive from the gateway
            log.info("Connection on the stick's port from %s (the stick is %s); taking it as the stick", peer[0], self.stick_ip)
        self._drop()   # a reconnect replaces the old connection
        self.reader, self.writer, self.peer = reader, writer, peer
        self.connected_at, self.error = time.time(), None
        log.info("Wi-Fi stick connected from %s", peer)
        try:
            await self._send(1, heartbeat_payload())
            while True:
                head = await reader.readexactly(8)
                tid, _dev, length, _addr, fc = struct.unpack(">HHHBB", head)
                body = await reader.readexactly(length - 2) if length > 2 else b""
                f = self.waiting.pop(tid, None)
                if f and not f.done():
                    f.set_result((fc, body))
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            if self.writer is writer:
                log.info("Wi-Fi stick disconnected")
                self._drop()

    async def _send(self, fc, payload):
        self.tid = (self.tid + 1) & 0xFFFF or 1
        tid = self.tid
        self.writer.write(frame(tid, fc, payload))
        await self.writer.drain()
        return tid

    async def read(self, start, count):
        """Holding registers start..start+count-1, or None if the inverter refuses that block."""
        async with self.lock:
            if not self.connected:
                raise PowlandError(self.error or "The Wi-Fi stick hasn't connected to this server yet")
            settle = self.connected_at + SETTLE_SECS - time.time()
            if settle > 0:   # the stick hangs up on a request that follows its first heartbeat too closely
                await asyncio.sleep(settle)
            fut = asyncio.get_running_loop().create_future()
            tid = await self._send(4, read_pdu(start, count))
            self.waiting[tid] = fut
            try:
                _fc, body = await asyncio.wait_for(fut, REPLY_SECS)
            except asyncio.TimeoutError:
                self.waiting.pop(tid, None)
                raise PowlandTimeout(f"The inverter didn't answer (registers {start}-{start + count - 1})")
            return parse_read_reply(body, start, count)

    async def write(self, reg, value):
        """Set one setting register (function 6). Only registers and values in WRITABLE; the echo must match."""
        allowed = WRITABLE.get(reg)
        if allowed is None or value not in allowed:
            raise PowlandError(f"Register {reg} can't be set to {value} from here")
        async with self.lock:
            if not self.connected:
                raise PowlandError(self.error or "The Wi-Fi stick isn't connected")
            pdu = bytes([1, 6]) + struct.pack(">HH", reg, value)
            fut = asyncio.get_running_loop().create_future()
            tid = await self._send(4, pdu + crc16(pdu))
            self.waiting[tid] = fut
            try:
                _fc, body = await asyncio.wait_for(fut, REPLY_SECS)
            except asyncio.TimeoutError:
                self.waiting.pop(tid, None)
                raise PowlandError(f"The inverter didn't answer the change to register {reg}")
        if len(body) >= 3 and body[1] == 0x86:
            raise PowlandError(EXCEPTIONS.get(body[2], f"The inverter refused the change (code {body[2]})"))
        if len(body) < 8 or body[:6] != pdu or crc16(body[:6]) != body[6:8]:
            raise PowlandError(f"The inverter's answer to the change didn't match ({body.hex()})")

    async def read_blocks(self, blocks):
        regs = {}
        todo = list(blocks)
        while todo:
            start, count = todo.pop(0)
            try:
                got = await self.read(start, count)
            except PowlandTimeout:
                if count <= MIN_SPLIT or not self.connected:
                    raise
                got = None   # no answer to a long block (seen in Grid first mode): smaller pieces, as for a refused one
            if got:
                regs.update(got)
            elif count > MIN_SPLIT:   # refused: try it in two halves (some spans hold an address the inverter won't read in a block)
                half = count // 2
                todo[:0] = [(start, half), (start + half, count - half)]
        return regs

    async def wait_connected(self, secs):
        end = time.time() + secs
        while not self.connected and time.time() < end:
            await asyncio.sleep(0.2)
        return self.connected


# --------------------------------------------------------------------------- decoding
def s16(v):
    return v - 0x10000 if v >= 0x8000 else v


def _v(r, a, div=10, nd=1):
    return round(r[a] / div, nd) if a in r else None


def _status(pv, charge, discharge, grid):
    parts = []
    if pv > 20:
        parts.append("solar")
    if grid > 20:
        parts.append("from grid")
    if charge > 20:
        parts.append("charging")
    elif discharge > 20:
        parts.append("on battery")
    return ", ".join(parts).capitalize() if parts else "Standby"


def norm_live(r, now=None):
    """Live registers -> the dict local.norm_live / eg4.norm_runtime make, plus the split-phase legs."""
    g = lambda a: r.get(a, 0)
    strings = [dict(n=1, w=g(30013), v=_v(r, 30011) or 0, a=_v(r, 30012)), dict(n=2, w=g(30016), v=_v(r, 30014) or 0, a=_v(r, 30015))]
    pv = sum(s["w"] for s in strings)
    v_bat = round(g(30000) / 10, 1)
    amps = s16(g(30001)) / 10                 # positive while discharging, negative while charging
    battery = -round(v_bat * amps)            # the dashboard's sign: positive = charging
    charge, discharge = max(0, battery), max(0, -battery)
    load = g(30046)
    # grid power per leg (positive = from the grid; negative would mean sending power out). Found during a grid charge
    # on 2026-11-14: 30065/30066 read 1793/1699 W while 30059/30062 read 14.9/14.1 A at ~122 V.
    grid_w = [s16(g(30065)), s16(g(30066))]
    grid = sum(grid_w)
    grid_chg_a = round(g(30004) / 10, 1)        # grid charging current into the battery (48.0 A = the set limit)
    leg = lambda v, a, w, va, pct: dict(v=_v(r, v), a=_v(r, a), w=r.get(w), va=r.get(va), pct=r.get(pct))
    temps = [_v(r, a) for a in (30073, 30074, 30075, 30076)]
    return dict(
        t=round(now or time.time()), pv=pv, pv_strings=strings,
        soc=g(30002), v_bat=v_bat, a_bat=round(amps, 1), charge=charge, discharge=discharge, battery=battery,
        grid=grid, inv=load, load=load, rectifier=round(grid_chg_a * v_bat), grid_chg_a=grid_chg_a,
        v_ac=_v(r, 30058), hz=_v(r, 30060, 100, 2),                   # grid ("line") side
        v_eps=_v(r, 30024), hz_eps=_v(r, 30026, 100, 2),              # the inverter's output, which feeds the house
        bus1=_v(r, 30071), bus2=_v(r, 30072),
        t_inner=temps[0], t_rad1=temps[2], t_rad2=temps[3], t_extra=temps[1], t_bat=None,
        cell_max=None, cell_min=None,
        va=r.get(30047), load_pct=r.get(30048),
        legs=[leg(30024, 30025, 30028, 30029, 30030), leg(30032, 30033, 30035, 30036, 30037)],
        grid_legs=[dict(v=_v(r, 30061), a=_v(r, 30059), w=grid_w[0]), dict(v=_v(r, 30063), a=_v(r, 30062), w=grid_w[1])],
        status=_status(pv, charge, discharge, grid), lost=False, fw="", rating="12000 W", device_time="",
        source="local", model=NAME)


def text(r, start, n):
    """ASCII text, one character per register (the identity block holds the brand, serial and build date this way)."""
    return "".join(chr(r[a]) for a in range(start, start + n) if 32 <= r.get(a, 0) < 127).strip()


def ident(r):
    return dict(brand=text(r, 51002, 12), serial=text(r, 51014, 19), built=text(r, 51052, 20), machine=r.get(51000), hw=[r.get(51044), r.get(51045)])


# --------------------------------------------------------------------------- energy (the inverter's own kWh counters aren't mapped yet)
ENERGY_KEYS = ("solar", "used", "imported", "exported", "charged", "discharged")
MAX_GAP = 120   # a longer gap between readings isn't filled in


def new_meter():
    return dict(day="", last=0, today={k: 0.0 for k in ENERGY_KEYS}, total={k: 0.0 for k in ENERGY_KEYS}, days={})


def meter_add(m, live, now=None):
    """Adds the energy since the previous reading (kWh) and rolls the day over at midnight; returns m."""
    now = now or live["t"]
    today = date.fromtimestamp(now).isoformat()
    if m["day"] != today:
        if m["day"]:
            m["days"][m["day"]] = dict(m["today"])
        m["day"], m["today"] = today, {k: 0.0 for k in ENERGY_KEYS}
        m["days"] = dict(sorted(m["days"].items())[-400:])
    dt = now - m["last"] if m["last"] else 0
    m["last"] = now
    if 0 < dt <= MAX_GAP:
        w = dict(solar=live["pv"], used=live["load"], imported=max(0, live["grid"]), exported=max(0, -live["grid"]),
                 charged=live["charge"], discharged=live["discharge"])
        for k, watts in w.items():
            kwh = watts * dt / 3.6e6
            m["today"][k] += kwh
            m["total"][k] += kwh
    return m


def meter_energy(m):
    r = lambda d: {k: round(d.get(k, 0), 2) for k in ENERGY_KEYS}
    return dict(today=r(m["today"]), total=r(m["total"]))


def meter_days(m):
    """Finished days and today, in eg4.norm_month's row shape."""
    rows = [dict(date=d, **{k: round(v.get(k, 0), 1) for k in ENERGY_KEYS}) for d, v in m["days"].items()]
    if m["day"]:
        rows.append(dict(date=m["day"], **{k: round(m["today"][k], 1) for k in ENERGY_KEYS}))
    return rows


# --------------------------------------------------------------------------- operating modes (manual section 5, pages 26-28)
EXCEPTIONS = {1: "The inverter doesn't allow changing this setting", 2: "The inverter doesn't have that setting",
              3: "The inverter refused that value: it's outside the range it allows",
              4: "The inverter couldn't make the change right now"}

# Numbering follows the SRNE layout this firmware's labels come from (SmartValue shows "Batt" for 41000 = 2 and "SNU" for
# 41006 = 2). Hybrid output mode is left out on purpose: it's the mode that can send power to the grid.
OUTPUT_MODES = [
    (0, "Solar first (PV)",
     "Solar and the battery power the house; the grid takes over when solar can't keep up or the battery falls to the "
     "switch-to-grid level. When solar is short, the grid is used before the battery, so the battery is mostly kept "
     "as backup. Order: solar → battery → grid while solar is enough; solar → grid → battery when it isn't."),
    (1, "Grid first (Mains)",
     "The house runs on the grid and the inverter only takes over in a power cut, like a UPS. Solar still charges the "
     "battery. Good where the grid is unreliable; uses the most grid power. Order: grid → solar → battery. This is the "
     "factory setting."),
    (2, "Battery first (Battery)",
     "Solar and the battery power the house. When the battery falls to the switch-to-grid level the house moves to the "
     "grid, and it comes back once the battery is charged to the back-to-battery level. Uses your own power the most. "
     "Order: solar → battery → grid."),
]
CHARGE_MODES = [
    (0, "Solar first (PV priority)",
     "Solar charges the battery; the grid charges it only when solar isn't working (at night, for example). Keeps the "
     "battery up while using little grid power."),
    (1, "Grid first (Mains priority)",
     "The grid charges the battery first; solar only charges when the grid is off."),
    (2, "Solar and grid together (SNU)",
     "Solar charges first and the grid adds what solar can't, stopping when solar is enough again. The fastest way to "
     "charge. The grid can only help while the house is on the grid; on battery, only solar charges."),
    (3, "Solar only (PV only)",
     "Only solar charges the battery; the grid never does. Uses no grid power for charging, so after a dark spell the "
     "battery may stay low (and the house on the grid) until the sun brings it back."),
]
HYBRID_NOTE = (" Hybrid mode isn't offered here on purpose: it's the mode that can send power out to the grid (its "
               "'Constant Power On Grid' and 'Hybrid priority' options export; 'CurrAntiRev (CT)' prevents it but needs the "
               "optional current sensors). Change it only in SmartValue, and only if you mean to.")
MODES = [   # (register, name, label, options, help, risk)
    (41000, "OutPriority", "Where the house gets its power", OUTPUT_MODES,
     "Which source powers the house first: solar, the battery or the grid." + HYBRID_NOTE, "caution"),
    (41006, "ChgPriority", "How the battery is charged", CHARGE_MODES,
     "Which source charges the battery: solar, the grid, or both.", ""),
]
# Number settings that can be changed: register -> (lowest, highest, step) in the units shown on the page.
# Grid charge current: whole amps, 10-160 A (the manual's "Max AC charger current" range, default 120 A). The grid
# breaker and the battery's own charge limit matter more: the charger and the house share the grid inlet.
LIMITS = {41009: (10, 160, 1), 41118: (5, 100, 1), 41119: (5, 100, 1)}
SCALE = {41009: 10, 41118: 1, 41119: 1}
WRITABLE = {41000: {v for v, *_ in OUTPUT_MODES}, 41006: {v for v, *_ in CHARGE_MODES},
            **{reg: range(lo * SCALE[reg], hi * SCALE[reg] + 1, st * SCALE[reg]) for reg, (lo, hi, st) in LIMITS.items()}}


# --------------------------------------------------------------------------- settings
SETTINGS = [   # (register, name, label, scale, unit, help) -- confirmed against SmartValue's Default Group, 2026-11-14
    (41001, "OutVoltSet", "Output voltage", 10, "V", "The AC voltage the inverter makes on each leg."),
    (41002, "OutFreqSet", "Output frequency", 100, "Hz", "The AC frequency the inverter makes."),
    (41007, "MaxChgCurrSet", "Maximum charge current", 10, "A", "The most current the battery is charged with, from solar and grid together."),
    (41008, "ChgCurrByPvSet", "Solar charge current", 10, "A", "The most current solar may put into the battery."),
    (41009, "ChgCurrByLineSet", "Grid charge current", 10, "A", "The most current the grid may put into the battery. "
     "This caps grid charging (and Charge now). Battery amps, not grid amps: 48 A is about 2.7 kW, 100 A about 6 kW "
     "(25 A at 240 V), 160 A about 9.7 kW (40 A at 240 V). The house runs through the same grid inlet while charging, so "
     "keep the two together under 80% of the inlet breaker (40 A on a 50 A circuit), and within the battery's own charge limit."),
    (41101, "CvVolt", "Bulk (absorption) voltage", 10, "V", "The battery is charged up to this voltage, then held there."),
    (41102, "FloatVolt", "Float voltage", 10, "V", "The voltage the battery is kept at once it's full."),
    (41103, "", "Battery over-voltage (likely)", 10, "V", "Not shown in SmartValue; probably the voltage at which charging stops as a protection."),
    (41104, "CvChgTimeSet", "Bulk hold time", 1, "min", "How long the battery is held at the bulk voltage before dropping to float."),
    (41105, "CvChgBackVolt", "Recharge voltage", 10, "V", "Below this, a full battery starts a new bulk charge."),
    (41106, "Inv2LineVolt", "Switch to grid below", 10, "V", "When the battery falls to this voltage, the house moves onto the grid."),
    (41107, "LineBack2InvVolt", "Back to battery above", 10, "V", "Once the battery is back up to this voltage, the house moves off the grid again."),
    (41108, "BattLowVolt", "Low battery warning", 10, "V", "The inverter warns that the battery is low."),
    (41109, "BattDelayOffVolt", "Delayed shut-off voltage", 10, "V", "Below this for the delay time, the inverter stops using the battery."),
    (41110, "BattEodVolt", "Cut-off voltage", 10, "V", "The battery is never discharged below this."),
    (41111, "BattDelayOffTime", "Shut-off delay", 1, "s", "How long the battery may sit below the delayed shut-off voltage."),
    (41112, "BattEodVolt", "Restart voltage (likely)", 10, "V", "SmartValue shows this as a second BattEodVolt; probably the voltage the battery must recover to after a cut-off."),
    (41115, "BatSocLowAlarm", "Low battery alarm", 1, "%", "Charge level that raises a low-battery alarm (with a BMS connected)."),
    (41116, "BatStopSOC", "Stop discharging at", 1, "%", "The battery isn't discharged below this charge level."),
    (41117, "BattFullSOC", "Full at", 1, "%", "The charge level that counts as full."),
    (41118, "Inv2LineSOC", "Switch to grid at", 1, "%", "When the battery falls to this level, the house moves onto the grid (Battery first and Solar first modes)."),
    (41119, "LineBack2InvSOC", "Back to battery at", 1, "%", "Once the battery is charged back up to this level, the house moves off the grid again (Battery first mode). "
     "The inverter wants it well above 'Switch to grid at': with that at 10%, 30% was the lowest it accepted."),
]


GROUPS = [("modes", "Operating modes", "Where the house gets its power and how the battery is charged (manual section 5). "
                                       "A change takes effect right away; check SmartValue shows the same afterwards."),
          ("output", "Output", "What the inverter makes for the house."),
          ("current", "Charge current", "How hard the battery may be charged, and from where."),
          ("charge", "Charging voltages", "The voltages the charger works to. Wrong values here can overcharge the battery: check them against the battery maker's numbers."),
          ("battery", "Battery voltages", "When the house moves between battery and grid, and when the inverter stops using the battery, by voltage."),
          ("soc", "Battery levels", "The same decisions by charge level, used when the battery's BMS reports its level (CAN-BMS is on).")]


def group_of(reg):
    return "output" if reg < 41007 else "current" if reg < 41100 else "charge" if reg < 41106 else "battery" if reg < 41113 else "soc"


def norm_settings(r):
    out = []
    for reg, name, label, opts, help_, risk in MODES:
        if reg in r:
            known = {v: t for v, t, _ in opts}
            options = [[v, t] for v, t, _ in opts]
            if r[reg] not in known:   # hybrid (3) or anything else: shown, not offered
                options.append([r[reg], "Hybrid (can send power to the grid)" if r[reg] == 3 else f"Unknown ({r[reg]})"])
            out.append(dict(reg=reg, name=name, label=label, kind="select", value=r[reg], raw=r[reg], div=1, unit="", help=help_, group="modes",
                            step=1, options=options, option_help=[[v, t, d] for v, t, d in opts], risk=risk,
                            writable=r[reg] in known))
    for reg, name, label, div, unit, help_ in SETTINGS:
        if reg in r:
            v = r[reg] / div
            lo, hi, step = LIMITS.get(reg, (None, None, 1 / div))
            out.append(dict(reg=reg, name=name, label=label, kind="number", value=round(v, 2) if div > 1 else r[reg], raw=r[reg],
                            div=div, unit=unit, help=help_, group=group_of(reg), step=step, options=[], option_help=[],
                            risk="caution" if reg == 41009 else "", writable=reg in WRITABLE, lo=lo, hi=hi))
    return out


def encode(setting, value):
    """A value from the settings page -> the register value, or ValueError with a plain reason."""
    reg = setting["reg"]
    if reg not in WRITABLE or not setting.get("writable"):
        raise ValueError(f"{setting['label']} can't be changed here")
    try:
        raw = int(round(float(value) * setting.get("div", 1)))
    except (TypeError, ValueError):
        raise ValueError(f"{setting['label']}: {value!r} isn't a number")
    if raw not in WRITABLE[reg]:
        lim = LIMITS.get(reg)
        if lim:
            raise ValueError(f"{setting['label']}: {value} is outside {lim[0]}-{lim[1]} {setting['unit']}".rstrip()
                             + (" (whole numbers only)" if lim[0] <= float(value) <= lim[1] else ""))
        raise ValueError(f"{setting['label']}: {value} isn't one of the allowed values")
    return raw


def fmt(setting, raw):
    if setting["kind"] == "select":
        return next((t for v, t in setting["options"] if v == raw), f"Unknown ({raw})")
    div = setting.get("div", 1)
    v = raw / div if div > 1 else raw
    return f"{v:g} {setting['unit']}".strip()


# --------------------------------------------------------------------------- try it: python3 powland.py <stick ip>
if __name__ == "__main__":
    import json
    import sys

    async def main(ip):
        st = Stick(ip)
        await st.start()
        print("waiting for the stick at", ip, "to connect to", st.address(), "port", st.port)
        if not await st.wait_connected(60):
            print("it didn't connect:", st.error)
            return
        regs = await st.read_blocks(LIVE_BLOCKS)
        print(json.dumps(norm_live(regs), indent=1))
        print(json.dumps(norm_settings(await st.read_blocks(SETTING_BLOCKS)), indent=1))
        print(ident(await st.read_blocks(IDENT_BLOCKS)))
        await st.close()

    asyncio.run(main(sys.argv[1]))
