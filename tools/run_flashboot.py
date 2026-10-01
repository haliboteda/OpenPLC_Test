"""T1-29..T1-32 -- replacing the bootloader in place.

    python3 tools/run_flashboot.py --bin <boot.bin> --key <owner.pem>
    python3 tools/run_flashboot.py --bin <boot.bin> --key <owner.pem> --sign-with-leaf
    python3 tools/run_flashboot.py --unclaimed --bin <boot.bin> --key <any.pem>

WHAT EACH MODE PROVES

  default              T1-29 the board runs the new bootloader afterwards, and
                       T1-31 it still reports the same owner and generation
  --sign-with-leaf     T1-30 an image signed by a leaf is refused, and sector 0
                       is untouched -- the board still boots the old bootloader
  --unclaimed          T1-32 a board with no root refuses flashboot outright:
                       there is no root to check the image against

These flags state how the board has been SET UP; they do not set it up. For
--sign-with-leaf, pass a leaf key as --key. For --unclaimed, the board must
have no root (factory reset, or a bootloader just written over ST-Link).

⚠️ DESTRUCTIVE, AND NOT RECOVERABLE WITHOUT AN ST-LINK. A failure between the
erase and the last write leaves a board that does not boot. That is inherent
to replacing a bootloader with itself, not a defect in this script; see
$PROD/docs/modules/M1/FLASHBOOT.md.

Exit code is the verdict: 0 the case holds, 1 it does not, 2 setup missing.
"""

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (cfg, Section, Ok, Warn, Fail, close_ports,  # noqa: E402
                    get_iap_tool, get_programmer_cli, get_scratch_file,
                    open_log_ports, python_exe, run_capture,
                    run_while_draining, tcp_command)

HERE = Path(__file__).resolve().parent

# IAP_config.h builds this from OPENPLC_FW_VERSION. It is the reply to `info`,
# NOT a line the board prints while booting -- so the serial log alone cannot
# say which image is running, and asking over TCP is the only evidence there
# is. (2026-09-22: this script used to scan the boot log for it and therefore
# could never pass.)
BANNER_RE = re.compile(r"Boot Loader[ ]+([0-9][0-9A-Za-z._-]*)")
IAP_PORT = 56865
# owner.go's getowner output. Both fields have to be unchanged for T1-31.
OWNER_RE = re.compile(r"generation[ ]+([0-9]+)", re.I)
ROOT_RE = re.compile(r"([0-9a-fA-F]{128})")

TAIL_S = 8


def ask_info(ip, port, tries=12):
    """`info`, retried: the board answers only once lwIP is up."""
    reply = ""
    for _ in range(tries):
        reply = tcp_command(ip, port, "info")
        if BANNER_RE.search(reply):
            break
        time.sleep(1.0)
    return reply


def step_the_app_aside(ip, owner_key, ports, seconds):
    """Reboot a running application into the bootloader.

    A board with a valid application boots straight into it, and the
    application owns the port -- so `info` goes unanswered and the version is
    unreachable. T1-29 REQUIRES an application to be installed (it has to
    still start afterwards), so this is the normal case, not an edge one.

    Shells out to enter_bootloader.py rather than repeating the handshake:
    one implementation, one place for it to be wrong.
    """
    argv = [python_exe(), str(HERE / "enter_bootloader.py"), "--ip", ip,
            "--seconds", str(int(seconds))]
    if owner_key:
        argv += ["--key", str(owner_key)]
    if ports:
        argv += ["--ports"] + list(ports)
    Warn("  an application is holding the port; asking it to reboot")
    proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if proc.returncode != 0:
        # Its output is the only account of why, and swallowing it turns a
        # diagnosable failure into "the board did not answer".
        Warn("  enter_bootloader.py exited %d:" % proc.returncode)
        print(proc.stdout.decode("utf-8", errors="replace"))
        return False
    return True


