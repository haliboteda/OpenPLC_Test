"""T3-02 -- build, flash and read out the SDRAM wrapper acceptance sketch.

    python3 tools/run_sdram.py               build, flash, collect
    python3 tools/run_sdram.py --skip-flash  the sketch is already on the board

The sketch prints "RESULT <name> PASS|FAIL" lines and "MEASURE <name> <n>"
lines. This script fails on any FAIL, on a missing DONE (which means the sketch
stopped early -- a hang or a fault), and on zero results collected.

The one that matters most is alloc_is_zeroed: the whole reason the wrapper
exists is to take "memset it yourself" away from the caller.

Exit 0 = every check passed, 1 = something failed, 2 = setup problem.
"""

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (cfg, Section, Ok, Warn, Fail, get_go_bin,  # noqa: E402
                    get_iap_tool, get_programmer_cli, get_scratch_dir,
                    get_scratch_file, open_log_ports, read_log_ports,
                    decode_serial, run_capture)

FQBN = ("OpenPLC_Alpha:stm32:OPEN-PLC:pnum=PLC_H743,usb=CDCgen,xusb=FS,"
        "upload_method=cdcMethod,knxrole=dual_device")

ACCEPTED = "Checksum and signature OK"
MIB = 1024.0 * 1024.0


def build(sketch):
    """Compile the sketch and return the path to its .bin, or None."""
    cli = getattr(cfg, "ARDUINO_CLI", "")
    if not cli or not Path(cli).exists():
        Fail("arduino-cli not found. Set ARDUINO_CLI in config/machine.py")
        return None

    build_path = get_scratch_dir() / "sdram_build"

    Section("building")
    out, rc = run_capture([cli, "compile", "--warnings", "all",
                           "--config-file", cfg.ARDUINO_CLI_CONFIG,
                           "--fqbn", FQBN,
                           "--build-path", build_path, sketch])
    if rc != 0:
        Fail("compile failed")
        for line in out.splitlines():
            if "error" in line:
                print("    " + line)
        return None
    for line in out.splitlines():
        if re.search(r"program storage|dynamic memory", line):
            print("  " + line)

    bin_path = build_path / "SDRAM_Acceptance.ino.bin"
    if not bin_path.exists():
        Fail("no .bin at %s" % bin_path)
        return None

    # The .bin must not grow by the size of the buffers. That is the whole point
    # of not using a linker section -- report it so a regression is visible
    # rather than merely absent.
    print("  image size: %d bytes" % bin_path.stat().st_size)
    return bin_path


def flash_and_collect(bin_path, ip, port, collect_seconds):
    """Upload over ethernet, holding the log port open across the whole flash.

    IAPTool exits before the board has finished writing, and the sketch's output
    starts right after the board reboots itself -- so the port must already be
    open when IAPTool returns.
    """
    Section("flashing")
    iap = get_go_bin("IAPTool")
    if not iap.exists():
        iap = get_iap_tool()

    open_ports = open_log_ports([port])
    log = ""
    with open(get_scratch_file("sdram_flash.out"), "w") as so, \
            open(get_scratch_file("sdram_flash.err"), "w") as se:
        proc = subprocess.Popen([str(iap), "ether", str(bin_path), ip], stdout=so, stderr=se)
        while proc.poll() is None:
            for h in open_ports.values():
                try:
                    n = h.in_waiting
                    if n:
                        log += decode_serial(h.read(n))
                except Exception:
                    pass
            time.sleep(0.05)

    for text in read_log_ports(open_ports, collect_seconds).values():
        log += text

    if ACCEPTED not in log:
        Fail("the board did not accept the image")
        tail = [ln for ln in log.splitlines() if ln.strip()][-8:]
        for line in tail:
            print("    " + line)
        return None
    return log


def collect_only(port, collect_seconds):
    Section("collecting (reset to restart the sketch)")
    open_ports = open_log_ports([port])
    run_capture([get_programmer_cli(), "-c", "port=SWD", "mode=UR", "-rst"])
    return "\n".join(read_log_ports(open_ports, collect_seconds).values())


def report(serial):
    """Print what the board said, the measurements, and the verdict."""
    Section("board said")
    for line in serial.splitlines():
        if re.search(r"RESULT|MEASURE|DONE|SDRAM|===", line):
            print("    | " + line)

    results = re.findall(r"RESULT\s+(\S+)\s+(PASS|FAIL)", serial)
    failed = [name for name, verdict in results if verdict == "FAIL"]

    Section("measurements")
    for name, value in re.findall(r"MEASURE\s+(\S+)\s+(\d+)", serial):
        print("  %-14s %s" % (name, value))

    zb = re.search(r"MEASURE zero_bytes (\d+)", serial)
    zu = re.search(r"MEASURE zero_us (\d+)", serial)
    if zb and zu and float(zu.group(1)) > 0:
        mb = float(zb.group(1)) / MIB
        ms = float(zu.group(1)) / 1000.0
        print("  -> zeroing {:,.0f} MB took {:,.1f} ms ({:,.0f} MB/s)".format(
            mb, ms, mb / (ms / 1000.0)))
        print("  -> extrapolated for the full 64 MB: {:,.0f} ms".format(
            ms * 64.0 / mb))

    Section("result")
    print("  checks: %d run, %d failed" % (len(results), len(failed)))
    if not results:
        Fail("no RESULT lines at all -- the sketch never ran, or the port is wrong")
        return 1
    if "DONE" not in serial:
        Fail("no DONE line -- the sketch stopped partway (hang or fault)")
        return 1
    if failed:
        for name in failed:
            Fail("  FAILED: " + name)
        return 1
    Ok("all %d SDRAM checks passed" % len(results))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--skip-flash", action="store_true",
                    help="the sketch is already on the board")
    ap.add_argument("--ip", default="")
    ap.add_argument("--port", default="")
    ap.add_argument("--collect-seconds", type=int, default=25)
    args = ap.parse_args()

    ip = args.ip or getattr(cfg, "BOARD_IP", "")
    port = args.port or (cfg.LOG_PORTS[0] if cfg.LOG_PORTS else "")

    sketch = Path(cfg.TEST_REPO) / "onboard" / "sdram" / "SDRAM_Acceptance"
    if not sketch.exists():
        Fail("sketch not found: %s" % sketch)
        return 2

    if args.skip_flash:
        serial = collect_only(port, args.collect_seconds)
    else:
        if not ip:
            Fail("need --ip (or set BOARD_IP in config/machine.py)")
            return 2
        bin_path = build(sketch)
        if bin_path is None:
            return 2
        serial = flash_and_collect(bin_path, ip, port, args.collect_seconds)
        if serial is None:
            return 2

    return report(serial)


if __name__ == "__main__":
    sys.exit(main())
