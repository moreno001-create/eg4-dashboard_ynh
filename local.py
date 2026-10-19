"""Direct connection to the inverter's Wi-Fi dongle on the home network (TCP port 8000), read-only.

The dongle speaks the LuxPower protocol: a small header wrapping Modbus RTU frames
(https://github.com/celsworth/lxp-bridge/wiki/TCP-Packet-Spec). The framing and the input-register map follow
pylxpweb (https://github.com/joyfulhouse/pylxpweb, MIT: transports/dongle.py, registers/inverter_input.py), which needs
Python 3.12+, so the few parts needed are written out here, like eg4.py does for the cloud.

Reads input registers (Modbus function 4) and holding registers (function 3). Writes (function 6) are stopping a grid charge
(stop_quick_charge, only the four registers EG4's own Stop changes on the owner's 6000XP, see QC_*), and the inverter
settings page (write_holding, only the registers inverter_settings.py names, each checked against the echo and read back).

The dongle is found by its MAC address, so a new address from the router doesn't break the link: the IP is looked up in
the ARP table, after a quick sweep of port 8000 on the local network if it isn't there. Every connection is checked
against the dongle's serial number (it's in each frame), so a reused address can't be mistaken for it.

The dongle accepts ONE local client at a time (its upload to the EG4 cloud carries on regardless), so this keeps one
connection open and closes it cleanly.
"""
import asyncio
import ipaddress
import os
import platform
import re
import socket
import struct
import subprocess
import time

PORT = 8000
HEADER = b"\xa1\x1a"
PROTOCOL = 1
TCP_TRANSLATED = 0xC2      # a Modbus frame for (or from) the inverter
READ_INPUT = 0x04
READ_HOLDING = 0x03
WRITE_SINGLE = 0x06

# Quick Charge on the owner's 6000XP (found 2026-10-10 by diffing every holding register before and after pressing Stop in the
# EG4 app): EG4 starts it by writing AC-charge window #3 = now..now+duration, the charge type = 1 (SOC/voltage) and a higher
# end voltage; Stop puts the window to 00:00-00:00, the type back to 0 and the end voltage back to 54.0 V. (pylxpweb's
# register 233 bit 0 / 234 are 0 on this model, charging or not.)
QC_WINDOW_START, QC_WINDOW_END = 72, 73   # AC-charge window #3 (low byte hour, high byte minute)
QC_CHARGE_TYPE = 120                      # bits 1-3: 0 time, 1 SOC/voltage, 2 both; other bits are other settings
QC_END_VOLT = 159                         # AC-charge end voltage, 0.1 V
QC_TYPE_MASK = 0x0E
QC_DEFAULT_NORMAL = dict(type=0, end_v=540)   # what EG4's Stop wrote back on 2026-10-10
WRITABLE = {QC_WINDOW_START, QC_WINDOW_END, QC_CHARGE_TYPE, QC_END_VOLT}
HOLDING_BLOCKS = [(68, 6), (120, 1), (159, 1)]
BLOCKS = [(0, 40), (40, 40), (80, 40), (120, 40), (160, 40), (200, 12)]   # input registers 0-211


class LocalError(Exception):
    pass


# --------------------------------------------------------------------------- framing
def crc16(data):
    """Modbus CRC-16."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def read_request(dongle_sn, inverter_sn, start, count, fn=READ_INPUT):
    """A packet asking the inverter for `count` input (or holding) registers from `start`."""
    return _packet(dongle_sn, bytes([0, fn]) + _sn(inverter_sn) + struct.pack("<HH", start, count))


def write_request(dongle_sn, inverter_sn, register, value, allowed=WRITABLE):
    """A packet setting one holding register (Modbus function 6). Only registers in `allowed` (by default the Quick
    Charge ones) are accepted."""
    if register not in allowed or not 0 <= value <= 0xFFFF:
        raise LocalError(f"refusing to write register {register}")
    return _packet(dongle_sn, bytes([0, WRITE_SINGLE]) + _sn(inverter_sn) + struct.pack("<HH", register, value))


def _packet(dongle_sn, data):
    data += struct.pack("<H", crc16(data))
    body = bytes([1, TCP_TRANSLATED]) + _sn(dongle_sn) + struct.pack("<H", len(data)) + data
    return HEADER + struct.pack("<HH", PROTOCOL, len(body)) + body


def _sn(s):
    b = s.encode("ascii") if isinstance(s, str) else bytes(s)
    if len(b) != 10:
        raise LocalError(f"serial numbers are 10 characters, got {s!r}")
    return b


def split_frames(buf):
    """Complete frames at the front of `buf`, and what's left over. Junk before a header is skipped."""
    frames = []
    while True:
        i = buf.find(HEADER)
        if i < 0:
            return frames, buf[-1:] if buf.endswith(HEADER[:1]) else b""
        buf = buf[i:]
        if len(buf) < 6:
            return frames, buf
        n = struct.unpack("<H", buf[4:6])[0]
        if len(buf) < 6 + n:
            return frames, buf
        frames.append(buf[:6 + n])
        buf = buf[6 + n:]