def board_banner(ports, seconds, ip, port=IAP_PORT, reset=True, owner_key=None):
    """Reset over SWD, then ask the board which bootloader it is running.

    Returns (banner_reply, boot_log). The boot log is printed for the operator
    -- it is where ownership and the app verdict show up -- but the version
    itself only exists as the reply to `info`.

    reset=False leaves the board alone.
    """
    log = ""
    if reset:
        open_ports = open_log_ports(ports)
        rc, buf = run_while_draining([str(get_programmer_cli()), "-c", "port=SWD",
                                      "mode=UR", "-rst"],
                                     open_ports,
                                     get_scratch_file("flashboot_reset.out"),
                                     get_scratch_file("flashboot_reset.err"),
                                     tail_seconds=seconds)
        close_ports(open_ports)
        log = "\n".join(buf.values())

    # The board answers only once lwIP is up, which is a little after the
    # boot log goes quiet. Retry rather than time it.
    reply = ask_info(ip, port)
    if not BANNER_RE.search(reply):
        # Either the board is still coming up, or an application is running
        # and holding the port. Only the second is fixable from here.
        if step_the_app_aside(ip, owner_key, ports, seconds):
            reply = ask_info(ip, port)
    return reply, log


def owner_fingerprint(ip, key):
    """(generation, root pubkey) as the board reports them, or (None, None)."""
    out, rc = run_capture([get_iap_tool(), "getowner", ip, "--key=" + key])
    if rc != 0:
        Warn("  getowner exit %d" % rc)
        return None, None
    gen = OWNER_RE.search(out)
    root = ROOT_RE.search(out)
    return (gen.group(1) if gen else None), (root.group(1) if root else None)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bin", required=True, help="the bootloader image to install")
    ap.add_argument("--key", default="", help="owner root private key (PEM)")
    ap.add_argument("--ip", default="")
    ap.add_argument("--ports", nargs="*", default=None)
    ap.add_argument("--port", type=int, default=IAP_PORT,
                    help="the board TCP port `info` is asked on")
    ap.add_argument("--owner-key",
                    help="owner key used ONLY to reboot a running application "
                         "into the bootloader. Defaults to --key, which is "
                         "wrong for --sign-with-leaf -- pass it there.")
    ap.add_argument("--tail-seconds", type=int, default=TAIL_S)
    ap.add_argument("--sign-with-leaf", action="store_true",
                    help="T1-30: --key is a leaf, not the root; expect a refusal")
    ap.add_argument("--unclaimed", action="store_true",
                    help="T1-32: the board has no root; expect a refusal")
    args = ap.parse_args()

    image = Path(args.bin)
    if not image.exists():
        Fail("no such image: %s" % image)
        return 2
    size = image.stat().st_size
    if size > 128 * 1024:
        Fail("%d bytes will not fit sector 0 (128 KiB)" % size)
        return 1
    ip = args.ip or getattr(cfg, "BOARD_IP", "")
    if not ip:
        Fail("need --ip (or set BOARD_IP in config)")
        return 2
    if not args.key:
        Fail("need --key: the board checks a bootloader image against the owner root")
        return 2
    ports = list(args.ports if args.ports is not None else cfg.LOG_PORTS)
    # A board with no root has nothing to check an image against (R1-37).
    expect_refusal = args.sign_with_leaf or args.unclaimed

    Section("Before")
    owner_key = args.owner_key or (None if args.sign_with_leaf else args.key)
    before, before_log = board_banner(ports, args.tail_seconds, ip, args.port,
                                      owner_key=owner_key)
    print(before_log)
    old = BANNER_RE.search(before)
    if not old:
        Fail("the board did not answer `info` with a banner -- nothing to compare against")
        Warn("  it said: %r" % before)
        return 1
    Ok("  board runs Boot Loader %s" % old.group(1))

    gen_before = root_before = None
    if not args.unclaimed:
        gen_before, root_before = owner_fingerprint(ip, args.key)
        print("  owner: generation %s, root %s" % (gen_before, (root_before or "")[:16]))

    Section("flashboot")
    if expect_refusal:
        Warn("  this run expects the board to REFUSE; a success is the failure")
    cmd = [get_iap_tool(), "flashboot", str(image), ip, "--key=" + args.key]
    open_ports = open_log_ports(ports)
    rc, buf = run_while_draining(cmd, open_ports,
                                 get_scratch_file("flashboot.out"),
                                 get_scratch_file("flashboot.err"),
                                 tail_seconds=args.tail_seconds)
    close_ports(open_ports)
    # Both halves of the exchange: the board narrates on serial, but its wire
    # answer ("Refused", "Signature Failed") only reaches IAPTool's stdout --
    # and the wire answer is what the criteria are written against.
    during = "\n".join(buf.values())
    for stream in ("flashboot.out", "flashboot.err"):
        try:
            during += "\n" + Path(get_scratch_file(stream)).read_text(
                encoding="utf-8", errors="replace")
        except OSError:
            pass
    print(during)

    Section("After")
    after, after_log = board_banner(ports, args.tail_seconds, ip, args.port,
                                    owner_key=owner_key)
    print(after_log)
    new = BANNER_RE.search(after)
    fails = 0

    if expect_refusal:
        # The board has to still be the board: same banner, and it said why.
        if not new:
            Fail("  the board no longer boots -- a refused flashboot must not touch sector 0")
            return 1
        if new.group(1) != old.group(1):
            Fail("  bootloader changed from %s to %s after a refusal" % (old.group(1), new.group(1)))
            fails += 1
        else:
            Ok("  sector 0 untouched: still Boot Loader %s" % new.group(1))
        # Asked of the BOARD, not of the tool: the board narrates its own
        # refusal on serial, and "the result is asked of the board, never of
        # the tool" is the rule these cases are written to. On a board with no
        # root IAPTool stops before sending (getpubkey answers "none"), so the
        # board's own evidence there is the unchanged banner above.
        if args.unclaimed:
            said = "has no root"
        else:
            said = "Signature verification FAILED"
        if said.lower() in during.lower():
            Ok("  refusal reason seen: %r" % said)
        else:
            Fail("  no refusal reason; expected %r in the board log or IAPTool output" % said)
            fails += 1

        # Separately: did IAPTool pass that verdict on to the operator? A
        # refusal the tool reports as success is its own defect, and one a
        # customer would read as "the bootloader was replaced".
        tool_noticed = ("unexpected ack" in during.lower()
                        or "file send failed" in during.lower()
                        or "has no root" in during.lower())
        if tool_noticed:
            Ok("  IAPTool reported the refusal")
        else:
            Fail("  IAPTool did NOT report the refusal -- it printed "
                 "'File transfer complete.' and exited 0")
            Warn("     the board was right; the TOOL is wrong. IAP_Ether.go's")
            Warn("     sendFile() returns as soon as the last chunk is acked and")
            Warn("     never reads the verdict the board sends after verifying.")
            Warn("     Post-transfer refusals are therefore silent on both the")
            Warn("     `ether` and `flashboot` paths.")
            fails += 1
    else:
        if not new:
            Fail("  the board does not boot after the upgrade")
            return 1
        Ok("  board runs Boot Loader %s" % new.group(1))
        gen_after, root_after = owner_fingerprint(ip, args.key)
        if gen_after == gen_before and root_after == root_before and gen_after is not None:
            Ok("  ownership survived: generation %s, same root" % gen_after)
        else:
            Fail("  ownership changed: generation %s -> %s, root %s -> %s"
                 % (gen_before, gen_after, (root_before or "")[:16], (root_after or "")[:16]))
            fails += 1

    if fails:
        Fail("%d check(s) failed" % fails)
        return 1
    Ok("flashboot behaved as specified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
