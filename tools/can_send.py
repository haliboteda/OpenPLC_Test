"""Send a number to the board over CAN by hand and show what comes back.

The board runs TestCase/CAN's echo mode (CAN_ECHO_TEST_ENABLE): it transmits
nothing on its own, and answers every frame with the payload incremented as one
big-endian number, on the same identifier.

    python tools/can_send.py            interactive: type a number, press Enter
    python tools/can_send.py 5          send 5 once, print the reply, exit
    python tools/can_send.py 0xFF --len 2      send 00 FF, expect 01 00 back
    python tools/can_send.py 7 --id 123        use identifier 0x123

Numbers may be decimal (5), hex (0x1F), or a raw byte string (--raw 01AABB).
The analyser is the CANable 2.0 in SLCAN mode, found by USB VID 16D0 PID 117E.
"""

import argparse
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from common import Fail, Ok, Section, Warn, _import_serial  # noqa: E402

serial = _import_serial()
if serial is None:
    sys.exit(1)

CANABLE_VID_PID = (0x16D0, 0x117E)
SLCAN_BITRATE = {
    10000: "S0", 20000: "S1", 50000: "S2", 100000: "S3", 125000: "S4",
    250000: "S5", 500000: "S6", 750000: "S7", 1000000: "S8",
}


def find_analyser(explicit):
    if explicit:
        return explicit
    from serial.tools import list_ports
    for p in list_ports.comports():
        if (p.vid, p.pid) == CANABLE_VID_PID:
            return p.device
    return None


def to_bytes(text, length):
    """A decimal or hex number, big-endian, in `length` bytes (auto if None)."""
    value = int(text, 0)
    if value < 0:
        raise ValueError("negative values have no big-endian byte form here")
    if length is None:
        length = max(1, (value.bit_length() + 7) // 8)
    if length < 1 or length > 8:
        raise ValueError("length must be 1..8 bytes")
    return value.to_bytes(length, "big")


def show(prefix, payload):
    return "%s %s   (=%d)" % (prefix, " ".join("%02X" % b for b in payload),
                              int.from_bytes(payload, "big"))


class Analyser:
    def __init__(self, port, bitrate):
        self.sp = serial.Serial(port, 115200, timeout=0)
        self._cmd("C\r")
        self._cmd(SLCAN_BITRATE[bitrate] + "\r")
        version = self._cmd("V\r")
        if version:
            Ok("  firmware : %s" % version.decode("latin1").strip())
        self._cmd("O\r")
        self.sp.reset_input_buffer()
        self.line = b""

    def _cmd(self, text, wait=0.2):
        self.sp.write(text.encode())
        self.sp.flush()
        time.sleep(wait)
        got = self.sp.read(self.sp.in_waiting or 0)
        if b"\x07" in got:
            Warn("  %s rejected by the firmware" % text.strip())
        return got

    def send(self, can_id, payload):
        frame = "t%03X%d%s\r" % (can_id, len(payload),
                                 "".join("%02X" % b for b in payload))
        self.sp.write(frame.encode())
        self.sp.flush()

    def collect(self, seconds):
        """Every complete SLCAN line seen within the window."""
        out = []
        deadline = time.time() + seconds
        while time.time() < deadline:
            n = self.sp.in_waiting
            if n:
                for ch in self.sp.read(n):
                    b = bytes([ch])
                    if b == b"\r":
                        if self.line:
                            out.append(self.line.decode("latin1"))
                            self.line = b""
                    elif b == b"\x07":
                        out.append("<BELL: bus or command error>")
                    else:
                        self.line += b
            else:
                time.sleep(0.005)
        return out


def parse_reply(text):
    """'t1231AB' -> (0x123, b'\\xab'). None if it is not a standard data frame."""
    if len(text) < 5 or text[0] != "t":
        return None
    try:
        can_id = int(text[1:4], 16)
        n = int(text[4], 16)
        data = bytes.fromhex(text[5:5 + 2 * n])
    except ValueError:
        return None
    return can_id, data


def one_shot(an, can_id, payload, wait):
    print(show("  TX  id=0x%03X  data=" % can_id, payload))
    an.send(can_id, payload)
    lines = an.collect(wait)
    if not lines:
        Fail("  no reply within %.1f s" % wait)
        return False

    expect = (int.from_bytes(payload, "big") + 1) % (1 << (8 * len(payload)))
    seen_ok = False
    for text in lines:
        got = parse_reply(text)
        if got is None:
            print("  RX  %s" % text)
            continue
        rid, data = got
        print(show("  RX  id=0x%03X  data=" % rid, data))
        if rid == can_id and int.from_bytes(data, "big") == expect:
            seen_ok = True
    if seen_ok:
        Ok("  reply is the value plus one, as expected")
    else:
        Warn("  a frame came back but it is not value+1 on the same id")
    return seen_ok


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("value", nargs="?", default=None,
                    help="number to send; omit for an interactive prompt")
    ap.add_argument("--id", dest="can_id", default="0x123",
                    help="standard identifier, default 0x123")
    ap.add_argument("--len", dest="length", type=int, default=None,
                    help="payload length in bytes, 1..8; default is the "
                         "smallest that holds the value")
    ap.add_argument("--raw", default=None,
                    help="send these hex bytes verbatim, e.g. 01AABB")
    ap.add_argument("--bitrate", type=int, default=500000)
    ap.add_argument("--can-port", default=None)
    ap.add_argument("--wait", type=float, default=1.0,
                    help="seconds to wait for the reply")
    args = ap.parse_args()

    if args.bitrate not in SLCAN_BITRATE:
        Fail("bitrate %d has no SLCAN code" % args.bitrate)
        return 1
    can_id = int(args.can_id, 0)
    if not 0 <= can_id <= 0x7FF:
        Fail("identifier must be a standard 11-bit id, 0x000..0x7FF")
        return 1

    Section("Analyser")
    port = find_analyser(args.can_port)
    if not port:
        Fail("no CANable found (USB VID 16D0 PID 117E); pass --can-port")
        return 1
    Ok("  port     : %s" % port)
    an = Analyser(port, args.bitrate)
    Ok("  %d bit/s, channel open, id 0x%03X" % (args.bitrate, can_id))

    try:
        if args.raw is not None:
            Section("Send")
            return 0 if one_shot(an, can_id, bytes.fromhex(args.raw),
                                 args.wait) else 2
        if args.value is not None:
            Section("Send")
            return 0 if one_shot(an, can_id, to_bytes(args.value, args.length),
                                 args.wait) else 2

        Section("Interactive")
        print("  Type a number and press Enter. Blank line or Ctrl-C quits.")
        print("  Decimal (5), hex (0x1F). Prefix with 'id=' to change the")
        print("  identifier for the rest of the session, e.g. id=0x200")
        while True:
            try:
                text = input("  > ").strip()
            except (EOFError, KeyboardInterrupt):
                print("")
                break
            if not text:
                break
            if text.lower().startswith("id="):
                try:
                    can_id = int(text[3:], 0)
                    Ok("  identifier is now 0x%03X" % can_id)
                except ValueError:
                    Warn("  not a number")
                continue
            try:
                payload = to_bytes(text, args.length)
            except ValueError as exc:
                Warn("  %s" % exc)
                continue
            one_shot(an, can_id, payload, args.wait)
    finally:
        an._cmd("C\r")
        an.sp.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
