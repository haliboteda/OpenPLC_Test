"""T2-10 -- after a root change, firmware signed by the OLD root cannot be installed.

    python tools/run_old_root_image_is_refused.py --bin <file.bin> --old-key <old.pem>
    python tools/run_old_root_image_is_refused.py --bin <file.bin> --app-bytes 0x40000

After setowner hands a board to a new root, images signed by the old root must
stop being installable. This is the case that says so.

Two claims, both checked:

  refused        the upload does not go through
  nothing moved  the application region is byte-identical before and after,
                 read back over SWD. The log is not asked about this: a
                 refusal that quietly erased the application would print the
                 same lines as one that did not.

⚠️ POSITIVE CONTROL, and it is not optional. "The board said no" cannot be told
from "the board was dead" without showing the same board accepting an image
signed by the root it does trust. So the run ends with a real upload using
--current-key, which also proves the flash comparison can see a change at all.
That means THIS SCRIPT INSTALLS AN APPLICATION -- the one passed as --bin.

⚠️ PRECONDITION -- the board must NOT trust the key passed as --old-key any
more, i.e. the root change has already happened and the rebuilt bootloader is
on the board. Checked by asking the board for its root over TCP and comparing.
Not met -> SETUP (exit 2), not FAIL.

⚠️ BLIND SPOT, stated because a green result would otherwise be read wider than
it is: IAPTool asks the board for its root and refuses before sending the
image, so what is usually exercised here is the TOOL's refusal, not the
BOARD's. The run prints which of the two said no. The board's own refusal of a
badly signed image is T1-11 / T1-12.

⚠️ The board must be in the bootloader for the precondition query (raw TCP
getpubkey). A board running an application does not answer; put it in the
bootloader with: python tools/enter_bootloader.py

RECOVERY -- nothing is written until the positive control, which installs
--bin. If that upload dies halfway, the board reports "App signature invalid or
absent" and stays in the bootloader; re-uploading fixes it:

      python tools/upload_and_watch.py --bin <file.bin>

Exit 0 = refused and nothing moved, 1 = not refused / something moved / the
control failed, 2 = setup, or flash could not be read.
"""

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from common import (Fail, Ok, Section, Warn, cfg, close_ports,  # noqa: E402
                    emit_file, get_iap_tool, get_programmer_cli,
                    get_scratch_dir, get_scratch_file, open_log_ports,
                    read_log_ports, run_capture, run_while_draining,
                    target_voltage, tcp_command, wait_for_board)

# The application region. $PROD/docs/modules/M1-firmware-upgrade.md, "地址布局".
APP_BASE = 0x08020000
APP_SIZE = 1792 * 1024

# Printed on every boot whatever the ownership state, so its absence means the
# capture failed rather than the board having nothing to say.
LOG_CAPTURE_PROOF = "Bootloader state:"

# The two shapes IAPTool's own refusal takes: a key that certifies itself, and
# a key carrying a certificate from some other root.
TOOL_REFUSED_KEY = "verifies against a different signing key"
TOOL_REFUSED_CERT = "was not issued by this board's root"


def working_copy(key):
    """Sign with a copy, never with the file in the repository.

    Issuing a self-signed certificate writes a serial counter next to the key,
    and neither the repository nor a rotation snapshot is a place to write.
    """
    dst = get_scratch_dir() / ("t2-10-old-root-" + key.name)
    shutil.copyfile(str(key), str(dst))
    return dst


def pubkey_of(iap, key):
    """The public half of a private key, as the 128 hex characters the board speaks."""
    out, _ = run_capture([iap, "pubkey", str(key)])
    m = re.search(r"\b([0-9a-fA-F]{128})\b", out)
    if not m:
        Fail("IAPTool pubkey did not return a key for %s:" % key)
        print(out.strip())
        return ""
    return m.group(1).lower()