def parse_frame(f):
    """{dongle, tcp} for any frame; plus {inverter, fn, start, regs} for a register reply with a good checksum."""
    out = dict(tcp=f[7] if len(f) > 7 else None, dongle=f[8:18].decode("ascii", "replace") if len(f) >= 18 else "")
    if out["tcp"] != TCP_TRANSLATED or len(f) < 20 + 15:
        return out
    d = f[20:]
    addr, fn = d[0], d[1]
    if addr == 1 and fn == WRITE_SINGLE and len(d) >= 18 and struct.unpack("<H", d[16:18])[0] == crc16(d[:16]):
        reg, val = struct.unpack("<HH", d[12:16])   # a write is answered by echoing the register and value
        return dict(out, inverter=d[2:12].decode("ascii", "replace"), fn=fn, start=reg, value=val)
    if addr == 1 and fn & 0x80:                       # the inverter refused the request
        return dict(out, inverter=d[2:12].decode("ascii", "replace"), fn=fn, error=d[14] if len(d) > 14 else None,
                    start=struct.unpack("<H", d[12:14])[0] if len(d) >= 14 else None)
    if addr != 1 or fn not in (3, 4):   # 1 = from the inverter; 3/4 = holding/input register read
        return out
    start, ln = struct.unpack("<H", d[12:14])[0], d[14]
    if len(d) < 17 + ln or struct.unpack("<H", d[15 + ln:17 + ln])[0] != crc16(d[:15 + ln]):
        return out
    regs = list(struct.unpack(f"<{ln // 2}H", d[15:15 + ln - ln % 2]))
    return dict(out, inverter=d[2:12].decode("ascii", "replace"), fn=fn, start=start, regs=regs)


# --------------------------------------------------------------------------- finding the dongle by MAC
def norm_mac(mac):
    """'F0-9E-9E-C9-F8-68', 'f0:9e:9e:c9:f8:68' and macOS's 'f0:9e:9e:c9:f8:8' styles -> 'f0:9e:9e:c9:f8:68'."""
    parts = re.split(r"[:\-]", (mac or "").strip().lower())
    if len(parts) != 6:
        return ""
    try:
        return ":".join(f"{int(p, 16):02x}" for p in parts)
    except ValueError:
        return ""


