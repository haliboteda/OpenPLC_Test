"""Run one TestCase case while capturing the board's serial log, and optionally
reset afterwards to see what the board does on the next boot.

    python3 tools/run_case.py --case T1-01
    python3 tools/run_case.py --case T1-11 --bin <file.bin> --then-reset

--then-reset is what turns T1-11 into G1: T1-11 proves the board refuses a bad image,
the reset afterwards proves the previously-installed application still boots.
Before SDRAM staging that second half was impossible -- a rejected upload had
already destroyed the running application.

Exit code is the TestCase binary's own, or 1 when the --then-reset verdict fails.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (cfg, Section, Ok, Warn, Fail, close_ports,  # noqa: E402
                    emit_file, get_go_bin, get_iap_tool, get_programmer_cli,
                    get_scratch_file, open_log_ports, read_log_ports,
                    run_capture, run_while_draining)

RESET_WATCH_S = 8


def then_reset(ports, seconds):
    """The G1 half: reset the board and judge what comes out of the next boot."""
    Section("Reset, then watch the next boot")
    open2 = open_log_ports(ports)
    out, _ = run_capture([get_programmer_cli(), "-c", "port=SWD", "mode=UR", "-rst"])
    for line in out.splitlines():
        if "Error" in line or "Reset" in line:
            print(line)

    buf2 = read_log_ports(open2, seconds)
    for k, text in buf2.items():
        if text:
            Section("serial %s (after reset)" % k)
            print(text)

    Section("Verdict")
    boot = "\n".join(buf2.values())
    if "APP Mod" in boot:
        Ok("PASS - the previously-installed application still boots after a rejected upload.")
        Ok("       The application region was never touched. This is SDRAM staging working.")
        return 0
    if "no valid application" in boot or "App signature invalid" in boot:
        Fail("FAIL - the board considers its application invalid, so the upload damaged it.")
        Fail("       That is the pre-staging behaviour; staging did not take effect.")
        return 1
    if not boot.strip():
        Warn("no serial output after reset - cannot judge")
        return 1
    Warn("board booted but into neither state cleanly - read the log above")
    return 1


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--case", required=True)
    ap.add_argument("--ip", default="")
    ap.add_argument("--bin", default="")
    ap.add_argument("--key", default="", help="the key this board trusts; T1-11/T1-12 sign the challenge with it")
    ap.add_argument("--iaptool", default="")
    ap.add_argument("--then-reset", action="store_true")
    ap.add_argument("--reset-watch-seconds", type=int, default=RESET_WATCH_S)
    ap.add_argument("--ports", nargs="*", default=None)
    args = ap.parse_args()

    ports = args.ports if args.ports is not None else cfg.LOG_PORTS
    ip = args.ip or getattr(cfg, "BOARD_IP", "")
    if not ip:
        Fail("need --ip (or set BOARD_IP in config)")
        return 1

    iaptool = Path(args.iaptool) if args.iaptool else get_iap_tool()
    keyfile = Path(args.key) if args.key else iaptool.parent / "keys" / "fw_signing_key.pem"

    tt = get_go_bin("TestCase")
    if not tt.exists():
        Fail("TestCase not built - run: go build -o Output/<goos>/TestCase .")
        return 1

    argv = [args.case, "--ip=%s" % ip, "--iaptool=%s" % iaptool]
    if args.bin:
        argv.append("--bin=%s" % args.bin)
    if keyfile.exists():
        argv.append("--key=%s" % keyfile)

    Section("Case %s" % args.case)
    open_ports = open_log_ports(ports)
    print("TestCase %s" % " ".join(argv))

    tt_out = get_scratch_file("tt.out")
    tt_err = get_scratch_file("tt.err")
    rc, buf = run_while_draining([tt] + argv, open_ports, tt_out, tt_err,
                                 tail_seconds=0.8)
    close_ports(open_ports)

    Section("TestCase output (exit %d)" % rc)
    emit_file(tt_out)
    emit_file(tt_err)

    for k, text in buf.items():
        if text:
            Section("serial %s" % k)
            print(text)

    if not args.then_reset:
        return rc

    return then_reset(ports, args.reset_watch_seconds)


if __name__ == "__main__":
    sys.exit(main())
