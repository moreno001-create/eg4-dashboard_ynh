"""Read-only logger: every INTERVAL seconds, reads the POWLAND's live register blocks through its Eybond stick and appends
one JSON line {t, regs} to LOG. Used to match register numbers with what the SmartValue app shows at the same moment.
Usage: live_log.py ME STICK LOG MINUTES [INTERVAL]"""
import asyncio, json, socket, struct, sys, time
from datetime import datetime, timezone
ME, STICK, LOGF, MINUTES = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])
INTERVAL = float(sys.argv[5]) if len(sys.argv) > 5 else 10
BLOCKS = [(30000, 60), (30060, 40), (30100, 40), (39620, 50), (41000, 64), (41098, 25)]   # one request each (falls back to single registers if refused)

def crc(b):
    c = 0xFFFF
    for x in b:
        c ^= x
        for _ in range(8): c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
    return struct.pack("<H", c)

S = dict(tid=0, live=False, end=time.time() + MINUTES * 60)

async def handle(reader, writer):
    S["live"] = True
    print("connected", writer.get_extra_info("peername"), flush=True)
    async def ask(fc, payload):
        S["tid"] = (S["tid"] + 1) & 0xFFFF
        tid = S["tid"]
        writer.write(struct.pack(">HHHBB", tid, 0, len(payload) + 2, 1, fc) + payload); await writer.drain()
        if fc == 1:
            return None
        end = time.time() + 5
        while True:
            h = await asyncio.wait_for(reader.readexactly(8), max(.1, end - time.time()))
            t, _, wl, _, _ = struct.unpack(">HHHBB", h)
            body = await asyncio.wait_for(reader.readexactly(wl - 2), 5) if wl > 2 else b""
            if t == tid:
                return body
    async def read(start, n):
        pdu = bytes([1, 3]) + struct.pack(">HH", start, n)
        b = await ask(4, pdu + crc(pdu))
        if len(b) >= 3 + 2 * n and b[1] == 3 and b[2] == 2 * n:
            return dict(zip(range(start, start + n), struct.unpack(f">{n}H", b[3:3 + 2 * n])))
        return None
    try:
        now = datetime.now(timezone.utc)
        await ask(1, bytes([now.year - 2000, now.month, now.day, now.hour, now.minute, now.second]) + struct.pack(">H", 60))
        await asyncio.sleep(1)
        multi = {}
        while time.time() < S["end"]:
            t0, regs = time.time(), {}
            for start, n in BLOCKS:
                got = await read(start, n) if multi.get(start, True) else None
                if got is None:          # this block refuses multi-register reads: one at a time
                    multi[start] = False
                    for a in range(start, start + n):
                        one = await read(a, 1)
                        if one:
                            regs.update(one)
                else:
                    regs.update(got)
            with open(LOGF, "a") as f:
                f.write(json.dumps(dict(t=round(t0), local=time.strftime("%H:%M:%S", time.localtime(t0)), regs=regs)) + "\n")
            print(time.strftime("%H:%M:%S"), len(regs), "registers", flush=True)
            await asyncio.sleep(max(0, INTERVAL - (time.time() - t0)))
    except (asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError) as e:
        print("stick hung up:", repr(e), flush=True)
    finally:
        S["live"] = False
        writer.close()

async def main():
    srv = await asyncio.start_server(handle, "0.0.0.0", 8899)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    while time.time() < S["end"]:
        if not S["live"]:
            s.sendto(f"set>server={ME}:8899;".encode(), (STICK, 58899))
        await asyncio.sleep(5)
    srv.close()

asyncio.run(main())
