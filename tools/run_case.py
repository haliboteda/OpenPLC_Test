"""Run one TestCase case while capturing the board's system log, and optionally
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
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (board_ip, Section, Ok, Warn, Fail, boot_log_next,  # noqa: E402
                    close_ports, emit_file, get_go_bin, get_iap_tool,
                    get_programmer_cli, get_scratch_file, open_board_log,
                    read_boot_log_swd, run_capture, run_while_draining)

RESET_WATCH_S = 8


def then_reset(seconds):
    """The G1 half: reset the board and judge what comes out of the next boot.
    Read from the SRAM4 buffer over SWD, as the ST-Link does the reset anyway."""
    Section("Reset, then watch the next boot")
    cli = get_programmer_cli()
    mark = boot_log_next(read_boot_log_swd(cli) or [])
    out, _ = run_capture([cli, "-c", "port=SWD", "mode=UR", "-rst"])
    for line in out.splitlines():
        if "Error" in line or "Reset" in line:
            print(line)

    time.sleep(seconds)
    boot = "\n".join(t for _, t in read_boot_log_swd(cli, since=mark) or [])
    if boot:
        Section("boot log (after reset)")
        print(boot)

    Section("Verdict")
    if "[sys][BL] ** APP Mod" in boot:
        Ok("PASS - the previously-installed application still boots after a rejected upload.")
        Ok("       The application region was never touched. This is SDRAM staging working.")
        return 0
    if "no valid application" in boot or "App signature invalid" in boot:
        Fail("FAIL - the board considers its application invalid, so the upload damaged it.")
        Fail("       That is the pre-staging behaviour; staging did not take effect.")
        return 1
    if not boot.strip():
        Warn("no new boot log record after reset - cannot judge")
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
    args = ap.parse_args()

    ip = args.ip or board_ip()
    if not ip:
        Fail("need --ip (or BOARD_UID in config/machine.py)")
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
    # Shows what the app says during the case; a board in the bootloader has no port 1.
    open_ports = open_board_log(sys_only=True)
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
            Section("board log %s" % k)
            print(text)

    if not args.then_reset:
        return rc

    return then_reset(args.reset_watch_seconds)


if __name__ == "__main__":
    sys.exit(main())
