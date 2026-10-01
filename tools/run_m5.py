"""T3-03 (requirement R3-05) -- builds, flashes and drives the Serial_Test-vs-Serial conflict case.

    python3 tools/run_m5.py               build, flash, then test
    python3 tools/run_m5.py --skip-flash  the sketch is already on the board, just test

What it proves: after the user's sketch calls Serial.begin(), the core's
diagnostic port Serial_Test can still RECEIVE.

⚠️ Receive, not transmit. In the broken configuration Serial_Test still prints
perfectly -- both objects resolve to UART4, uart_handlers[] keeps one handler
per peripheral, and the loser only loses its RX path. A check that watches for
output alone passes on a board that is broken.

So the test writes a byte INTO the RS232 terminal (C06) and waits for the sketch
to echo it back. No echo = the diagnostic port is deaf = T3-03 fails.

Exit 0 = Serial_Test received, 1 = it did not, 2 = setup problem.
"""

import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (cfg, Section, Ok, Warn, Fail,  # noqa: E402
                    get_go_bin, get_iap_tool, get_programmer_cli,
                    get_scratch_dir, get_scratch_file, nonblank_lines,
                    open_log_ports, read_log_ports, run_capture,
                    run_while_draining)

FQBN = ("OpenPLC_Alpha:stm32:OPEN-PLC:pnum=PLC_H743,usb=CDCgen,xusb=FS,"
        "upload_method=cdcMethod,knxrole=dual_device")
LETTERS = ["A", "B", "C", "D", "E"]


def build_and_flash(sketch, ip, port):
    """Returns 0 to carry on, or the exit code to stop with."""
    cli = getattr(cfg, "ARDUINO_CLI", "")
    if not cli or not Path(cli).exists():
        Fail("arduino-cli not found. Set ARDUINO_CLI in config/machine.py")
        return 2
    if not ip:
        Fail("need --ip (or set BOARD_IP in config/machine.py)")
        return 2

    build_path = get_scratch_dir() / "m5_build"
    Section("building")
    out, rc = run_capture([cli, "compile", "--warnings", "all",
                           "--config-file", cfg.ARDUINO_CLI_CONFIG,
                           "--fqbn", FQBN, "--build-path", build_path, sketch])
    for line in out.splitlines()[-3:]:
        print(line)
    if rc != 0:
        Fail("compile failed")
        return 2

    binary = build_path / "M5_SerialConflict.ino.bin"
    if not binary.exists():
        Fail("no .bin at %s" % binary)
        return 2

    Section("flashing")
    iap = get_go_bin("IAPTool")
    if not iap.exists():
        iap = get_iap_tool()

    # ⚠️ IAPTool exits when the last byte is sent; the board is only then
    # verifying, erasing and writing from SDRAM. Resetting or testing here lands
    # mid-write and destroys the application. Wait for the board to say so.
    open_ports = open_log_ports([port])
    _, buf = run_while_draining([iap, "ether", str(binary), ip],
                                open_ports,
                                get_scratch_file("m5_flash.out"),
                                get_scratch_file("m5_flash.err"))
    log = "".join(buf.values())
    tail = read_log_ports(open_ports, 15)      # also closes them
    log += "".join(tail.values())

    if "Checksum and signature OK" not in log:
        Fail("the board did not accept the image")
        for line in nonblank_lines(log)[-8:]:
            print("    " + line)
        return 2
    Ok("flashed and accepted")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--skip-flash", action="store_true")
    ap.add_argument("--ip", default="")
    ap.add_argument("--port", default="")
    ap.add_argument("--bytes", type=int, default=5)
    args = ap.parse_args()

    ip = args.ip or getattr(cfg, "BOARD_IP", "")
    port = args.port or (cfg.LOG_PORTS[0] if cfg.LOG_PORTS else "")
    if not port:
        Fail("no log port: pass --port or set LOG_PORTS in config/machine.py")
        return 2

    sketch = Path(cfg.TEST_REPO) / "onboard" / "rs232" / "M5_SerialConflict"
    if not sketch.exists():
        Fail("sketch not found: %s" % sketch)
        return 2

    if not args.skip_flash:
        rc = build_and_flash(sketch, ip, port)
        if rc:
            return rc

    Section("can Serial_Test still RECEIVE after Serial.begin()?")

    try:
        import serial
    except ImportError:
        Fail("pyserial is not installed: python -m pip install pyserial")
        return 2
    try:
        sp = serial.Serial(port, cfg.LOG_BAUD, timeout=0.5)
    except Exception as e:
        Fail("cannot open %s: %s" % (port, e))
        return 2

    # Let the sketch get to its banner, and prove the port is alive at all: if
    # the banner is missing the board is not running this sketch and nothing
    # below means anything.
    time.sleep(1.5)
    banner_text = read_available(sp)
    if "[M5] ready" not in banner_text:
        Warn("no '[M5] ready' banner seen yet; resetting the board to catch it")
        run_capture([get_programmer_cli(), "-c", "port=SWD", "mode=UR", "-rst"])
        time.sleep(2.5)
        banner_text += read_available(sp)
    for line in nonblank_lines(banner_text):
        print("    | " + line)

    if "[M5] ready" not in banner_text:
        sp.close()
        if args.skip_flash:
            Fail("no banner, and this run did not flash -- is the M5 sketch actually on the board?")
            return 2
        # The sketch was flashed and accepted moments ago, so it IS running.
        # Silence here is the failure itself, not a setup problem: Serial_Test
        # lost its transmit path as well, which is worse than the documented
        # symptom.
        Fail("E7 FAILS: Serial_Test went silent entirely after Serial4.begin().")
        Fail("Not even the banner survived -- transmit died too, not just receive.")
        Fail("(the boot log above is cut off mid-line, which is that happening)")
        return 1
    Ok("transmit works (banner received) -- now testing receive")

    sent = echoed = 0
    for c in LETTERS[:max(0, min(args.bytes, 5))]:
        try:
            sp.write(c.encode("ascii"))
            sp.flush()
        except Exception as e:
            Fail("cannot write to %s : %s" % (port, e))
            sp.close()
            return 2
        sent += 1
        time.sleep(0.4)
        reply = read_available(sp)
        if re.search(r"\[echo\]\s*" + c, reply):
            Ok("    sent '%s' -> echoed" % c)
            echoed += 1
        else:
            Fail("    sent '%s' -> NOTHING came back (%s)"
                 % (c, re.sub(r"\r?\n", " ", reply)))
    sp.close()

    Section("result")
    print("  bytes sent: %d, echoed: %d" % (sent, echoed))
    if echoed == sent:
        Ok("E7 holds: Serial_Test still receives after the sketch opened Serial")
        return 0
    if echoed == 0:
        Fail("E7 FAILS: Serial_Test transmits but is completely deaf.")
        Fail("This is the documented symptom of Serial_Test and Serial sharing UART4.")
        return 1
    Fail("E7 FAILS intermittently: %d of %d echoed -- worse than a clean failure" % (echoed, sent))
    return 1


def read_available(sp):
    """ReadExisting(): whatever is buffered right now, decoded the same way the
    log ports are."""
    from common import decode_serial
    try:
        n = sp.in_waiting
        return decode_serial(sp.read(n)) if n else ""
    except Exception:
        return ""


if __name__ == "__main__":
    sys.exit(main())
