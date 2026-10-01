"""T3-06 -- run the own-library examples on the real board, one at a time.

    python3 tools/run_examples.py --cdc COM11 --rs485 COM16
    python3 tools/run_examples.py --only RS485_Echo --skip-upload

Each example is compiled from the installed board package, uploaded over USB
CDC, and judged by the PC from the example's own human-readable output. The
examples carry no machine lines; the criteria live in EXAMPLES below.
Decided in $PROD/maps/core-examples-on-board/issues/EXB-01-how-does-the-script-judge-an-example.md

Exit 0 = every example run passed, 1 = one failed, 2 = setup problem.
"""

import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (cfg, Section, Ok, Fail, decode_serial,  # noqa: E402
                    get_iap_tool, get_scratch_dir, run_capture)

FQBN = ("OpenPLC_Alpha:stm32:OPEN-PLC:pnum=PLC_H743,usb=CDCgen,xusb=FS,"
        "upload_method=cdcMethod,knxrole=dual_device")

# One row per example. "ready" is the setup() banner on the CDC port; "send"
# goes out on the named port once it is seen; every "expect" must then show
# up on its port within "within_s".
EXAMPLES = [
    {
        "name": "RS485_Echo",
        "lib": "OpenPLC_Ports",
        "ready": r"RS485_Echo: send a line over RS485\.",
        "send": ("rs485", b"EXB-PING-1\n"),
        "expect": [("rs485", r"EXB-PING-1\n"),
                   ("cdc", r"RS485 echoed: EXB-PING-1")],
        "within_s": 2.0,
    },
]

# The board re-enumerates after the upload; the sketch then waits up to 3 s
# for the host to open the port before it prints its banner.
REOPEN_S = 15.0
READY_S = 8.0


def sketch_dir(ex):
    return (Path(cfg.CORE_LIVE) / "libraries" / ex["lib"] / "examples"
            / ex["name"])


def build_and_upload(ex, cdc, key):
    """Returns 0 to carry on, or the exit code to stop with."""
    cli = getattr(cfg, "ARDUINO_CLI", "")
    if not cli or not Path(cli).exists():
        Fail("arduino-cli not found. Set ARDUINO_CLI in config/machine.py")
        return 2
    build = get_scratch_dir() / ("exb_" + ex["name"])
    out, rc = run_capture([cli, "compile", "--config-file",
                           cfg.ARDUINO_CLI_CONFIG, "--fqbn", FQBN,
                           "--build-path", build, sketch_dir(ex)])
    if rc != 0:
        print("\n".join(out.splitlines()[-5:]))
        Fail("compile failed")
        return 2
    binary = build / (ex["name"] + ".ino.bin")
    argv = [get_iap_tool(), "cdc", str(binary), cdc]
    if key:
        argv.append("--key=%s" % key)
    out, rc = run_capture(argv)
    if rc != 0:
        print("\n".join(out.splitlines()[-8:]))
        Fail("upload failed (IAPTool exit %d)" % rc)
        return 2
    Ok("uploaded")
    return 0


def open_retry(serial, port, seconds):
    deadline = time.time() + seconds
    while True:
        try:
            return serial.Serial(port, 115200, timeout=0.1)
        except Exception:
            if time.time() > deadline:
                raise
            time.sleep(0.3)


def wait_for(ports, bufs, want, seconds):
    """Reads every port into bufs until each (port, regex) in want matches."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        for k, sp in ports.items():
            bufs[k] += decode_serial(sp.read(4096))
        if all(re.search(rx, bufs[k]) for k, rx in want):
            return []
    return [(k, rx) for k, rx in want if not re.search(rx, bufs[k])]


def run_one(serial, ex, names):
    ports = {}
    try:
        ports["cdc"] = open_retry(serial, names["cdc"], REOPEN_S)
        for k in {ex["send"][0]} | {k for k, _ in ex["expect"]}:
            if k not in ports:
                ports[k] = serial.Serial(names[k], 115200, timeout=0.1)
    except Exception as e:
        Fail("cannot open port: %s" % e)
        return 2
    try:
        bufs = {k: "" for k in ports}
        if wait_for(ports, bufs, [("cdc", ex["ready"])], READY_S):
            Fail("no banner /%s/ on the CDC port" % ex["ready"])
            print(bufs["cdc"][-400:])
            return 1
        for k in bufs:
            bufs[k] = ""
        port, data = ex["send"]
        ports[port].write(data)
        missing = wait_for(ports, bufs, ex["expect"], ex["within_s"])
        for k, rx in missing:
            Fail("%s: no /%s/ within %.1f s; got %r"
                 % (k, rx, ex["within_s"], bufs[k][-200:]))
        return 1 if missing else 0
    finally:
        for sp in ports.values():
            sp.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", default="")
    ap.add_argument("--cdc", default=getattr(cfg, "CDC_PORT", ""))
    ap.add_argument("--rs485", default="",
                    help="the USB-RS485 adapter wired to A10/A11")
    ap.add_argument("--key", default="",
                    help="owner private key, for a claimed board")
    ap.add_argument("--skip-upload", action="store_true")
    args = ap.parse_args()

    try:
        import serial
    except ImportError:
        Fail("pyserial is not installed: python -m pip install pyserial")
        return 2
    if not args.cdc:
        Fail("need --cdc (or set CDC_PORT in config/machine.py)")
        return 2
    names = {"cdc": args.cdc, "rs485": args.rs485}

    chosen = [e for e in EXAMPLES if not args.only or e["name"] == args.only]
    if not chosen:
        Fail("no example named %s" % args.only)
        return 2

    failed = []
    for ex in chosen:
        Section(ex["name"])
        needed = {ex["send"][0]} | {k for k, _ in ex["expect"]}
        absent = [k for k in needed if not names.get(k)]
        if absent:
            Fail("needs --%s" % " --".join(absent))
            return 2
        if not args.skip_upload:
            rc = build_and_upload(ex, args.cdc, args.key)
            if rc:
                return rc
        rc = run_one(serial, ex, names)
        if rc == 2:
            return 2
        if rc == 0:
            Ok("PASS")
        else:
            failed.append(ex["name"])

    Section("result")
    if failed:
        Fail("failed: %s" % " ".join(failed))
        return 1
    Ok("all %d passed" % len(chosen))
    return 0


if __name__ == "__main__":
    sys.exit(main())
