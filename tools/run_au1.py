"""T1-17 -- drives both phases of the nonce-uniqueness case around a real power cut.

    python3 tools/run_au1.py                   full run, prompts you to pull the plug
    python3 tools/run_au1.py --count 12        take 12 nonces per phase instead of 8
    python3 tools/run_au1.py --ip 192.168.0.7  override the address from config
    python3 tools/run_au1.py --resume          keep the nonces already collected and go
                                               straight to waiting for the power cut

Why a script and not one TestCase invocation: the network goes away with the
power, so the nonces from before the cut have to survive on disk. TestCase does
the protocol and the verdict; this file does the choreography.

It never asks you to press Enter. It watches the board's own UDP discovery to
see the power go and come back, so the timing recorded is the board's, not a
human's reaction time -- and so the script cannot be fooled by somebody
confirming a power cut that did not happen.

Exit 0 = T1-17 passed, 1 = it failed, 2 = the run could not be set up.
"""

import argparse
import datetime
import json
import re
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (cfg, GOOS_DIR, EXE, Section, Ok, Warn, Fail,  # noqa: E402
                    banner, close_ports, get_go_bin, get_programmer_cli,
                    get_scratch_file, local_ip_for, open_log_ports,
                    python_exe, read_log_ports, run_capture, run_emit,
                    target_voltage)

DISCOVERY = b"openplc_server_where_r_y"


