"""Asking for a CDC upload must not bring the ethernet stack up.

Case T1-25, requirement R1-05. Core/Src/main.c calls MX_LWIP_Init() only for IAP_ETHERNET and
IAP_ALL, so in CDC mode lwIP is never initialised and the board cannot answer
anything on the network. Observable from the PC: broadcast UDP discovery at a
board sitting in CDC upload mode and get silence.

Silence on its own proves nothing -- an unplugged or dead board is also silent.
So the run has two halves and BOTH must hold:

    CDC mode      broadcast discovery -> no reply       (the requirement)
    ethernet mode broadcast discovery -> at least one   (the control)

Without the control this case would pass on a board that is simply off.

    python3 tools/run_cdc_does_not_start_ethernet.py --cdc COM6
    python3 tools/run_cdc_does_not_start_ethernet.py --cdc COM6 --ip 192.168.0.3

Exit code is the verdict: 0 both halves held, 1 either did not.
"""

import argparse
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (cfg, Section, Ok, Warn, Fail, get_iap_tool,  # noqa: E402
                    get_scratch_file, local_ip_for, open_log_ports,
                    read_log_ports, close_ports)

# IAP_config.h: #define OPENPLC_SERVER_PORT 56865
DISCOVERY_PORT = 56865

# The board answers this one at IAPServer/IAP_server.c; the other keywords go
# through the same path, so one is enough to tell "stack up" from "stack down".
KEYWORD = b"openplc_server_where_r_y"

# Anything over IAP_APP_MAX_SIZE (1,835,008). The board refuses it at the size
# check, which leaves it in the bootloader in the mode we asked for without
# writing anything.
OVERSIZE_BYTES = 2000000

SETTLE_S = 4.0
LISTEN_S = 3.0


def oversize_image():
    path = get_scratch_file("cdc_ether_oversize.bin")
    if not path.exists() or path.stat().st_size < OVERSIZE_BYTES:
        with open(str(path), "wb") as fh:
            fh.truncate(OVERSIZE_BYTES)
    return path


def probe_discovery(ip, seconds=LISTEN_S):
    """Ask the board for a discovery reply and count what comes back.

    Unicast to the board, which is what TestCase's own UDP cases do
    (udp_discovery.go dials the board directly). A broadcast does not reach
    the board's subnet on a multi-homed host, so it reports silence whether
    or not the ethernet stack is up -- useless for a case whose whole
    question is "is it up".
    """
    # Pinned to the physical interface on the board's subnet. Without it a
    # VPN or other virtual adapter holding a better-metric default route can
    # take the packet -- measured 2026-09-18, this is what made a live board
    # read as absent. See $PROD/docs/tables/DECISIONS.md decision 51. Only
    # called twice per run (once per mode), so resolving it here rather than
    # caching is not worth the complexity.
    local_ip = local_ip_for(ip)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if local_ip:
        s.bind((local_ip, 0))
    s.settimeout(0.5)
    replies = []
    try:
        s.sendto(KEYWORD, (ip, DISCOVERY_PORT))
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                data, addr = s.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError:
                break
            replies.append((addr[0], data.decode("utf-8", "replace").strip()))
    finally:
        s.close()
    return replies


def request_mode(argv, label, ports=None):
    """Ask the board for one upload mode with an image it will refuse.

    Returns whatever the board printed while doing it. server_decide() names
    the mode it picked and why, which is the only way to tell "CDC isolation
    held" from "the board never entered CDC mode".
    """
    Section(label)
    out = get_scratch_file("cdc_ether.out")
    err = get_scratch_file("cdc_ether.err")
    open_ports = open_log_ports(ports) if ports else {}
    with open(str(out), "wb") as so, open(str(err), "wb") as se:
        proc = subprocess.Popen(argv, stdout=so, stderr=se)
        proc.wait()
    print("  %s -> exit %d" % (" ".join(str(a) for a in argv), proc.returncode))
    time.sleep(SETTLE_S)
    log = ""
    if open_ports:
        buf = read_log_ports(open_ports, 1)
        close_ports(open_ports)
        log = "\n".join(buf.values())
    return log