def dump_app_region(cli, nbytes, tag):
    """Read the application region over SWD into a file. Returns the path, or None.

    A file rather than a hex dump on stdout: the comparison is over the whole
    region, and it is the only evidence that speaks about flash rather than
    about what the board chose to print.
    """
    path = get_scratch_file("t2-10-app-%s.bin" % tag)
    if path.exists():
        path.unlink()
    out = subprocess.run(
        [str(cli), "-c", "port=SWD", "mode=HOTPLUG",
         "-u", hex(APP_BASE), hex(nbytes), str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, errors="replace").stdout or ""
    if not path.exists() or path.stat().st_size != nbytes:
        Fail("could not read the application region over SWD (%s)" % tag)
        for line in out.splitlines():
            if re.search(r"Error|error", line):
                print("    %s" % line)
        return None
    return path


def digest(path):
    h = hashlib.sha256()
    with open(str(path), "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def first_difference(a, b):
    """Offset of the first differing byte, or None. Says where, not just whether."""
    with open(str(a), "rb") as fa, open(str(b), "rb") as fb:
        off = 0
        while True:
            ca, cb = fa.read(1 << 20), fb.read(1 << 20)
            if not ca and not cb:
                return None
            if ca != cb:
                for i in range(min(len(ca), len(cb))):
                    if ca[i] != cb[i]:
                        return off + i, ca[i], cb[i]
                return off + min(len(ca), len(cb)), -1, -1
            off += len(ca)


def upload(iap, image, ip, key, ports, tag, tail_seconds):
    """One real upload through the shipping tool. Returns (exit code, log text)."""
    Section("Upload with the %s key" % tag)
    open_ports = open_log_ports(ports)
    argv = [str(iap), "ether", str(image), ip, "--key=%s" % key]
    print("IAPTool %s" % " ".join(argv[1:]))
    rc, buf = run_while_draining(argv, open_ports,
                                 get_scratch_file("t2-10-%s.out" % tag),
                                 get_scratch_file("t2-10-%s.err" % tag),
                                 tail_seconds=tail_seconds)
    close_ports(open_ports)
    Section("IAPTool output (exit %d)" % rc)
    emit_file(get_scratch_file("t2-10-%s.out" % tag))
    emit_file(get_scratch_file("t2-10-%s.err" % tag))
    for name, text in buf.items():
        Section("%s  (%d bytes)" % (name, len(text)))
        if text:
            print(text)
    return rc, "\n".join(buf.values())


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--bin", required=True,
                    help="the image to offer; the positive control installs it")
    ap.add_argument("--ip", default="")
    ap.add_argument("--port", default="56865")
    ap.add_argument("--old-key", required=True,
                    help="the root the board trusted before setowner handed it over")
    ap.add_argument("--current-key", default="",
                    help="the root the board trusts now; used for the positive control")
    ap.add_argument("--app-bytes", default=hex(APP_SIZE),
                    help="how much of the application region to compare")
    ap.add_argument("--seconds", type=int, default=10)
    ap.add_argument("--tail-seconds", type=int, default=6)
    ap.add_argument("--ports", nargs="*", default=None)
    args = ap.parse_args()

    ports = args.ports if args.ports else cfg.LOG_PORTS
    ip = args.ip or getattr(cfg, "BOARD_IP", "")
    if not ip:
        Fail("need --ip (or set BOARD_IP in config/machine.py)")
        return 2
    image = Path(args.bin)
    if not image.exists():
        Fail("no such image: %s" % image)
        return 2
    nbytes = int(str(args.app_bytes), 0)
    if nbytes <= 0 or nbytes > APP_SIZE:
        Fail("--app-bytes must be between 1 and %d" % APP_SIZE)
        return 2

    cli = get_programmer_cli()
    iap = get_iap_tool()

    Section("Target check")
    volts = target_voltage(cli)
    if volts is None:
        Fail("SWD cannot reach the MCU, and the flash comparison is the case.")
        Warn("  0.00V or no target: board unpowered, or ST-Link VTREF not wired.")
        return 2
    print("target voltage: %.2f V" % volts)
    print("IAPTool:        %s" % iap)
    print("image:          %s  (%s B)" % (image, format(image.stat().st_size, ",d")))

    # ------------------------------------------------------------ preconditions
    Section("Boot log")
    open_ports = open_log_ports(ports)
    subprocess.run([str(cli), "-c", "port=SWD", "mode=UR", "-rst"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    buf = read_log_ports(open_ports, args.seconds)
    for name, text in buf.items():
        Section("%s  (%d bytes)" % (name, len(text)))
        if text:
            print(text)
    log = "\n".join(buf.values())

    Section("Precondition")
    if LOG_CAPTURE_PROOF not in log:
        Warn("SETUP - no boot log captured (%d bytes); the board's state is unknown." % len(log))
        Warn("  Check the log ports listed above.")
        return 2
    Ok("  boot log            captured")

    if not wait_for_board(ip, timeout=60.0):
        Warn("SETUP - %s never answered discovery." % ip)
        return 2
    board_pub = tcp_command(ip, args.port, "getpubkey").strip().lower()
    if not re.fullmatch(r"[0-9a-fA-F]{128}", board_pub):
        Warn("SETUP - the board did not answer getpubkey with a key: %s" % board_pub)
        Warn("  It is probably running its application. Put it in the bootloader:")
        Warn("    python tools/enter_bootloader.py")
        return 2
    print("  board root:         %s..." % board_pub[:32])

    old_key = Path(args.old_key)
    if not old_key.exists():
        Warn("SETUP - no such key: %s" % old_key)
        return 2
    print("  old root key:       %s" % old_key)
    old_key = working_copy(old_key)
    old_pub = pubkey_of(iap, old_key)
    if not old_pub:
        return 2
    if old_pub == board_pub:
        Warn("SETUP - this board still trusts that key, so there is no old root yet.")
        Warn("  Rotate the root and flash the rebuilt bootloader first, or pass the")
        Warn("  key the board used to trust as --old-key.")
        return 2
    Ok("  old root            is NOT what this board trusts")

    cur_key = Path(args.current_key) if args.current_key else iap.parent / "keys" / "fw_signing_key.pem"
    if not cur_key.exists():
        Warn("SETUP - no current root private key at %s." % cur_key)
        Warn("  Without it there is no positive control, and a refusal on its own")
        Warn("  cannot be told from a board that stopped answering.")
        return 2
    cur_pub = pubkey_of(iap, cur_key)
    if not cur_pub:
        return 2
    if cur_pub != board_pub:
        Warn("SETUP - --current-key is not the root this board trusts.")
        Warn("    board: %s..." % board_pub[:32])
        Warn("    key:   %s..." % cur_pub[:32])
        Warn("  The positive control has to be an image the board should accept.")
        return 2
    Ok("  current root        matches the board (%s)" % cur_key)

    # --------------------------------------------------------- flash, before
    Section("Application region before (%s bytes)" % format(nbytes, ",d"))
    before = dump_app_region(cli, nbytes, "before")
    if before is None:
        return 2
    d_before = digest(before)
    Ok("  sha256 %s" % d_before)

    # ------------------------------------------------ the case: old root image
    rc_old, _ = upload(iap, image, ip, old_key, ports, "old-root", args.tail_seconds)

    Section("Application region after the refused upload")
    after = dump_app_region(cli, nbytes, "after")
    if after is None:
        return 2
    d_after = digest(after)
    Ok("  sha256 %s" % d_after)

    # ------------------------------------------------------- positive control
    if not wait_for_board(ip, timeout=60.0):
        Warn("the board stopped answering discovery after the refused upload")
    rc_cur, _ = upload(iap, image, ip, cur_key, ports, "current-root", args.tail_seconds)

    Section("Application region after the control upload")
    control = dump_app_region(cli, nbytes, "control")
    if control is None:
        return 2
    d_control = digest(control)
    Ok("  sha256 %s" % d_control)

    # ------------------------------------------------------------------ verdict
    Section("T2-10 verdict")
    checks = []

    if rc_old != 0:
        Ok("  old-root upload     refused (IAPTool exit %d)" % rc_old)
        checks.append(True)
    else:
        Fail("  old-root upload     WENT THROUGH - the old root can still install firmware")
        checks.append(False)

    if d_after == d_before:
        Ok("  application region  unchanged, %s bytes compared" % format(nbytes, ",d"))
        checks.append(True)
    else:
        diff = first_difference(before, after)
        Fail("  application region  CHANGED during a refused upload")
        if diff:
            Fail("    first difference at +0x%X: 0x%02X -> 0x%02X" % diff)
        checks.append(False)

    if rc_cur == 0:
        Ok("  control upload      accepted (IAPTool exit 0)")
        checks.append(True)
    else:
        Fail("  control upload      FAILED (IAPTool exit %d)" % rc_cur)
        Fail("    Without it the refusal above proves nothing: a board that refuses")
        Fail("    everything and a board that is dead look the same.")
        checks.append(False)

    if d_control != d_before:
        Ok("  control changed the application region, so the comparison can see a write")
        checks.append(True)
    else:
        Fail("  the control left the application region identical")
        Fail("    Either it did not install, or --bin is byte-identical to what was")
        Fail("    already on the board. Pass an image different from the installed one.")
        checks.append(False)

    print("")
    print("  Which layer refused the old-root image:")
    out_old = get_scratch_file("t2-10-old-root.out")
    text_old = ""
    try:
        text_old = Path(out_old).read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    if TOOL_REFUSED_KEY in text_old or TOOL_REFUSED_CERT in text_old:
        print("    IAPTool, after asking the board for its root and before sending")
        print("    the image. The board itself never saw it -- see the blind spot in")
        print("    this file's header. T1-11 / T1-12 are where the board refuses.")
    elif rc_old != 0:
        print("    not IAPTool's key check - read its output above to see what said no")
    else:
        print("    nothing refused it")

    print("")
    if all(checks):
        Ok("PASS - the old root cannot install firmware, and the application region")
        Ok("       did not move. The same board accepted an image signed by the root")
        Ok("       it does trust, so the refusal was a decision, not a dead board.")
        return 0
    Fail("FAIL - read the four checks above.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
