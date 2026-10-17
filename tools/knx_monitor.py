"""Watch the KNX TP bus through the KNXnet/IP gateway and send group telegrams.

    python tools/knx_monitor.py [--gateway IP] [--seconds N] [--send "w 31/7/255 1"]

Without --gateway, the first gateway that answers a KNXnet/IP search is used.
Watches the bus from the PC side, independent of the board (KEX-09 in
$PROD/maps/knx-examples/issues/).

Commands (same as the KNX_BusMonitor sketch), then Enter:
    w 1/0/1 1        GroupValue.Write of a 0..63 value
    b 1/0/1 0C 1A    GroupValue.Write of data octets, in hex
    r 1/0/1          GroupValue.Read
    q                quit
"""

import argparse
import asyncio
import sys
import threading
import time
from pathlib import Path

from xknx import XKNX
from xknx.dpt import DPTArray, DPTBinary
from xknx.io import ConnectionConfig, ConnectionType
from xknx.telegram import GroupAddress, Telegram
from xknx.telegram.apci import GroupValueRead, GroupValueResponse, GroupValueWrite

sys.path.insert(0, str(Path(__file__).resolve().parent))
from knx_gateway_probe import search  # noqa: E402

USAGE = "usage: w 1/0/1 1  |  b 1/0/1 0C 1A  |  r 1/0/1  |  q"


def show(t):
    p = t.payload
    kind = type(p).__name__.replace("GroupValue", "") if p is not None else "-"
    val = ""
    v = getattr(p, "value", None)
    if isinstance(v, DPTBinary):
        val = " %d" % v.value
    elif isinstance(v, DPTArray):
        val = " " + " ".join("%02X" % b for b in v.value)
    print("%s KNX %s -> %s %s%s" % (time.strftime("%H:%M:%S"), t.source_address,
                                    t.destination_address, kind, val), flush=True)


def parse(line):
    f = line.split()
    if not f or f[0] not in ("w", "b", "r") or len(f) < 2:
        return None
    ga = GroupAddress(f[1])
    if f[0] == "r" and len(f) == 2:
        return Telegram(destination_address=ga, payload=GroupValueRead())
    if f[0] == "w" and len(f) == 3 and 0 <= int(f[2]) <= 63:
        return Telegram(destination_address=ga, payload=GroupValueWrite(DPTBinary(int(f[2]))))
    if f[0] == "b" and 3 <= len(f) <= 16:
        data = tuple(int(x, 16) for x in f[2:])
        return Telegram(destination_address=ga, payload=GroupValueWrite(DPTArray(data)))
    return None


async def main(a):
    if not a.gateway:
        a.gateway = search()
        if not a.gateway:
            sys.exit("no KNXnet/IP gateway answered; pass --gateway")
    x = XKNX(connection_config=ConnectionConfig(
        connection_type=ConnectionType.TUNNELING, gateway_ip=a.gateway))
    x.telegram_queue.register_telegram_received_cb(show)
    await x.start()
    print("connected to %s as %s; listening, bus telegrams print here as they come" % (a.gateway, x.current_address), flush=True)
    print("type a command and press Enter -- " + USAGE, flush=True)

    lines = asyncio.Queue()
    loop = asyncio.get_running_loop()
    for s in a.send:
        lines.put_nowait(s)
    if a.seconds:
        loop.call_later(a.seconds, lines.put_nowait, "q")
    else:
        def reader():
            for s in sys.stdin:
                loop.call_soon_threadsafe(lines.put_nowait, s)
            loop.call_soon_threadsafe(lines.put_nowait, "q")
        threading.Thread(target=reader, daemon=True).start()

    while True:
        s = (await lines.get()).strip()
        if s == "q":
            break
        if not s:
            continue
        try:
            t = parse(s)
        except ValueError:
            t = None
        if t is None:
            print(USAGE, flush=True)
            continue
        await x.telegrams.put(t)
        print("%s sent  %s" % (time.strftime("%H:%M:%S"), s), flush=True)
    await x.stop()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--gateway", default="")
    ap.add_argument("--seconds", type=float, default=0, help="run this long without reading the keyboard")
    ap.add_argument("--send", action="append", default=[], help="a command to send at start")
    asyncio.run(main(ap.parse_args()))