def verdict(cdc_replies, ether_replies, cdc_log=""):
    """Judge both halves. Returns (failures, unknowns).

    A silent control is a failure, not an unknown: it means the run established
    nothing, and reporting that as a pass is exactly the trap this case has to
    avoid.

    The precondition is judged first. IAP_ALL starts lwIP by design, so a board
    that entered upload mode for any other reason answers discovery correctly
    -- calling that a failure would report a defect that is not there.
    """
    Section("Verdict")
    fails = unknowns = 0
    in_cdc_mode = True

    if cdc_log:
        if "CDC upload requested" in cdc_log:
            Ok("  precondition: board entered CDC upload mode")
        elif "UPLOAD Mod" in cdc_log:
            line = [l for l in cdc_log.splitlines() if "UPLOAD Mod" in l]
            Warn("  precondition: board entered upload mode for another reason "
                 "-- IAP_ALL starts lwIP by design, so nothing here judges CDC isolation")
            if line:
                print("      %s" % line[-1].strip())
            unknowns += 1
            in_cdc_mode = False
        else:
            Warn("  precondition: no mode line captured -- cannot tell which mode the board took")
            unknowns += 1
            in_cdc_mode = False
    else:
        # No log means the precondition is unverified, not satisfied. Judging the
        # CDC half without it turns "the board never entered CDC mode" into a
        # reported defect.
        Warn("  precondition: no serial log captured -- cannot confirm the board entered CDC mode")
        unknowns += 1
        in_cdc_mode = False

    if ether_replies:
        Ok("  control: board answered before the CDC request (%d reply/replies)"
           % len(ether_replies))
        for ip, text in ether_replies:
            print("      %s  %s" % (ip, text))
    else:
        Fail("  control: board answered nothing even before the CDC request -- "
             "this run proves nothing about CDC mode")
        fails += 1

    if cdc_replies and not in_cdc_mode:
        Warn("  board answered discovery (%d reply/replies), but it was not in CDC mode "
             "-- expected, and not a defect" % len(cdc_replies))
    elif cdc_replies:
        Fail("  board answered discovery while in CDC upload mode (%d reply/replies) -- "
             "the ethernet stack came up when it should not have" % len(cdc_replies))
        for ip, text in cdc_replies:
            print("      %s  %s" % (ip, text))
        fails += 1
    elif ether_replies:
        Ok("  silent in CDC mode, and the control shows that silence means something")
    else:
        Warn("  silent in CDC mode, but with no working control that says nothing")
        unknowns += 1

    return fails, unknowns


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cdc", default="", help="the board's CDC COM port")
    ap.add_argument("--ip", default="")
    ap.add_argument("--listen-seconds", type=float, default=LISTEN_S)
    ap.add_argument("--ports", nargs="*", default=None,
                    help="serial log ports; defaults to config LOG_PORTS")
    args = ap.parse_args()

    cdc = args.cdc or getattr(cfg, "CDC_PORT", "")
    if not cdc:
        Fail("need --cdc <COM port> (CDC_PORT is empty in config)")
        return 1
    ip = args.ip or getattr(cfg, "BOARD_IP", "")
    if not ip:
        Fail("need --ip (or set BOARD_IP in config)")
        return 1

    if args.ports is None:
        args.ports = list(cfg.LOG_PORTS)
    tool = get_iap_tool()
    big = oversize_image()

    # The control has to run first, from whatever mode the board is already in.
    # Once the CDC half has run the board sits in CDC mode with lwIP down, so
    # nothing can reach it over ethernet to establish a control afterwards.
    Section("Probe discovery before the CDC request (control)")
    ether_replies = probe_discovery(ip, args.listen_seconds)
    print("  %d reply/replies" % len(ether_replies))

    cdc_log = request_mode([str(tool), "cdc", str(big), cdc], "Ask for CDC upload mode",
                           ports=args.ports)
    Section("Probe discovery while the board is in CDC mode")
    cdc_replies = probe_discovery(ip, args.listen_seconds)
    print("  %d reply/replies" % len(cdc_replies))

    fails, unknowns = verdict(cdc_replies, ether_replies, cdc_log)
    if fails:
        Fail("%d check(s) failed" % fails)
        return 1
    # A check with no evidence is not a pass. Until 2026-09-18 this only warned
    # and then returned 0, so a run that could not even confirm the board had
    # entered CDC mode still reported "CDC mode leaves the ethernet stack down"
    # -- measured that day by passing the wrong --cdc port.
    if unknowns:
        Warn("  %d check(s) had no evidence either way" % unknowns)
        Fail("nothing was proven this run; fix the setup and try again")
        return 2
    Ok("CDC mode leaves the ethernet stack down")
    return 0


if __name__ == "__main__":
    sys.exit(main())
