"""T3-08 on the real board (requirement R3-11) -- a watchdog reset reaches the sketch as WATCHDOG.

    python tools/run_t3_08.py --key <owner.pem>

Uploads onboard/reset_cause/T3_08_Watchdog over USB CDC. The sketch stops
feeding the IWDG after 3 s; after the reset it must print
"WDT cause=WATCHDOG", then keep feeding with no further reset.
Also reports what upstream IWatchdog::isReset() said (expected 0: the
bootloader clears RCC->RSR before the jump).

Exit 0 = pass, 1 = fail, 2 = setup problem.
"""

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (cfg, Section, Ok, Fail, get_iap_tool,  # noqa: E402
                    get_scratch_dir, open_rs232_ports, read_log_ports,
                    run_capture)

FQBN = ("OpenPLC_Alpha:stm32:OPEN-PLC:pnum=PLC_H743,usb=CDCgen,"
        "upload_method=cdcMethod")
NAME = "T3_08_Watchdog"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cdc", default=getattr(cfg, "CDC_PORT", ""))
    ap.add_argument("--port", default=getattr(cfg, "RS232_PORT", ""),
                    help="RS232 adapter port (C05/C06), where the sketch prints")
    ap.add_argument("--key", default="")
    args = ap.parse_args()

    sketch = Path(cfg.TEST_REPO) / "onboard" / "reset_cause" / NAME
    build = get_scratch_dir() / ("t3_08_" + NAME)
    Section("build")
    out, rc = run_capture([cfg.ARDUINO_CLI, "compile", "--config-file",
                           cfg.ARDUINO_CLI_CONFIG, "--fqbn", FQBN,
                           "--build-path", build, sketch])
    if rc:
        print("\n".join(out.splitlines()[-5:]))
        Fail("compile failed")
        return 2

    ports = open_rs232_ports([args.port])
    if not ports:
        return 2
    Section("upload")
    argv = [get_iap_tool(), "cdc", str(build / (NAME + ".ino.bin")), args.cdc]
    if args.key:
        argv.append("--key=%s" % args.key)
    out, rc = run_capture(argv)
    if rc:
        print("\n".join(out.splitlines()[-8:]))
        Fail("upload failed")
        return 2

    Section("watch")
    log = "".join(read_log_ports(ports, 30, until=r"WDT fed ok",
                                 until_count=3).values())
    for line in log.splitlines():
        if re.search(r"WDT cause|Reset cause|fed ok", line):
            print("    | " + line.strip())

    m = re.search(r"WDT cause=(\w+) isReset=(\d)", log.split("WDT not feeding")[-1])
    if not m or "WDT not feeding" not in log:
        Fail("never saw the sketch stop feeding and come back")
        return 1
    if m.group(1) != "WATCHDOG":
        Fail("after the watchdog reset the sketch read %s" % m.group(1))
        return 1
    if len(re.findall(r"WDT fed ok", log)) < 3:
        Fail("fed, but the board kept resetting")
        return 1
    Ok("watchdog reset read as WATCHDOG; IWatchdog.isReset() = %s" % m.group(2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
