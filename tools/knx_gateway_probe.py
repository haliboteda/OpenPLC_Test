"""Can a script reach the KNX bus through the KNXnet/IP gateway?

    python tools/knx_gateway_probe.py [--gateway IP] [--ga 31/7/255]

Without --gateway, the first gateway that answers a KNXnet/IP search is used.

1. DESCRIPTION_REQUEST: name, individual address, services, tunnelling slots.
2. Opens a tunnel and writes 0 to --ga; xknx waits for the gateway's
   L_Data.con, so a clean return means the telegram reached the bus.
3. Opens a second tunnel while the first is up, to see whether two clients
   (ETS and this script) can be connected at the same time.

Ticket: $PROD/maps/knx-examples/issues/KEX-04-can-the-script-talk-through-the-ip-gateway.md
UDP tunnels. Run it from a PC on the gateway's subnet: from another subnet
neither gateway seen so far answered (2026-10-09).
"""

import argparse
import asyncio
import socket
import struct
import sys
import time

from xknx import XKNX
from xknx.io import ConnectionConfig, ConnectionType
from xknx.io.self_description import request_description
from xknx.telegram import GroupAddress, Telegram
from xknx.telegram.apci import GroupValueWrite
from xknx.dpt import DPTBinary


def tunnel(gw):
    return XKNX(connection_config=ConnectionConfig(
        connection_type=ConnectionType.TUNNELING, gateway_ip=gw))


def search(seconds=3):
    """KNXnet/IP SEARCH_REQUEST from every local IPv4 address; first answer wins.
    Per address, because a VPN or proxy interface can own the default route."""
    for loc in {a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(loc))
            s.bind((loc, 0))
            hpai = bytes([8, 1]) + socket.inet_aton(loc) + struct.pack(">H", s.getsockname()[1])
            s.sendto(bytes([6, 0x10, 2, 1, 0, 14]) + hpai, ("224.0.23.12", 3671))
            end = time.time() + seconds
            while time.time() < end:
                s.settimeout(max(0.1, end - time.time()))
                try:
                    data, addr = s.recvfrom(2048)
                except socket.timeout:
                    break
                if data[2:4] == bytes([2, 2]):      # SEARCH_RESPONSE
                    return addr[0]
        except OSError:
            pass
        finally:
            s.close()
    return ""


async def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--gateway", default="")
    ap.add_argument("--ga", default="31/7/255")
    args = ap.parse_args()

    if not args.gateway:
        args.gateway = search()
        if not args.gateway:
            print("no KNXnet/IP gateway answered the search")
            return 2
    print("== gateway %s" % args.gateway)
    print("== description (UDP)")
    try:
        d = await request_description(args.gateway, route_back=True)
    except Exception as e:
        print("  no answer: %r" % e)
        d = None
    for k in () if d is None else ("name", "individual_address", "supports_tunnelling",
              "supports_tunnelling_tcp", "supports_routing",
              "tunnelling_requires_secure", "core_version"):
        print("  %s = %s" % (k, getattr(d, k, "?")))
    slots = (getattr(d, "tunnelling_slots", {}) or {}) if d else {}
    for ia, s in slots.items():
        print("  slot %s usable=%s free=%s" % (ia, s.usable, s.free))

    print("== tunnel 1: write 0 to %s" % args.ga)
    a = tunnel(args.gateway)
    await a.start()
    print("  connected as %s" % a.current_address)
    await a.telegrams.put(Telegram(destination_address=GroupAddress(args.ga),
                                   payload=GroupValueWrite(DPTBinary(0))))
    await asyncio.sleep(2)
    print("  sent (no error from the gateway)")

    print("== tunnel 2 while tunnel 1 is up")
    b = tunnel(args.gateway)
    try:
        await asyncio.wait_for(b.start(), 15)
        print("  connected as %s" % b.current_address)
        await b.stop()
    except Exception as e:
        print("  refused: %r" % e)
    await a.stop()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
