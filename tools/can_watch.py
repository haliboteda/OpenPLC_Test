"""Watch both ends of the CAN link at once.

The board runs TestCase/CAN's soak mode (CAN_SOAK_TEST_ENABLE): normal mode,
one frame every 3 s, every received frame printed to the RS232 console. This
prints that console and the USB-CAN analyser side together on one timeline, so
a frame that leaves one end and never arrives at the other is visible directly.

    python tools/can_watch.py                  run until Ctrl-C
    python tools/can_watch.py --seconds 60     run for a while, then summarise
    python tools/can_watch.py --send-every 0   analyser listens only
    python tools/can_watch.py --listen-only    analyser does not acknowledge
    python tools/can_watch.py --bitrate 125000

The analyser is a CANable 2.0 running the SLCAN (Lawicel ASCII) firmware from
github.com/normaldotcom/canable2, which enumerates as a CDC serial port with
USB VID 16D0 PID 117E. That firmware answers only the V command; every other
command is silent on success and returns BELL on rejection.

By default the analyser acknowledges what the board sends. That matters: with
nobody acknowledging, the board's transmit error counter gains 8 per frame and
it goes bus-off after 32 of them.
"""

import argparse
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from common import (Fail, Ok, Section, Warn, cfg, decode_serial,  # noqa: E402
                    _import_serial)

serial = _import_serial()
if serial is None:
    sys.exit(1)

CANABLE_VID_PID = (0x16D0, 0x117E)

# SLCAN speed codes. The board's own table is in TestCase/CAN/can_test.c.
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


