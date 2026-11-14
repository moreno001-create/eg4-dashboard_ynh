"""Read-only probe of an Eybond Wi-Fi Plug Pro: redirect it to this machine, read who it is and a few registers.
Sends only: heartbeat (FC1), Modbus reads (FC3/FC4) and Voltronic Q-queries wrapped in Forward2Device (FC4). Writes nothing."""
import asyncio, socket, struct, sys, time
from datetime import datetime, timezone

STICK, ME, PORT = sys.argv[1], sys.argv[2], 8899
LOG = open(sys.argv[3], "w")
def log(*a):
    line = f"{time.strftime('%H:%M:%S')} " + " ".join(str(x) for x in a)
    print(line, flush=True); LOG.write(line + "\n"); LOG.flush()

def crc_modbus(b):
    c = 0xFFFF
    for x in b:
        c ^= x
        for _ in range(8): c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
    return struct.pack("<H", c)

def crc_xmodem(b):
    c = 0
    for x in b:
        c ^= x << 8
        for _ in range(8): c = ((c << 1) ^ 0x1021) & 0xFFFF if c & 0x8000 else (c << 1) & 0xFFFF
    hi, lo = c >> 8, c & 0xFF
    hi += hi in (0x28, 0x0D, 0x0A); lo += lo in (0x28, 0x0D, 0x0A)   # Voltronic byte stuffing
    return bytes([hi, lo])

def frame(tid, devcode, devaddr, fc, payload):
    return struct.pack(">HHHBB", tid, devcode, len(payload) + 2, devaddr, fc) + payload

got = asyncio.Event()
state = dict(devcode=None, tid=100)

async def read_frames(reader, replies):
    while True:
        h = await reader.readexactly(8)
        tid, devcode, wl, addr, fc = struct.unpack(">HHHBB", h)
        body = await reader.readexactly(wl + 6 - 8) if wl + 6 > 8 else b""
        log(f"RX tid={tid} devcode=0x{devcode:04X} addr={addr} fc={fc} len={len(body)} hex={body.hex()} ascii={body.decode('latin1')!r}")
        if devcode and state["devcode"] is None:
            state["devcode"] = devcode
        replies.put_nowait((tid, fc, body))

async def ask(writer, replies, fc, payload, devaddr=1, wait=6):
    state["tid"] += 1
    tid = state["tid"]
    f = frame(tid, state["devcode"] or 0, devaddr, fc, payload)
    log(f"TX tid={tid} fc={fc} hex={f.hex()}")
    writer.write(f); await writer.drain()
    end = time.time() + wait
    while time.time() < end:
        try:
            r = await asyncio.wait_for(replies.get(), end - time.time())
        except asyncio.TimeoutError:
            break
        if r[0] == tid:
            return r
    log("   (no reply)")

async def handle(reader, writer):
    log("CONNECTED from", writer.get_extra_info("peername"))
    got.set()
    replies = asyncio.Queue()
    rt = asyncio.create_task(read_frames(reader, replies))
    try:
        now = datetime.now(timezone.utc)
        await ask(writer, replies, 1, bytes([now.year - 2000, now.month, now.day, now.hour, now.minute, now.second]) + struct.pack(">H", 60))
        await asyncio.sleep(1)
        log("devcode after heartbeat:", hex(state["devcode"] or 0))
        # Modbus RTU reads (function 3 = holding, 4 = input), slave 1: common blocks on SRNE / Must / PowMr-style hybrids
        for fn, start, n in [(3, 0x0000, 16), (3, 0x000A, 16), (3, 0x0100, 32), (3, 0x0200, 32), (3, 0xE000, 16), (4, 0x0000, 32), (3, 0x1000, 32)]:
            pdu = bytes([1, fn]) + struct.pack(">HH", start, n)
            await ask(writer, replies, 4, pdu + crc_modbus(pdu))
        # Voltronic (Axpert/PI30) and PI17 status queries, ASCII
        for q in [b"QPI", b"QPIGS", b"QMOD", b"QID"]:
            await ask(writer, replies, 4, q + crc_xmodem(q) + b"\r")
        for q in [b"^P005PI", b"^P005GS", b"^P006MOD"]:
            await ask(writer, replies, 4, q + crc_xmodem(q) + b"\r")
    except Exception as e:
        log("error:", repr(e))
    finally:
        rt.cancel(); writer.close()
        log("closed connection")

async def main():
    srv = await asyncio.start_server(handle, "0.0.0.0", PORT)
    log(f"listening on {PORT}; redirecting {STICK} to {ME}:{PORT}")
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.bind(("", 0)); s.setblocking(False)
    msg = f"set>server={ME}:{PORT};".encode()
    end = time.time() + 75
    while time.time() < end and not got.is_set():
        for dst in (STICK, "192.168.7.255"):
            try: s.sendto(msg, (dst, 58899))
            except OSError as e: log("udp send", e)
        try:
            d, a = s.recvfrom(1024); log("UDP reply from", a, d)
        except (BlockingIOError, OSError):
            pass
        await asyncio.sleep(3)
    if not got.is_set():
        log("the stick never connected"); srv.close(); return
    await asyncio.sleep(70)   # time for all the queries
    srv.close()
    log("done")

asyncio.run(main())