def arp_table():
    """{mac: ip} from the system's ARP (neighbour) table: Linux, macOS or Windows."""
    table = {}
    try:   # Linux (YunoHost): no subprocess needed
        with open("/proc/net/arp") as f:
            for line in f.read().splitlines()[1:]:
                cols = line.split()
                if len(cols) >= 4 and norm_mac(cols[3]) not in ("", "00:00:00:00:00:00"):
                    table[norm_mac(cols[3])] = cols[0]
        return table
    except OSError:
        pass
    try:
        out = subprocess.run(["arp", "-a"] if platform.system() == "Windows" else ["arp", "-an"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return table
    for line in out.splitlines():
        ip = re.search(r"(\d+\.\d+\.\d+\.\d+)", line)
        mac = re.search(r"([0-9a-fA-F]{1,2}(?:[:\-][0-9a-fA-F]{1,2}){5})", line)
        if ip and mac and norm_mac(mac.group(1)):
            table[norm_mac(mac.group(1))] = ip.group(1)
    return table


def mac_for_ip(ip):
    return next((m for m, i in arp_table().items() if i == ip), "")


MIN_PREFIX = 20   # never sweep more than a /20 (4,094 addresses), whatever the network's size


def _prefix_linux(me):
    """The prefix length of the local network that holds `me`, from the kernel's routing table (/proc/net/route)."""
    best = None
    with open("/proc/net/route") as f:
        for line in f.read().splitlines()[1:]:
            cols = line.split()
            if len(cols) < 8:
                continue
            dest, mask = int(cols[1], 16), int(cols[7], 16)   # printed as host-order (little-endian) numbers
            if mask and (struct.unpack("<I", socket.inet_aton(me))[0] & mask) == dest:
                bits = bin(mask).count("1")
                best = max(best or 0, bits)
    return best


def _prefix_ifconfig(me):
    """The same from `ifconfig` (macOS/BSD): 'inet 192.168.4.56 netmask 0xfffffc00'."""
    out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=10).stdout
    m = re.search(rf"inet {re.escape(me)} netmask (0x[0-9a-fA-F]+)", out)
    return bin(int(m.group(1), 16)).count("1") if m else None


def configured_networks():
    """The home networks named in EG4_NETWORKS ("192.168.4.0/22", comma-separated), for when this runs where it can't see
    the home network itself (in a Docker container, its own address is on Docker's private network). Each is kept to a
    /20 at most, like the networks found by themselves."""
    nets = []
    for part in os.environ.get("EG4_NETWORKS", "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            n = ipaddress.ip_network(part, strict=False)
        except ValueError:
            continue
        if n.version == 4 and n.prefixlen < MIN_PREFIX:
            n = ipaddress.ip_network(f"{n.network_address}/{MIN_PREFIX}", strict=False)
        if n.version == 4 and n not in nets:
            nets.append(n)
    return nets


def local_networks():
    """The local network around this computer's own address, at its real size (a home network can be bigger than a /24:
    the owner's eero hands out 192.168.4.0-192.168.7.255, so the server at .6.237 must sweep the whole /22 to reach the
    dongle at .4.26). No packets are sent to find it. EG4_NETWORKS, when set, names them instead."""
    if configured_networks():
        return configured_networks()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))   # documentation address; UDP connect only picks the outgoing interface
            me = s.getsockname()[0]
    except OSError:
        return []
    prefix = None
    for find in (_prefix_linux, _prefix_ifconfig):
        try:
            prefix = find(me)
        except (OSError, ValueError, subprocess.SubprocessError, struct.error):
            prefix = None
        if prefix:
            break
    prefix = max(MIN_PREFIX, min(prefix or 24, 30))
    return [ipaddress.ip_network(f"{me}/{prefix}", strict=False)]


async def _port_open(ip, port, timeout):
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)
    except (OSError, asyncio.TimeoutError):
        return False
    w.close()
    try:
        await w.wait_closed()
    except OSError:
        pass
    return True


async def sweep(networks, port=None, timeout=0.8, limit=128):
    """Addresses on these networks with `port` open. Touching every address also fills the ARP table."""
    gate = asyncio.Semaphore(limit)

    async def one(ip):
        async with gate:
            return ip if await _port_open(ip, port or PORT, timeout) else None

    ips = [str(h) for n in networks for h in n.hosts()]
    return [ip for ip in await asyncio.gather(*(one(ip) for ip in ips)) if ip]


async def identify(ip, inverter_sn="", port=None, timeout=4):
    """The dongle serial a device at `ip` names in its frames ('' if it doesn't speak the dongle protocol)."""
    try:
        r, w = await asyncio.wait_for(asyncio.open_connection(ip, port or PORT), timeout)
    except (OSError, asyncio.TimeoutError):
        return ""
    try:
        if inverter_sn:   # a harmless read gets an answer at once (without one, the dongle's next heartbeat can take a while)
            w.write(read_request("0000000000", inverter_sn, 0, 1))
            await w.drain()
        buf, end = b"", time.monotonic() + timeout
        while time.monotonic() < end:
            chunk = await asyncio.wait_for(r.read(4096), end - time.monotonic())
            if not chunk:
                break
            frames, buf = split_frames(buf + chunk)
            for f in frames:
                if parse_frame(f)["dongle"]:
                    return parse_frame(f)["dongle"]
    except (OSError, asyncio.TimeoutError):
        pass
    finally:
        w.close()
    return ""


