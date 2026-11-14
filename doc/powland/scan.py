"""Read-only Modbus map scan through the Eybond stick: one register (or COUNT) at each address, function 3 by default.
Usage: scan.py ME LOG START STOP STEP [fns] [count]. Logs every address that answers with data. Resumes after reconnects."""
import asyncio, socket, struct, sys, time
from datetime import datetime, timezone
ME, LOGF = sys.argv[1], sys.argv[2]
START, STOP, STEP = (int(sys.argv[3], 0) if "-" not in sys.argv[3] else 0), int(sys.argv[4], 0), int(sys.argv[5], 0)
FNS = [int(x) for x in (sys.argv[6] if len(sys.argv) > 6 else "3").split(",")]
COUNT = int(sys.argv[7]) if len(sys.argv) > 7 else 1
RANGES = [tuple(int(x) for x in r.split("-")) for r in sys.argv[3].split(",")] if "-" in sys.argv[3] else [(START, STOP)]
JOBS = [(fn, a) for fn in FNS for lo, hi in RANGES for a in range(lo, hi, STEP)]
LOG = open(LOGF, "a")
def log(*a):
    line = f"{time.strftime('%H:%M:%S')} " + " ".join(str(x) for x in a); print(line, flush=True); LOG.write(line + "\n"); LOG.flush()
def crc(b):
    c = 0xFFFF
    for x in b:
        c ^= x
        for _ in range(8): c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
    return struct.pack("<H", c)
S = dict(i=0, tid=0, hits=0, done=None, connected=None)

async def handle(reader, writer):
    log("connected", writer.get_extra_info("peername"), "at job", S["i"])
    S["connected"].set(); S["live"] = True
    async def send(fc, payload):
        S["tid"] = (S["tid"] + 1) & 0xFFFF
        writer.write(struct.pack(">HHHBB", S["tid"], 0, len(payload) + 2, 1, fc) + payload); await writer.drain()
        return S["tid"]
    async def reply(tid, wait=4):
        end = time.time() + wait
        while True:
            h = await asyncio.wait_for(reader.readexactly(8), max(.1, end - time.time()))
            t, dc, wl, addr, fc = struct.unpack(">HHHBB", h)
            body = await asyncio.wait_for(reader.readexactly(wl - 2), 4) if wl > 2 else b""
            if t == tid:
                return body
    try:
        now = datetime.now(timezone.utc)
        await send(1, bytes([now.year - 2000, now.month, now.day, now.hour, now.minute, now.second]) + struct.pack(">H", 60))
        await asyncio.sleep(1)
        last_hb = time.time()
        while S["i"] < len(JOBS):
            if time.time() - last_hb > 30:   # keep the link alive
                now = datetime.now(timezone.utc)
                await send(1, bytes([now.year - 2000, now.month, now.day, now.hour, now.minute, now.second]) + struct.pack(">H", 60))
                last_hb = time.time()
            fn, a = JOBS[S["i"]]
            pdu = bytes([1, fn]) + struct.pack(">HH", a, COUNT)
            tid = await send(4, pdu + crc(pdu))
            try:
                body = await reply(tid)
            except asyncio.TimeoutError:
                log(f"fn{fn} {a} (0x{a:04X}) timeout"); S["i"] += 1; continue
            S["i"] += 1
            if len(body) >= 3 + 2 * COUNT and body[1] == fn and body[2] == 2 * COUNT:
                S["hits"] += 1
                log(f"HIT fn{fn} {a} (0x{a:04X}) = {list(struct.unpack(f'>{COUNT}H', body[3:3 + 2 * COUNT]))}")
            elif len(body) >= 3 and body[1] == fn | 0x80:
                pass
            else:
                log(f"fn{fn} {a} odd reply {body.hex()}")
        log(f"scan finished: {S['hits']} answered of {len(JOBS)}")
        S["done"].set()
    except (asyncio.IncompleteReadError, ConnectionError) as e:
        log("stick hung up at job", S["i"], repr(e))
    finally:
        S["live"] = False
        writer.close()

async def main():
    S["done"], S["connected"] = asyncio.Event(), asyncio.Event()
    srv = await asyncio.start_server(handle, "0.0.0.0", 8899)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    msg = f"set>server={ME}:8899;".encode()
    deadline = time.time() + 900
    while not S["done"].is_set() and time.time() < deadline:
        if not S.get("live"):
            s.sendto(msg, ("192.168.7.87", 58899))
        try:
            await asyncio.wait_for(S["done"].wait(), 5)
        except asyncio.TimeoutError:
            pass
    srv.close()
asyncio.run(main())
