"""T3-06 -- run the own-library examples on the real board, one at a time.

    python3 tools/run_examples.py --cdc COM11 --rs485 COM16 --rs232 COM17
    python3 tools/run_examples.py --only RS485_Echo --skip-upload

Each example is compiled from the installed board package, uploaded over USB
CDC, and judged by the PC from the example's own human-readable output. The
examples carry no machine lines; the criteria live in EXAMPLES below.
Decided in $PROD/maps/core-examples-on-board/issues/EXB-01-how-does-the-script-judge-an-example.md

Steps that need a person run first, so the operator can leave once they are
done. What the board's output shows is waited for (HUMAN_S at most); what only
a person can see is a single y/n key. No measured value is recorded.
Decided in $PROD/maps/core-examples-on-board/issues/EXB-05-how-does-the-script-ask-for-a-human-step.md

The CAN analyser is the CANable from can_send.py, found by USB id unless --can
is given. --skip-upload misses the examples that only print at start-up.

Exit 0 = every example run passed, 1 = one failed, 2 = setup problem.
"""

import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (cfg, Section, Ok, Fail, banner, decode_serial,  # noqa: E402
                    get_iap_tool, get_scratch_dir, run_capture, wait_for_board)

FQBN = ("OpenPLC_Alpha:stm32:OPEN-PLC:pnum=PLC_H743,usb=CDCgen,xusb=FS,"
        "upload_method=cdcMethod,knxrole=dual_device")

def di_bits(n):
    """The DI_Inputs line with only DI n at 1 (n = 0 for all off)."""
    return r"DI1\.\.DI8: %s\r?\n" % " ".join(
        "1" if i == n else "0" for i in range(1, 9))


def di_steps():
    steps = [("expect", [("cdc", di_bits(0))], 3.0,
              "every input should read 0 with the 24 V supply on and nothing "
              "applied; all 1 means the supply is off")]
    for n in range(1, 9):
        steps.append(("do", "Apply 24 V to DI%d" % n, [("cdc", di_bits(n))]))
        steps.append(("do", "Remove 24 V from DI%d" % n, [("cdc", di_bits(0))]))
    return steps