async def find_dongle(mac, hint_ip="", port=None, dongle_sn="", inverter_sn=""):
    """The dongle's current IP, found by MAC: the ARP table first, then (if missing) after a sweep. '' if not found.

    If the MAC still isn't in the ARP table (the table came back empty to Homebrew's Python on a Mac), the swept devices
    are asked who they are, and the one naming the expected dongle serial is taken."""
    mac = norm_mac(mac)
    if not mac and not dongle_sn:
        return ""
    ip = arp_table().get(mac) if mac else None
    if ip:
        return ip
    nets = local_networks()
    if hint_ip:
        try:
            hint = ipaddress.ip_network(f"{hint_ip}/24", strict=False)
            nets = nets + [hint] if hint not in nets else nets
        except ValueError:
            pass
    open_ips = await sweep(nets, port or PORT)
    ip = arp_table().get(mac) if mac else None
    if ip or not dongle_sn:
        return ip or ""
    for ip in open_ips:
        if await identify(ip, inverter_sn, port) == dongle_sn:
            return ip
    return ""


# --------------------------------------------------------------------------- the connection
class Dongle:
    """One open connection to the dongle; reads input registers. Reconnects (finding it by MAC again) when it drops."""

    def __init__(self, mac, inverter_sn, dongle_sn="", ip="", port=None, timeout=8):
        self.mac, self.inverter_sn, self.dongle_sn = norm_mac(mac), inverter_sn, dongle_sn
        self.ip, self.port, self.timeout = ip, port or PORT, timeout
        self.reader = self.writer = None
        self.buf = b""
        self.holding = {}     # the few holding registers read with each reading (HOLDING_BLOCKS)
        self.lock = asyncio.Lock()

    @property
    def connected(self):
        return self.writer is not None and not self.writer.is_closing()

    async def connect(self):
        """Opens the link: the last address if it still belongs to this MAC, else the address found by MAC now."""
        await self.close()
        candidates = []
        if self.ip and (not self.mac or mac_for_ip(self.ip) in ("", self.mac)):
            candidates.append(self.ip)
        if self.mac or self.dongle_sn:
            found = await find_dongle(self.mac, self.ip, self.port, self.dongle_sn, self.inverter_sn)
            if found and found not in candidates:
                candidates.append(found)
        if not candidates:
            raise LocalError(f"the dongle ({self.mac or self.dongle_sn or 'not set up'}) isn't on this network right now")
        err = None
        for ip in candidates:
            try:
                self.reader, self.writer = await asyncio.wait_for(asyncio.open_connection(ip, self.port), self.timeout)
            except (OSError, asyncio.TimeoutError) as e:
                err = e
                continue
            self.ip, self.buf = ip, b""   # the serial check in _read() confirms it's really the dongle
            return ip
        raise LocalError(f"couldn't connect to the dongle at {', '.join(candidates)}: {err or 'no answer'}")

    async def close(self):
        w, self.reader, self.writer = self.writer, None, None
        if w:
            w.close()
            try:
                await asyncio.wait_for(w.wait_closed(), 3)
            except (OSError, asyncio.TimeoutError):
                pass

    async def _read(self, start, count, fn=READ_INPUT):
        self.writer.write(read_request(self.dongle_sn or "0000000000", self.inverter_sn, start, count, fn))
        await self.writer.drain()
        return await self._answer(fn, start, count)

    async def _write(self, register, value, allowed=WRITABLE):
        self.writer.write(write_request(self.dongle_sn or "0000000000", self.inverter_sn, register, value, allowed))
        await self.writer.drain()
        got = await self._answer(WRITE_SINGLE, register, None)
        if got != value:
            raise LocalError(f"the inverter answered {got} after writing {value} to register {register}")

    async def _answer(self, fn, start, count):
        """The reply to a request just sent: registers for a read, the echoed value for a write."""
        end = time.monotonic() + self.timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                raise LocalError(f"no answer from the dongle for register {start}" + (f"-{start + count - 1}" if count else ""))
            chunk = await asyncio.wait_for(self.reader.read(4096), left)
            if not chunk:
                raise LocalError("the dongle closed the connection (is another local app connected to it?)")
            frames, self.buf = split_frames(self.buf + chunk)
            for f in frames:
                p = parse_frame(f)
                if self.dongle_sn and p["dongle"] and p["dongle"] != self.dongle_sn:
                    raise LocalError(f"that address is a different dongle ({p['dongle']}, expected {self.dongle_sn})")
                if not self.dongle_sn and p["dongle"]:
                    self.dongle_sn = p["dongle"]   # learned from the first frame
                # the dongle also passes along replies to the cloud's own reads, and a late reply to an earlier request can
                # arrive on a new connection: take only one that matches what we asked for, register count included
                if p.get("inverter") != self.inverter_sn or p.get("start") != start:
                    continue
                if p.get("fn") == fn | 0x80:
                    raise LocalError(f"the inverter refused the request for register {start} (code {p.get('error')})")
                if fn == WRITE_SINGLE and p.get("fn") == WRITE_SINGLE:
                    return p["value"]
                if p.get("fn") == fn and len(p.get("regs", ())) == count:
                    return p["regs"]

    async def read_all(self):
        """Input registers 0-211 as {register: value}. Connects (or reconnects once) as needed."""
        async with self.lock:
            for attempt in (1, 2):
                try:
                    if not self.connected:
                        await self.connect()
                    regs = {}
                    for start, count in BLOCKS:
                        for i, v in enumerate(await self._read(start, count)):
                            regs[start + i] = v
                    self.holding = {}
                    for start, count in HOLDING_BLOCKS:
                        for i, v in enumerate(await self._read(start, count, READ_HOLDING)):
                            self.holding[start + i] = v
                    return regs
                except (OSError, asyncio.TimeoutError, LocalError, asyncio.IncompleteReadError) as e:
                    await self.close()
                    if attempt == 2 or (isinstance(e, LocalError) and "different dongle" in str(e)):
                        raise LocalError(str(e) or type(e).__name__) from e


    async def read_holding(self, blocks):
        """Holding registers as {register: value}, for [(start, count), ...]. Connects (or reconnects once) as needed."""
        async with self.lock:
            for attempt in (1, 2):
                try:
                    if not self.connected:
                        await self.connect()
                    regs = {}
                    for start, count in blocks:
                        for i, v in enumerate(await self._read(start, count, READ_HOLDING)):
                            regs[start + i] = v
                    return regs
                except (OSError, asyncio.TimeoutError, LocalError, asyncio.IncompleteReadError) as e:
                    await self.close()
                    if attempt == 2 or (isinstance(e, LocalError) and "different dongle" in str(e)):
                        raise LocalError(str(e) or type(e).__name__) from e

    async def write_holding(self, want, allowed):
        """Set holding registers {register: value} (in that order), touching only those that differ and refusing any outside
        `allowed`. Each write must match the inverter's echo, then every register is read back. Returns {register: (before, after)}."""
        if any(r not in allowed or not 0 <= v <= 0xFFFF for r, v in want.items()):
            raise LocalError(f"refusing to write registers {sorted(r for r in want if r not in allowed)}")
        async with self.lock:
            if not self.connected:
                await self.connect()
            try:
                before = {r: (await self._read(r, 1, READ_HOLDING))[0] for r in want}
                for r, v in want.items():
                    if before[r] != v:
                        await self._write(r, v, allowed)
                after = {r: (await self._read(r, 1, READ_HOLDING))[0] for r in want}
            except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as e:
                await self.close()
                raise LocalError(f"the connection to the dongle broke while changing settings: {e or type(e).__name__}") from e
            if after != want:
                bad = {r: (want[r], after[r]) for r in want if after[r] != want[r]}
                raise LocalError(f"the inverter didn't keep every change (register: wanted, has): {bad}")
            return {r: (before[r], after[r]) for r in want}

    async def stop_quick_charge(self, normal=None):
        """Stop a grid charge (EG4 Quick Charge) the way EG4's own Stop does on the 6000XP: clear AC-charge window #3, put
        the charge type (bits 1-3 of register 120, the other bits kept) and the end voltage back to `normal`. Each write is
        checked against the inverter's echo, then everything is read back. Returns {register: (before, after)}."""
        normal = dict(QC_DEFAULT_NORMAL, **(normal or {}))
        if not 0 <= normal["type"] <= 2 or not 400 <= normal["end_v"] <= 600:
            raise LocalError(f"unexpected normal charge settings {normal}")
        async with self.lock:
            if not self.connected:
                await self.connect()
            try:
                before = dict(zip((QC_WINDOW_START, QC_WINDOW_END), await self._read(QC_WINDOW_START, 2, READ_HOLDING)))
                before[QC_CHARGE_TYPE] = (await self._read(QC_CHARGE_TYPE, 1, READ_HOLDING))[0]
                before[QC_END_VOLT] = (await self._read(QC_END_VOLT, 1, READ_HOLDING))[0]
                want = {QC_WINDOW_START: 0, QC_WINDOW_END: 0,
                        QC_CHARGE_TYPE: (before[QC_CHARGE_TYPE] & ~QC_TYPE_MASK) | (normal["type"] << 1),
                        QC_END_VOLT: normal["end_v"]}
                for reg in (QC_WINDOW_START, QC_WINDOW_END, QC_CHARGE_TYPE, QC_END_VOLT):   # the window first: that ends the charge
                    if before[reg] != want[reg]:
                        await self._write(reg, want[reg])
                after = dict(zip((QC_WINDOW_START, QC_WINDOW_END), await self._read(QC_WINDOW_START, 2, READ_HOLDING)))
                after[QC_CHARGE_TYPE] = (await self._read(QC_CHARGE_TYPE, 1, READ_HOLDING))[0]
                after[QC_END_VOLT] = (await self._read(QC_END_VOLT, 1, READ_HOLDING))[0]
            except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as e:
                await self.close()
                raise LocalError(f"the connection to the dongle broke while stopping the charge: {e or type(e).__name__}") from e
            if after != want:
                raise LocalError(f"the settings didn't take: wanted {want}, the inverter has {after}")
            return {r: (before[r], after[r]) for r in want}