def probe_board(addr, udp_port, timeout=1.5, local_ip=None):
    """One UDP discovery query. Returns the identity string, or None when the
    board does not answer -- which is how "the power is off" is detected.

    local_ip, when given, pins the socket to that source address so the
    packet leaves through the physical interface on the board's subnet
    instead of whatever the routing table picks. Without it, a VPN or other
    virtual adapter holding a better-metric default route can take the
    packet -- measured 2026-09-18, this is what made a live board read as
    absent. See $PROD/docs/tables/DECISIONS.md decision 51.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            if local_ip:
                sk.bind((local_ip, 0))
            sk.settimeout(timeout)
            sk.connect((addr, int(udp_port)))
            sk.send(DISCOVERY)
            return sk.recv(2048).decode("ascii", errors="replace").strip()
    except Exception:
        return None


def wait_board_state(ip, udp_port, present, what, minutes, local_ip=None):
    """Wait for the board to go quiet (present=False) or come back (True).

    Requires three consecutive agreeing probes: a single missed datagram is
    normal on UDP and would otherwise read as a power cut.
    """
    deadline = time.monotonic() + minutes * 60
    streak = 0
    while time.monotonic() < deadline:
        reply = probe_board(ip, udp_port, local_ip=local_ip)
        if (reply is not None) == present:
            streak += 1
            if streak >= 3:
                Ok("  board is back (%s)" % reply) if present else Ok("  board has gone quiet")
                return True
        else:
            streak = 0
        time.sleep(0.7)
    Fail("  timed out after %d min waiting for: %s" % (minutes, what))
    return False


def enter_bootloader(ip, ports):
    """Run the Python enter_bootloader and return (captured text, exit code).

    Captured, not just printed: the bootloader's own counter report lands in
    this output rather than in the power-up log (see the cross-check below).
    """
    argv = [python_exe(), str(Path(__file__).resolve().parent / "enter_bootloader.py"),
            "--ip", ip]
    if ports:
        argv += ["--ports"] + list(ports)
    out, rc = run_capture(argv)
    print(out, end="" if out.endswith("\n") else "\n")
    return out, rc


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ip", default="")
    ap.add_argument("--count", type=int, default=8)
    ap.add_argument("--port", default="56865")
    # Generous by default: the thing being waited on is a person walking to a
    # board, and a timeout here throws away a phase 1 that was perfectly good.
    ap.add_argument("--wait-minutes", type=int, default=20)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--ports", nargs="*", default=None)
    args = ap.parse_args()

    ports = list(args.ports if args.ports is not None else cfg.LOG_PORTS)
    ip = args.ip or getattr(cfg, "BOARD_IP", "")
    # Resolved once, not per probe: this loop polls every 0.7s for up to
    # --wait-minutes, and netifquery is a `go run` (compiles), too slow to
    # call on every tick.
    local_ip = local_ip_for(ip) if ip else None
    if not ip:
        Fail("need --ip (or set BOARD_IP in config/machine.py)")
        return 2

    test_tool = get_go_bin("TestCase")
    if not test_tool.exists():
        Warn("TestCase not built, building it now")
        _, rc = run_capture(["go", "build", "-o",
                             "Output/%s/TestCase%s" % (GOOS_DIR, EXE), "./TestCase"],
                            cwd=cfg.TEST_REPO)
        if rc != 0 or not test_tool.exists():
            Fail("cannot build TestCase")
            return 2

    state_file = Path(get_scratch_file("au1_phase1.json"))
    if not args.resume and state_file.exists():
        state_file.unlink()

    # ------------------------------------------------------------ phase 1 ----
    Section("T1-17 phase 1 -- collecting nonces before the power cut")

    if args.resume and state_file.exists():
        before = json.loads(state_file.read_text(encoding="utf-8"))
        taken = datetime.datetime.fromisoformat(before["taken"].replace("Z", "+00:00"))
        now = datetime.datetime.now(taken.tzinfo)
        age_h = (now - taken).total_seconds() / 3600.0
        print("resuming with %d nonces taken %d min ago"
              % (len(before["samples"]), int(age_h * 60)))
        # Stale state would still produce a verdict, just a meaningless one: the
        # power cut it is compared against might not be the one that happened.
        if age_h > 1:
            Fail("that state is %dh old -- too old to trust. Re-run without --resume" % int(age_h))
            return 2
    else:
        if args.resume:
            Warn("--resume given but %s does not exist; collecting phase 1 now" % state_file)
        _, rc = enter_bootloader(ip, ports)
        if rc != 0:
            Fail("could not park the board in the bootloader")
            return 2
        rc = run_emit([test_tool, "T1-17", "--ip=%s" % ip, "--port=%s" % args.port,
                       "--state=%s" % state_file, "--phase=1", "--count=%d" % args.count])
        if rc != 0:
            Fail("phase 1 failed -- not sending you to the board for nothing")
            return 1
        before = json.loads(state_file.read_text(encoding="utf-8"))

    last_counter = before["samples"][-1]["counter"]

    # -------------------------------------------------------- power cycle ----
    banner(["UNPLUG THE BOARD NOW -- pull the power, do not press reset."])

    print("  Why not reset: a reset never touches the RTC backup domain, so it would")
    print("  pass even on a board with a dead VBAT cell -- and that board is exactly")
    print("  what this case exists to catch.")
    print()
    print("  This script watches the board's own UDP discovery go quiet, then")
    print("  cross-checks with the ST-Link's own voltage reading before trusting it --")
    print("  UDP silence alone does not prove the power went; a network hiccup looks")
    print("  identical from here. Measured 2026-09-18: it does not always.")
    print()
    print("  Waiting for the board to go quiet...")

    if not wait_board_state(ip, args.port, False, "the board to stop answering", args.wait_minutes, local_ip=local_ip):
        return 2

    # UDP silence alone does not distinguish "the power went" from "the network
    # hiccupped" -- both look like three missed probes from here. Cross-check
    # with an independent electrical signal, the way run_s4.py already does for
    # T1-21/T1-22, before this run gets to claim it tested anything.
    cli = get_programmer_cli()
    volts = target_voltage(cli) if cli else None
    if volts is None:
        Warn("  could not read the target voltage over SWD -- proceeding on UDP silence alone")
    elif volts > 0.5:
        Fail("  target still reads %.2fV -- the board did not lose power. "
             "That was a network hiccup, not a power cut; nothing was tested. Try again." % volts)
        return 2
    else:
        Ok("  target voltage during the cut: %.2fV -- confirmed off" % volts)

    banner(["PLUG THE BOARD BACK IN."])
    print("  Capturing the boot log while it comes up.")
    print()

    # Hold the log ports open across power-up so the bootloader's own counter
    # report is captured. That line is independent corroboration: it is read
    # straight out of the backup register, not out of a nonce.
    open_ports = open_log_ports(ports)
    if not wait_board_state(ip, args.port, True, "the board to answer again", args.wait_minutes, local_ip=local_ip):
        close_ports(open_ports)
        return 2
    boot_log = "".join(read_log_ports(open_ports, 3).values())

    if boot_log.strip():
        Section("boot log")
        print(boot_log)

    # ------------------------------------------------------------ phase 2 ----
    Section("T1-17 phase 2 -- collecting nonces after the power cut")

    eb_out, eb_rc = enter_bootloader(ip, ports)
    if eb_rc != 0:
        Fail("board came back but could not be parked in the bootloader")
        return 2

    verdict = run_emit([test_tool, "T1-17", "--ip=%s" % ip, "--port=%s" % args.port,
                        "--state=%s" % state_file, "--phase=2", "--count=%d" % args.count])

    # ------------------------------------------------- serial cross-check ----
    Section("cross-check against the bootloader's own report")

    # The bootloader prints the counter it read from the backup register
    # (IAPServer/iap_auth.c iap_auth_report_backup_domain). Comparing that to the
    # last nonce issued before the cut tests the same claim through a completely
    # different path: the register read directly, rather than a counter inferred
    # from the first four bytes of a nonce. If those two disagree, one of them is
    # not reading what it claims to.
    #
    # Both logs are searched because that line is NOT on the power-up boot log:
    # the report only runs when the bootloader stays in upload mode, and a board
    # with a valid application hands off before reaching it. It shows up in the
    # enter_bootloader output instead.
    m = re.search(r"nonce counter = (\d+)", boot_log + "\n" + eb_out)
    if not m:
        Warn("no 'nonce counter =' line in either log -- cross-check not available")
        Warn("(the verdict above still stands; it just has no second opinion)")
    else:
        reported = int(m.group(1))
        print("  last nonce issued before the cut : %s" % last_counter)
        print("  counter reported after power-up  : %d" % reported)
        if reported < last_counter:
            Fail("  the backup register came back BELOW where the nonces had reached -- it did not survive")
            verdict = 1
        else:
            Ok("  the backup register survived the power cut and did not go backwards")

    if "Backup domain was lost" in boot_log:
        Fail("  the board itself reports the backup domain was lost -- replay protection is weakened")
        verdict = 1
    elif "Backup domain retained" in boot_log:
        Ok("  board reports: Backup domain retained")

    Section("result")
    if verdict != 0:
        Fail("T1-17 FAILED")
        return 1
    Ok("T1-17 passed -- nonces are unique and the counter survived a real power cut")
    print("state kept at: %s" % state_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