# One row per example, in run order: the ones that need a person come first.
# "ready" is the first line the example prints, on the named port. Steps:
#   ("send", port, bytes)              clears what was read so far, then writes
#   ("expect", [(port, regex)], s[, hint])   every regex within s seconds
#   ("do", action, [(port, regex)])    hands-on prompt, waits up to HUMAN_S
#   ("ask", question)                  hands-on prompt, one y/n key
#   ("range", port, regex, lo, hi, s)  every captured number within lo..hi
#   ("discover",)                      the address the example printed answers
#                                      UDP discovery
EXAMPLES = [
    {
        "name": "DI_Inputs",
        "lib": "OpenPLC_Ports",
        "ready": ("cdc", r"DI_Inputs: prints DI1\.\.DI8 on every change\."),
        "setup": "a 24 V lead you can move from DI1 to DI8",
        "steps": di_steps(),
    },
    {
        "name": "DO_Outputs",
        "lib": "OpenPLC_Ports",
        "ready": ("cdc", r"DO_Outputs: each output turns on for 1 s, in order\."),
        "steps": [
            ("expect", [("cdc", r"DO8 on")], 10.0),
            ("ask", "Do DO1..DO8 each turn on for 1 s, in order (it repeats)?"),
        ],
    },
    {
        "name": "Relays",
        "lib": "OpenPLC_Ports",
        "ready": ("cdc", r"Relays: each relay closes for 1 s, in order\."),
        "steps": [
            ("expect", [("cdc", r"RY6 closed")], 10.0),
            ("ask", "Do RY1..RY6 each click on and off, in order (it repeats)?"),
        ],
    },
    {
        "name": "SystemLED",
        "lib": "OpenPLC_Ports",
        "ready": ("cdc", r"SystemLED: blinks once a second\."),
        "steps": [
            ("expect", [("cdc", r"\boff\r?\n")], 3.0),
            ("ask", "Does the system LED blink, half a second on, half off?"),
        ],
    },
    {
        "name": "AO_Outputs",
        "lib": "OpenPLC_Ports",
        "ready": ("cdc", r"AO_Outputs: both outputs step through 0\.\.20 mA\."),
        "setup": "a meter on AO1 and on AO2",
        "steps": [
            ("expect", [("cdc", r"AO1 = AO2 = 20\.0 mA")], 20.0),
            ("ask", "Do both meters step 0, 5, 10, 15, 20 mA, 3 s each "
                    "(it repeats)?"),
        ],
    },
    {
        "name": "USB_Serial",
        "lib": "OpenPLC_Ports",
        "ready": ("cdc", r"USB_Serial: type a line and press Enter\."),
        "steps": [
            ("send", "cdc", b"EXB-PING-2\n"),
            ("expect", [("cdc", r"1: EXB-PING-2")], 2.0),
        ],
    },
    {
        "name": "RS485_Echo",
        "lib": "OpenPLC_Ports",
        "ready": ("cdc", r"RS485_Echo: send a line over RS485\."),
        "steps": [
            ("send", "rs485", b"EXB-PING-1\n"),
            ("expect", [("rs485", r"EXB-PING-1\n"),
                        ("cdc", r"RS485 echoed: EXB-PING-1")], 2.0),
        ],
    },
    {
        "name": "RS232_Echo",
        "lib": "OpenPLC_Ports",
        "ready": ("cdc", r"RS232_Echo: type in a terminal on the RS232 port\."),
        "steps": [
            ("send", "rs232", b"EXB-PING-3\n"),
            ("expect", [("rs232", r"EXB-PING-3\n"),
                        ("cdc", r"RS232 got 0x45")], 2.0),
        ],
    },
    {
        "name": "CAN_Counter",
        "lib": "OpenPLC_Ports",
        "ready": ("cdc", r"CAN_Counter: sending ID 0x123 every second"),
        "setup": "the CAN analyser's 120 ohm terminator switched on",
        "steps": [
            ("expect", [("can", r"(?s)t1234[0-9A-F]{8}\r.*t1234[0-9A-F]{8}\r")],
             3.0),
            ("send", "can", b"t4562BEEF\r"),
            ("expect", [("cdc", r"RX id=0x456 len=2 data=BE EF")], 2.0),
        ],
    },
    {
        "name": "Ethernet_IP",
        "lib": "OpenPLC_Ports",
        "ready": ("cdc", r"Ethernet_IP: waiting for link and address\."),
        "setup": "an Ethernet cable to a network with DHCP",
        "steps": [
            ("expect", [("cdc", r"link up"),
                        ("cdc", r"address \d+\.\d+\.\d+\.\d+")], 30.0),
            ("discover",),
        ],
    },
    {
        "name": "SD_ReadWrite",
        "lib": "OpenPLC_Ports",
        "ready": ("cdc", r"SD_ReadWrite: |Read back: "),
        "setup": "a FAT32 or exFAT microSD card in the slot",
        "steps": [
            ("expect", [("cdc", r"SD_ReadWrite: OK")], 5.0),
        ],
    },
    {
        "name": "BoardTemperature",
        "lib": "OpenPLC_Ports",
        "ready": ("cdc", r"BoardTemperature: degrees C, once a second\."),
        "steps": [
            ("range", "cdc",
             r"protection = (-?[\d.]+) C   output switches = (-?[\d.]+) C",
             10.0, 60.0, 3.0),
        ],
    },
    {
        "name": "SDRAM_Basic",
        "lib": "OpenPLC_SDRAM",
        "ready": ("rs232", r"SDRAM ready, \d+ MB free"),
        "steps": [
            ("expect", [("rs232", r"mismatches: 0\r?\n")], 5.0),
        ],
    },
    {
        "name": "SDRAM_DataLogger",
        "lib": "OpenPLC_SDRAM",
        "ready": ("rs232", r"=== SDRAM data logger ==="),
        "steps": [
            ("expect", [("rs232", r"press 'd' for a dump")], 5.0),
            ("send", "rs232", b"d"),
            ("expect", [("rs232", r"samples held: \d+")], 10.0),
        ],
    },
]

# The board re-enumerates after the upload; the sketch then waits up to 3 s
# for the host to open the port before it prints its banner.
REOPEN_S = 15.0
READY_S = 8.0
HUMAN_S = 120.0


def sketch_dir(ex):
    return (Path(cfg.CORE_LIVE) / "libraries" / ex["lib"] / "examples"
            / ex["name"])


def ports_of(ex):
    used = {ex["ready"][0]}
    for st in ex["steps"]:
        if st[0] == "send":
            used.add(st[1])
        elif st[0] in ("expect", "do"):
            used |= {k for k, _ in st[1 if st[0] == "expect" else 2]}
        elif st[0] == "range":
            used.add(st[1])
    return used | {"cdc"}


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


def getkey():
    """One key, no Enter. Falls back to a line when stdin is not a console."""
    if not sys.stdin.isatty():
        return sys.stdin.readline().strip()[:1].lower()
    if sys.platform == "win32":
        import msvcrt
        return msvcrt.getwch().lower()
    import termios
    import tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        return sys.stdin.read(1).lower()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def ask(lines):
    """Hands-on y/n. Returns (True, "") or (False, note)."""
    banner(lines + ["Press y if so, n if not."])
    print("\a", end="", flush=True)
    while True:
        k = getkey()
        if k in ("y", "n"):
            break
    if k == "y":
        return True, ""
    return False, input("  what did you see? ").strip()


def report_missing(missing, bufs, seconds, hint=""):
    for k, rx in missing:
        Fail("%s: no /%s/ within %.0f s; got %r"
             % (k, rx, seconds, bufs[k][-200:]))
    if hint:
        Fail("  " + hint)