def open_board(ports):
    for name in ports:
        try:
            return serial.Serial(name, cfg.LOG_BAUD, timeout=0), name
        except Exception as exc:
            Warn("  %s: %s" % (name, exc))
    return None, None


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--seconds", type=float, default=0.0,
                    help="0 = until Ctrl-C")
    ap.add_argument("--can-port", default=None,
                    help="override the analyser port (else found by VID/PID)")
    ap.add_argument("--board-port", default=None,
                    help="override the board's log port")
    ap.add_argument("--no-board", action="store_true",
                    help="drive the analyser only; leave the board's console "
                         "free for a serial terminal such as sscom")
    ap.add_argument("--bitrate", type=int, default=500000)
    ap.add_argument("--send-every", type=float, default=3.0,
                    help="analyser transmit interval in seconds; 0 = never")
    ap.add_argument("--tx-id", default="666",
                    help="analyser transmit id, 3 hex digits")
    ap.add_argument("--listen-only", action="store_true",
                    help="open the analyser without acknowledging (SLCAN L)")
    args = ap.parse_args()

    if args.bitrate not in SLCAN_BITRATE:
        Fail("bitrate %d has no SLCAN code; pick one of %s"
             % (args.bitrate, ", ".join(str(k) for k in SLCAN_BITRATE)))
        return 1
    if args.listen_only and args.send_every > 0:
        Warn("listen-only cannot transmit; forcing --send-every 0")
        args.send_every = 0.0

    Section("Ports")
    can_name = find_analyser(args.can_port)
    if not can_name:
        Fail("no CANable found (USB VID 16D0 PID 117E) and no --can-port given")
        return 1
    Ok("  analyser : %s" % can_name)

    board = None
    if args.no_board:
        Ok("  board    : not opened; watch it in your own serial terminal")
    else:
        board_ports = [args.board_port] if args.board_port else list(cfg.LOG_PORTS)
        board, board_name = open_board(board_ports)
        if not board:
            Fail("no board log port could be opened from %s" % board_ports)
            Warn("  a serial terminal may be holding it; or pass --no-board")
            return 1
        Ok("  board    : %s at %d" % (board_name, cfg.LOG_BAUD))

    can = serial.Serial(can_name, 115200, timeout=0)

    def cmd(text, wait=0.2):
        can.write(text.encode())
        can.flush()
        time.sleep(wait)
        got = can.read(can.in_waiting or 0)
        if b"\x07" in got:
            Warn("  %-4s rejected by the firmware"
                 % text.strip().replace("\r", ""))
        return got

    Section("Analyser setup")
    cmd("C\r")
    cmd(SLCAN_BITRATE[args.bitrate] + "\r")
    version = cmd("V\r")
    if version:
        Ok("  firmware : %s" % decode_serial(version).strip())
    cmd("L\r" if args.listen_only else "O\r")
    Ok("  %d bit/s, channel open, %s"
       % (args.bitrate,
          "listening without acknowledging" if args.listen_only
          else "acknowledging what the board sends"))
    if args.send_every > 0:
        Ok("  transmitting id=0x%s every %.1f s" % (args.tx_id, args.send_every))

    can.reset_input_buffer()
    if board:
        board.reset_input_buffer()

    t0 = time.time()
    deadline = t0 + args.seconds if args.seconds > 0 else None
    can_line = b""
    board_line = b""
    can_rx = 0
    board_rx = 0
    board_tx = 0
    tx_n = 0
    next_tx = t0 if args.send_every > 0 else None

    Section("Running%s" % ("" if not deadline else " %.0fs" % args.seconds))
    print("   ANALYSER RX = a frame the board put on the wire and the analyser")
    print("                 read back. That is the direction to watch.")
    print("")

    def stamp():
        return "%7.2fs" % (time.time() - t0)

    try:
        while deadline is None or time.time() < deadline:
            n = can.in_waiting
            if n:
                for ch in can.read(n):
                    b = bytes([ch])
                    if b == b"\r":
                        if can_line:
                            can_rx += 1
                            print("%s  [ANALYSER RX] %s"
                                  % (stamp(), can_line.decode("latin1")))
                            can_line = b""
                    elif b == b"\x07":
                        print("%s  [ANALYSER] BELL" % stamp())
                    else:
                        can_line += b

            n = board.in_waiting if board else 0
            if n:
                for ch in board.read(n):
                    b = bytes([ch])
                    if b in (b"\r", b"\n"):
                        if board_line.strip():
                            txt = decode_serial(board_line).rstrip()
                            if "  RX  id=" in txt:
                                board_rx += 1
                            elif "  TX  id=" in txt:
                                board_tx += 1
                            print("%s  [BOARD] %s" % (stamp(), txt))
                        board_line = b""
                    else:
                        board_line += b

            if next_tx is not None and time.time() >= next_tx:
                next_tx += args.send_every
                payload = "%02X" % (tx_n & 0xFF)
                can.write(("t%s4BEEF00%s\r" % (args.tx_id, payload)).encode())
                can.flush()
                tx_n += 1
                print("%s  [ANALYSER TX] id=0x%s  data= BE EF 00 %s"
                      % (stamp(), args.tx_id, payload))

            time.sleep(0.003)
    except KeyboardInterrupt:
        print("")

    cmd("C\r")
    can.close()
    if board:
        board.close()

    Section("Summary")
    print("   analyser sent      : %d frame(s)" % tx_n)
    print("   analyser read      : %d frame(s)" % can_rx)
    if board is None:
        print("   board side         : not watched here; read your terminal."
              " Every frame the")
        print("                        analyser sent should appear there as "
              "an RX line.")
        return 0

    print("   board  -> analyser : analyser read %d of the %d the board sent"
          % (can_rx, board_tx))
    print("   analyser -> board  : board read %d of the %d the analyser sent"
          % (board_rx, tx_n))
    if board_tx and not can_rx:
        Fail("   the analyser read nothing the board transmitted")
    if tx_n and not board_rx:
        Fail("   the board read nothing the analyser transmitted")
    if can_rx and board_rx:
        Ok("   both directions carried frames")
    return 0


if __name__ == "__main__":
    sys.exit(main())