def quick_status(h, now=None):
    """Grid-charge status from the holding registers read with each reading: EG4's Quick Charge uses window #3.
    {active, remain_min, until} like eg4.norm_quick, or None if the registers weren't read."""
    if QC_WINDOW_START not in h:
        return None
    start, end = h[QC_WINDOW_START], h[QC_WINDOW_END]
    if not start and not end:
        return dict(active=False, remain_min=0, status="", until="")
    t = time.localtime(now or time.time())
    end_min = (end & 0xFF) * 60 + (end >> 8)
    left = (end_min - (t.tm_hour * 60 + t.tm_min)) % (24 * 60)
    return dict(active=True, remain_min=left, status="AC charge window", until=f"{end & 0xFF:02d}:{end >> 8:02d}")


def normal_charge_settings(h):
    """The charge type and end voltage while no Quick Charge is set (what a stop puts back), or None."""
    if QC_WINDOW_START not in h or h[QC_WINDOW_START] or h[QC_WINDOW_END] or QC_CHARGE_TYPE not in h or QC_END_VOLT not in h:
        return None
    return dict(type=(h[QC_CHARGE_TYPE] & QC_TYPE_MASK) >> 1, end_v=h[QC_END_VOLT])


# --------------------------------------------------------------------------- units (same shapes as eg4.norm_*)
def _u32(r, lo):
    return r.get(lo, 0) | (r.get(lo + 1, 0) << 16)


