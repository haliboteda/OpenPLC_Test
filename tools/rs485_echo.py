"""R2 / R4 -- prove RS485 carries traffic in both directions.

    python3 tools/rs485_echo.py --port COM16
    python3 tools/rs485_echo.py --port COM16 --listen 8

Needs a USB-RS485 adapter wired to the board's RS485 terminals:

    adapter A  <->  terminal A10  (UpperDeck J11-3)
    adapter B  <->  terminal A11  (UpperDeck J11-2)

and TestCase/RS485/rs485_test.c running on the board (RS485_TEST_ENABLE 1).

⚠ The board cannot hear itself. SP3485EN has /RE and DE on one net (PD4), so
the receiver is off while the driver is on -- an adapter is the only way to
prove either direction.

    R2  board -> host: the banner "RS485 HELLO <n>" shows up on the adapter
    R4  host -> board: a probe string comes back byte for byte

Exit 0 = both directions passed, 1 = a direction failed, 2 = setup problem.
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import cfg, Section, Ok, Fail, decode_serial  # noqa: E402

BANNER = "RS485 HELLO"
PROBE = b"PING-FROM-HOST-0123456789"

# The board echoes a frame once the line has been quiet for 20 ms
# (RS485_IDLE_GAP_MS), so the echo cannot start earlier than that.
ECHO_WAIT_S = 2.0


def drain(port, seconds):
    """Collect everything the adapter hears for `seconds`."""
    text = ""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        n = port.in_waiting
        if n:
            text += decode_serial(port.read(n))
        else:
            time.sleep(0.02)
    return text


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", default="", help="the USB-RS485 adapter's port")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--listen", type=float, default=6.0,
                    help="seconds to wait for the banner (it repeats every 2 s)")
    args = ap.parse_args()

    port_name = args.port or getattr(cfg, "RS485_PORT", "")
    if not port_name:
        Fail("need --port (the USB-RS485 adapter, not the RS232 console)")
        return 2

    try:
        import serial
    except ImportError:
        Fail("pyserial is not installed: python -m pip install pyserial")
        return 2

    try:
        port = serial.Serial(port_name, args.baud, timeout=0.2)
    except Exception as e:
        Fail("cannot open %s: %s" % (port_name, e))
        return 2

    failures = []
    with port:
        Section("R2  board -> host")
        print("listening on %s @ %d for %.0fs" % (port_name, args.baud, args.listen))
        heard = drain(port, args.listen)
        for line in heard.splitlines():
            if line.strip():
                print("    | " + line.strip())
        if BANNER in heard:
            Ok("R2 PASS - the banner arrived over RS485")
        else:
            Fail("R2 FAIL - no %r in %d bytes" % (BANNER, len(heard)))
            failures.append("R2")

        Section("R4  host -> board")
        # Start from a clean line so the echo cannot be confused with a banner
        # that was already in flight.
        port.reset_input_buffer()
        print("sending %d bytes: %s" % (len(PROBE), PROBE.decode()))
        port.write(PROBE)
        port.flush()
        back = drain(port, ECHO_WAIT_S)
        for line in back.splitlines():
            if line.strip():
                print("    | " + line.strip())
        if PROBE.decode() in back:
            Ok("R4 PASS - the board echoed the probe back")
        else:
            Fail("R4 FAIL - the probe did not come back")
            failures.append("R4")

    Section("result")
    if failures:
        Fail("failed: " + ", ".join(failures))
        return 1
    Ok("RS485 works in both directions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
