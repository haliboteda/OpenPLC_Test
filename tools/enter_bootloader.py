"""Leave the board sitting in the bootloader, which T1-06-T1-10 and T1-11 all require.

    python tools/enter_bootloader.py

How: hand IAPTool an image larger than the application region. IAPTool reboots
the running application into the bootloader as its first step, and only then
offers the image -- which the board refuses at the size check, before erasing or
staging anything. So the board ends up in the bootloader with nothing written.

⚠ A CLAIMED board needs its owner key: the reboot request is signed, and one
signed by any other key is rejected. Without --key IAPTool uses this computer's
default key (decision 72), so pass --key when that is not the owner.

Every step here is shipping code: IAPTool's real authenticated reboot, and the
board's real size check (IAPServer/IAP_server.c:206). Nothing about the protocol
is reimplemented, which is the whole reason to do it this way rather than poking
the SRAM4 handoff record over SWD.

The alternative is holding BOOT0 through the startup window, which needs hands on
the board.

Proof it worked: the board answers discovery as the bootloader. The bootloader's
own lines (the size refusal) reach USB port 1 only once the app runs again, so
they are not read here.

"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from common import (board_ip, board_role, Fail, Ok, Section, close_ports,  # noqa: E402
                    get_iap_tool, get_scratch_file, open_board_log,
                    run_while_draining)

# Anything over IAP_APP_MAX_SIZE (1,835,008) does; the content is never read.
OVERSIZE_BYTES = 2000000


def oversize_image():
    path = get_scratch_file("testtool_oversize.bin")
    if not path.exists() or path.stat().st_size < OVERSIZE_BYTES:
        with open(str(path), "wb") as fh:
            fh.truncate(OVERSIZE_BYTES)
    return path


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--ip", default=None)
    ap.add_argument("--seconds", type=int, default=6,
                    help="how long to wait for the board to answer as the bootloader")
    ap.add_argument("--key", default=None,
                    help="owner private key (PEM). The reboot request is signed, and a claimed "
                         "board rejects one signed by any other key. Omit it to use this "
                         "computer's default key.")
    args = ap.parse_args()

    ip = args.ip or board_ip()
    if not ip:
        Fail("need --ip (or BOARD_UID in config/machine.py)")
        return 1

    big = oversize_image()

    Section("Requesting bootloader")
    # The app's refusal of an unsigned reboot is a system line on USB port 1.
    open_ports = open_board_log(sys_only=True)
    out_file = get_scratch_file("eb.out")
    err_file = get_scratch_file("eb.err")
    cmd = [str(get_iap_tool()), "ether", str(big), ip]
    if args.key:
        cmd += ["--key", args.key]
    _, buf = run_while_draining(cmd, open_ports, out_file, err_file)
    close_ports(open_ports)

    allof = "\n".join(buf.values())
    for k, v in buf.items():
        if v:
            Section("board log %s" % k)
            print(v)

    Section("Verdict")
    deadline = time.monotonic() + max(args.seconds, 2)
    role = board_role(ip)
    while not role.startswith("BOOTLD") and time.monotonic() < deadline:
        time.sleep(0.5)
        role = board_role(ip)
    if role.startswith("BOOTLD"):
        Ok("board is in the bootloader (discovery role %s)" % role)
        return 0
    # The board says why, and this script already has its log. Not reading it
    # was the whole problem: "does not appear to be in the bootloader" sent
    # people looking at cabling when the board had answered in plain words.
    if "[sys] Rejected unauthenticated" in allof:
        Fail("the board refused the reboot request: it is CLAIMED, and this "
             "request was signed by a key it does not trust")
        if args.key:
            print("  --key was passed (%s) but the board still refused it." % args.key)
            print("  That key is not the one this board trusts - check with:")
        else:
            print("  Pass the owner key:  --key <owner.pem>")
            print("  Which key this board trusts:")
        print("    IAPTool getowner %s      (needs the board in the bootloader)" % ip)
        print("  Or hold BOOT0 through startup, which needs nobody's signature.")
        return 1

    Fail("board does not appear to be in the bootloader")
    try:
        lines = err_file.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in lines[-5:]:
            print(line)
    except OSError:
        pass
    return 1


if __name__ == "__main__":
    sys.exit(main())