def _s16(v):
    return v - 0x10000 if v & 0x8000 else v


def _status(pv, charge, discharge, grid, rectifier):
    """A plain description from the power flows (the raw status code isn't documented)."""
    parts = []
    if pv > 30:
        parts.append("Solar")
    if rectifier > 50 and charge > 50:
        parts.append("charging from grid")
    elif charge > 50:
        parts.append("charging battery")
    if discharge > 50:
        parts.append("on battery")
    if grid > 50 and not (rectifier > 50 and charge > 50):
        parts.append("grid")
    text = ", ".join(parts) or "Idle"
    return text[0].upper() + text[1:]


def norm_live(r, now=None):
    """Input registers -> the dict eg4.norm_runtime makes, so the dashboard doesn't care where readings came from."""
    strings = [dict(n=i, w=r.get(6 + i, 0), v=round(r.get(i, 0) / 10, 1)) for i in (1, 2, 3)]
    strings = [s for s in strings if s["n"] <= 2 or s["w"] > 0]   # the 6000XP reports a stray voltage on a third input
    pv = sum(s["w"] for s in strings)
    charge, discharge = r.get(10, 0), r.get(11, 0)
    grid = r.get(27, 0) - r.get(26, 0)
    inv = max(r.get(16, 0), r.get(24, 0))
    load = _s16(r.get(170, 0)) or inv   # output power: what the home is using (6000XP loads sit on the inverter output)
    return dict(
        t=round(now or time.time()), pv=pv, pv_strings=strings,
        soc=r.get(5, 0) & 0xFF, v_bat=round(r.get(4, 0) / 10, 1), charge=charge, discharge=discharge,
        battery=charge - discharge, grid=grid, inv=inv, load=max(0, load), rectifier=r.get(17, 0),
        v_ac=round(r.get(12, 0) / 10, 1), hz=round(r.get(15, 0) / 100, 2), t_inner=_s16(r.get(64, 0)), t_bat=r.get(67, 0),
        v_eps=round(r.get(20, 0) / 10, 1), hz_eps=round(r.get(23, 0) / 100, 2),          # the inverter's own output (feeds the house)
        bus1=round(r.get(38, 0) / 10, 1), bus2=round(r.get(39, 0) / 10, 1),              # DC bus inside the inverter
        t_rad1=_s16(r.get(65, 0)), t_rad2=_s16(r.get(66, 0)),                            # heatsinks
        cell_max=round(r.get(101, 0) / 1000, 3) or None, cell_min=round(r.get(102, 0) / 1000, 3) or None,
        bms_chg_a=round(r.get(81, 0) / 10, 1), bms_dis_a=round(r.get(82, 0) / 10, 1),   # what the battery's BMS allows
        status=_status(pv, charge, discharge, grid, r.get(17, 0)), lost=False, fw="", rating="", device_time="",
        source="local")


