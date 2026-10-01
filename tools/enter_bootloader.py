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

"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from common import (Fail, Ok, Section, Warn, cfg, decode_serial,  # noqa: E402
                    get_iap_tool, get_scratch_file, open_log_ports,
                    read_log_ports)

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
    ap.add_argument("--seconds", type=int, default=6)
    ap.add_argument("--ports", nargs="*", default=None)
    ap.add_argument("--key", default=None,
                    help="owner private key (PEM). The reboot request is signed, and a claimed "
                         "board rejects one signed by any other key. Omit it to use this "
                         "computer's default key.")
    args = ap.parse_args()

    ports = args.ports if args.ports else cfg.LOG_PORTS
    ip = args.ip or getattr(cfg, "BOARD_IP", "")
    if not ip:
        Fail("need --ip (or set BOARD_IP in config)")
        return 1

    big = oversize_image()

    Section("Requesting bootloader")
    open_ports = open_log_ports(ports)

    out_file = get_scratch_file("eb.out")
    err_file = get_scratch_file("eb.err")
    with open(str(out_file), "wb") as so, open(str(err_file), "wb") as se:
        cmd = [str(get_iap_tool()), "ether", str(big), ip]
        if args.key:
            cmd += ["--key", args.key]
        proc = subprocess.Popen(cmd, stdout=so, stderr=se)

        # Drain while IAPTool runs: the driver's buffer overruns on a long
        # transfer and the interesting lines are the ones lost.
        buf = {k: "" for k in open_ports}
        while proc.poll() is None:
            for k, h in open_ports.items():
                try:
                    n = h.in_waiting
                    if n:
                        buf[k] += decode_serial(h.read(n))
                except Exception:
                    pass
            time.sleep(0.06)

    tail = read_log_ports(open_ports, args.seconds)
    for k, v in tail.items():
        buf[k] = buf.get(k, "") + v

    allof = "\n".join(buf.values())
    for k, v in buf.items():
        if v:
            Section("serial %s" % k)
            print(v)

    Section("Verdict")
    # The refusal line is the proof that nothing was written, not just that the
    # upload failed somewhere.
    if "Invalid flash size" in allof:
        Ok("board is in the bootloader; the oversized image was refused before "
           "anything was touched")
        return 0
    if "UPLOAD Mod" in allof:
        Warn("board entered upload mode, but the refusal line was not seen - check the log")
        return 0
    # The board says why, and this script already has its log. Not reading it
    # was the whole problem: "does not appear to be in the bootloader" sent
    # people looking at cabling when the board had answered in plain words.
    if "Rejected unauthenticated" in allof:
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
