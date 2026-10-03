"""The PC on Renode's virtual switch, reached through udp_frame_bridge.cs.

Answers ARP and DHCP (Renode has no DHCP server), then sends IAPTool's
discovery datagram once a second and records the replies. Used by T3-11's
Ethernet_IP; how it was worked out: $PROD/maps/sim-coverage/SIM-02-findings.md.
"""

import socket
import struct
import time

MY_MAC = bytes.fromhex("020000000001")
BCAST_MAC = b"\xff" * 6
MY_IP, GUEST_IP = socket.inet_aton("10.77.0.1"), socket.inet_aton("10.77.0.50")
GUEST = "10.77.0.50"
MASK, BCAST_IP = socket.inet_aton("255.255.255.0"), socket.inet_aton("10.77.0.255")
DISCOVERY = b"openplc_server_where_r_y"
SRV_PORT, MY_PORT = 56865, 50000


def _csum(b):
    if len(b) % 2:
        b += b"\0"
    s = sum(struct.unpack("!%dH" % (len(b) // 2), b))
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return (~s) & 0xFFFF


def _ip_udp(src, dst, sport, dport, payload):
    udp = struct.pack("!HHHH", sport, dport, 8 + len(payload), 0) + payload
    c = _csum(src + dst + struct.pack("!BBH", 0, 17, len(udp)) + udp) or 0xFFFF
    udp = udp[:6] + struct.pack("!H", c) + udp[8:]
    hdr = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 0, 0, 64, 17, 0, src, dst)
    return hdr[:10] + struct.pack("!H", _csum(hdr)) + hdr[12:] + udp


def _eth(dst, ethertype, payload):
    f = dst + MY_MAC + struct.pack("!H", ethertype) + payload
    return f + b"\0" * max(0, 60 - len(f))


def _dhcp_reply(req, msgtype):
    bootp = (struct.pack("!BBBB", 2, 1, 6, 0) + req[4:8] + b"\0\0\x80\x00" + b"\0" * 4 + GUEST_IP + MY_IP
             + b"\0" * 4 + req[28:44] + b"\0" * 192)
    opts = (b"\x63\x82\x53\x63" + bytes([53, 1, msgtype, 54, 4]) + MY_IP + bytes([51, 4]) + struct.pack("!I", 3600)
            + bytes([1, 4]) + MASK + bytes([3, 4]) + MY_IP + bytes([28, 4]) + BCAST_IP + b"\xff")
    return _eth(BCAST_MAC, 0x0800, _ip_udp(MY_IP, b"\xff" * 4, 67, 68, bootp + opts))


def _dhcp_type(payload):
    opts, i = payload[240:], 0
    while i < len(opts) and opts[i] != 255:
        if opts[i] == 0:
            i += 1
            continue
        if opts[i] == 53:
            return opts[i + 2]
        i += 2 + opts[i + 1]
    return None


def serve(listen_port, bridge_port, log, stop):
    """Runs until stop is set; appends 'leased' and 'reply <payload>' to log."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", listen_port))
    sock.settimeout(0.2)
    leased, next_discovery = False, 0.0
    try:
        while not stop.is_set():
            if leased and time.time() >= next_discovery:
                next_discovery = time.time() + 1.0
                sock.sendto(_eth(BCAST_MAC, 0x0800, _ip_udp(MY_IP, BCAST_IP, MY_PORT, SRV_PORT, DISCOVERY)),
                            ("127.0.0.1", bridge_port))
            try:
                f, _ = sock.recvfrom(4096)
            except socket.timeout:
                continue
            src, ethertype = f[6:12], struct.unpack("!H", f[12:14])[0]
            if ethertype == 0x0806:
                if struct.unpack("!H", f[20:22])[0] == 1 and f[38:42] == MY_IP:
                    arp = struct.pack("!HHBBH", 1, 0x0800, 6, 4, 2) + MY_MAC + MY_IP + src + f[28:32]
                    sock.sendto(_eth(src, 0x0806, arp), ("127.0.0.1", bridge_port))
                continue
            if ethertype != 0x0800 or f[14 + 9] != 17:
                continue
            ip = f[14:]
            ihl = (ip[0] & 0xF) * 4
            _, dport, ulen = struct.unpack("!HHH", ip[ihl:ihl + 6])
            payload = ip[ihl + 8:ihl + ulen]
            if dport == 67:
                kind = _dhcp_type(payload)
                if kind in (1, 3):   # DISCOVER -> OFFER, REQUEST -> ACK
                    sock.sendto(_dhcp_reply(payload, 2 if kind == 1 else 5), ("127.0.0.1", bridge_port))
                    if kind == 3 and not leased:
                        leased, next_discovery = True, time.time() + 2.0
                        log.append("leased")
            elif dport == MY_PORT:
                log.append("reply " + payload.decode("ascii", "replace"))
    finally:
        sock.close()