def norm_energy(r):
    """Today's and lifetime energy (kWh), in eg4.norm_energy's shape."""
    k = lambda v: round(v / 10, 1)
    # home use (171/172) reads 0 on the owner's 6000XP: None, so the cloud's figure is kept instead of a false 0
    today = dict(solar=k(r.get(28, 0) + r.get(29, 0) + r.get(30, 0)), charged=k(r.get(33, 0)), discharged=k(r.get(34, 0)),
                 imported=k(r.get(37, 0)), exported=k(r.get(36, 0)), used=k(r[171]) if r.get(171) else None)
    total = dict(solar=k(_u32(r, 40) + _u32(r, 42) + _u32(r, 44)), charged=k(_u32(r, 50)), discharged=k(_u32(r, 52)),
                 imported=k(_u32(r, 58)), exported=k(_u32(r, 56)), used=k(_u32(r, 172)) if _u32(r, 172) else None)
    return dict(today=today, total=total)


def norm_bank(r):
    """Bank-wide battery values, in the shape of eg4.norm_battery's `bank`."""
    return dict(cell_max=round(r.get(101, 0) / 1000, 3), cell_min=round(r.get(102, 0) / 1000, 3),
                t_max=round(_s16(r.get(103, 0)) / 10, 1), t_min=round(_s16(r.get(104, 0)) / 10, 1), cycles=r.get(106, 0),
                v=round(r.get(4, 0) / 10, 1), a=None)   # register 98 (BMS current) read 39 A while charging at ~100 A: not used


def quick_remaining(r):
    """Seconds left on a timed grid charge (register 210), 0 if none."""
    return r.get(210, 0)


# --------------------------------------------------------------------------- try it: python3 local.py <mac> <inverter serial>
if __name__ == "__main__":
    import json
    import sys

    async def main(mac, inverter_sn):
        d = Dongle(mac, inverter_sn)
        t0 = time.monotonic()
        regs = await d.read_all()
        print(f"dongle {d.dongle_sn} at {d.ip} ({d.mac}), {len(regs)} registers in {time.monotonic() - t0:.1f} s")
        print(json.dumps(dict(live=norm_live(regs), energy=norm_energy(regs), bank=norm_bank(regs)), indent=1))
        await d.close()

    asyncio.run(main(*sys.argv[1:3]))