def run_steps(ex, ports, bufs):
    """Returns (0, "") on pass, (1, note) on fail."""
    for st in ex["steps"]:
        kind = st[0]
        if kind == "send":
            for k in bufs:
                bufs[k] = ""
            ports[st[1]].write(st[2])
        elif kind == "expect":
            missing = wait_for(ports, bufs, st[1], st[2])
            if missing:
                report_missing(missing, bufs, st[2], st[3] if len(st) > 3 else "")
                return 1, ""
        elif kind == "do":
            for k in bufs:
                bufs[k] = ""
            banner([st[1], "The script carries on by itself once the board "
                           "shows it (%d s at most)." % HUMAN_S])
            print("\a", end="", flush=True)
            missing = wait_for(ports, bufs, st[2], HUMAN_S)
            if missing:
                report_missing(missing, bufs, HUMAN_S)
                return 1, "not seen after: " + st[1]
            Ok(st[1])
        elif kind == "ask":
            ok, note = ask([st[1]])
            if not ok:
                Fail("operator: %s" % (note or "no"))
                return 1, note
        elif kind == "range":
            _, port, rx, lo, hi, seconds = st
            missing = wait_for(ports, bufs, [(port, rx)], seconds)
            if missing:
                report_missing(missing, bufs, seconds)
                return 1, ""
            values = [float(v) for v in re.search(rx, bufs[port]).groups()]
            if not all(lo <= v <= hi for v in values):
                Fail("%s outside %g..%g" % (values, lo, hi))
                return 1, ""
            Ok("%s within %g..%g" % (values, lo, hi))
        elif kind == "discover":
            ip = re.search(r"address (\d+\.\d+\.\d+\.\d+)", bufs["cdc"]).group(1)
            if not wait_for_board(ip, timeout=30.0):
                Fail("%s does not answer discovery" % ip)
                return 1, ""
            Ok("%s answers discovery" % ip)
    return 0, ""


def run_one(serial, ex, names, aux, key, skip_upload):
    """aux: the non-CDC ports, opened before the upload so nothing printed at
    start-up is missed. Returns (rc, note); rc 2 = stop the run."""
    if not skip_upload:
        rc = build_and_upload(ex, names["cdc"], key)
        if rc:
            return rc, ""
    ports = {k: v for k, v in aux.items() if k in ports_of(ex)}
    try:
        ports["cdc"] = open_retry(serial, names["cdc"], REOPEN_S)
    except Exception as e:
        Fail("cannot open port: %s" % e)
        return 2, ""
    try:
        bufs = {k: "" for k in ports}
        rport, rrx = ex["ready"]
        if wait_for(ports, bufs, [(rport, rrx)], READY_S):
            Fail("no banner /%s/ on %s" % (rrx, rport))
            print(bufs[rport][-400:])
            return 1, ""
        return run_steps(ex, ports, bufs)
    finally:
        ports["cdc"].close()


def open_aux(serial, names, needed):
    aux = {}
    for k in needed:
        if k == "can":
            from can_send import Analyser, find_analyser
            port = find_analyser(names["can"])
            if not port:
                raise RuntimeError("no CAN analyser found; give --can")
            aux[k] = Analyser(port, 500000).sp
        else:
            aux[k] = serial.Serial(names[k], 115200, timeout=0.1)
    return aux


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", default="")
    ap.add_argument("--cdc", default=getattr(cfg, "CDC_PORT", ""))
    ap.add_argument("--rs485", default="",
                    help="the USB-RS485 adapter wired to A10/A11")
    ap.add_argument("--rs232", default="",
                    help="the RS232 adapter wired to C05/C06")
    ap.add_argument("--can", default="",
                    help="the CANable's port, if it is not found by USB id")
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
    names = {"cdc": args.cdc, "rs485": args.rs485, "rs232": args.rs232,
             "can": args.can}

    chosen = [e for e in EXAMPLES if not args.only or e["name"] == args.only]
    if not chosen:
        Fail("no example named %s" % args.only)
        return 2

    needed = set().union(*(ports_of(e) for e in chosen)) - {"cdc"}
    absent = sorted(k for k in needed if k != "can" and not names[k])
    if absent:
        Fail("needs --%s" % " --".join(absent))
        return 2
    try:
        aux = open_aux(serial, names, needed)
    except Exception as e:
        Fail("cannot open port: %s" % e)
        return 2

    setup = [e["setup"] for e in chosen if "setup" in e]
    if setup:
        ok, _ = ask(["Before starting, have ready:"] +
                    ["- " + s for s in setup])
        if not ok:
            Fail("not ready; nothing run")
            return 2

    failed = []
    try:
        for ex in chosen:
            Section(ex["name"])
            rc, note = run_one(serial, ex, names, aux, args.key,
                               args.skip_upload)
            if rc == 2:
                return 2
            if rc == 0:
                Ok("PASS")
            else:
                failed.append((ex["name"], note))
    finally:
        for sp in aux.values():
            sp.close()

    Section("result")
    for name, note in failed:
        Fail("%s%s" % (name, (" -- " + note) if note else ""))
    if failed:
        return 1
    Ok("all %d passed" % len(chosen))
    return 0


if __name__ == "__main__":
    sys.exit(main())
