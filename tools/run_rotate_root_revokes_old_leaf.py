"""T2-12 / T2-13 / T2-14 -- rotating the root revokes every leaf the old root issued.

    python tools/run_rotate_root_revokes_old_leaf.py --bin <app.bin> --current-key <owner.pem>
    python tools/run_rotate_root_revokes_old_leaf.py ... --new-root-key <next_owner.pem>
                                                 hand over to a key you keep, not a throwaway
    python tools/run_rotate_root_revokes_old_leaf.py ... --stop-after T2-12
                                                 leaves the board WITHOUT a runnable app
    python tools/run_rotate_root_revokes_old_leaf.py ... --compare-bytes 0x40000
                                                 read back less flash, for a faster run

Revocation today has exactly one mechanism: hand the board to a new root with
`setowner`. Every leaf certificate the old root issued dies with it -- including
the one that signed the application already sitting in flash. These three cases
are the three halves of that sentence, so they are one script: the second cannot
start until the first has rotated the root, and the state they share (which key
the board trusts, which leaf signed the installed app) is not something an
operator should have to carry between three invocations by hand.

  T2-12  after the rotation the board refuses to start the installed app
  T2-13  after the rotation the old leaf cannot upload either, and the
         application region is not touched by the attempt
  T2-14  a leaf issued by the NEW root uploads and boots

T2-14 is not optional. Without it "the board refused the old leaf" and "this
board refuses everybody" are the same observation, and only one of them is the
product working. It is also the step that leaves the board usable again, so it
runs even when T2-12 or T2-13 failed.

Requirement: R2-02 (a board can leave the factory root behind without an
ST-Link). The criteria and the path they belong to are in
$PROD/maps/five-paths-e2e-test/issues/E2E-02-what-does-security-mean-per-path.md
(path 5, rows c/d/e) and $PROD/docs/modules/M2-ownership.md section 4.

⚠️ PRECONDITION, established by this script but NOT created out of nothing:

  the board must already be CLAIMED, and --current-key must be the private half
  of the root it currently trusts.

Claiming needs somebody holding BOOT0 and is its own case -- run
`python tools/run_takeown.py` first. Given a claimed board, this script builds
the rest of the precondition itself: it issues a leaf certificate from the
current root, uploads the application signed by that leaf, and checks the board
booted it. If any of that does not happen the run reports SETUP, not FAIL,
because nothing about revocation was tested.

⚠️ NOBODY IS NEEDED AT THE BOARD for this run. Resets go over ST-Link, and the
handover is authorised by the current owner's signature rather than by a button.

⚠️ THIS CHANGES WHO OWNS THE BOARD, and that is the point. When the run
finishes, the root is the new one and the old owner key has no power over this
board any more. Pass --new-root-key so the incoming root is a key you keep; the
generated one lives in the key directory printed at the start of the run, which
is a temp directory.

RECOVERY
  Run finished normally
      The board is owned by the new root and runs an app signed by the new leaf.
      Keep the key directory: without the new root's private key nobody can hand
      this board on or issue another leaf for it.
  Run stopped between T2-12 and T2-14
      The board sits in the bootloader with no runnable application. Upload one
      signed by a leaf of the NEW root:
          IAPTool ether <app.bin> <ip> --key=<new_leaf.pem> --cert=<new_leaf.pem.cert>
      Both files are in the key directory printed at the start of the run.
  Back to the original owner
      Hold BOOT0 for ten seconds (factory reset, case T2-05), or reflash over
      ST-Link with `python tools/flash_bootloader.py`, then claim again with
      `python tools/run_takeown.py`.

⚠️ WHAT THIS RUN DOES NOT PROVE. In T2-13 the refusal comes from IAPTool's
pre-flight check (auth.go, verifyIdentityMatchesDevice): the tool asks the board
for its root, sees the certificate was not issued by it, and stops before
sending a byte. So T2-13 shows the customer-facing path refuses, and shows the
application region survives an attempt -- it does NOT exercise the bootloader's
own certificate check. That one is covered by T1-12 (a wrongly signed image on a
real board) and T1-16 (the bootloader's decision logic compiled and run on the
host).

Exit 0 = every criterion passed, 1 = a criterion failed, 2 = precondition not
met, or the evidence needed to judge was never captured.
"""

import argparse
import hashlib
import re
import subprocess
import sys
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from common import (Fail, Ok, Section, Warn, assert_target_reachable,  # noqa: E402
                    cfg, get_go_bin, get_iap_tool, get_programmer_cli,
                    get_scratch_dir, get_scratch_file, nonblank_lines,
                    open_log_ports, python_exe, read_log_ports, run_capture,
                    run_while_draining, tcp_command, wait_for_board)

# Core/Inc/usbd_cdc_flash.h: IAP_APP_ADDRESS, and IAP_APP_MAX_SIZE as the gap up
# to the journal sector. Read back over SWD, which is the only view of the
# application region that does not go through the code under test.
APP_BASE = 0x08020000
APP_MAX_SIZE = 0x1C0000

# Larger than IAP_APP_MAX_SIZE, so the board refuses it at the size check before
# erasing or staging anything. The same trick tools/enter_bootloader.py uses to
# move a running application into the bootloader without touching BOOT0.
OVERSIZE_BYTES = 2000000

# Printed on every boot in every ownership state, before the app/bootloader
# decision (IAP_server.c, bootloader_state_init). Its absence means the capture
# failed, not that the board had nothing to say -- and an empty capture must
# never be read as a pass.
LOG_CAPTURE_PROOF = "Bootloader state:"
LOG_APP_REJECTED = "App signature invalid or absent"
LOG_APP_STARTED = "** APP Mod"
LOG_IMAGE_ACCEPTED = "Checksum and signature OK"

# IAPTool's own words about the certificate it presented. The first one is what
# separates "the app is signed by a delegated leaf" from "the app is signed by
# the root itself" -- without it the precondition is not the one these cases need.
TOOL_CERT_ACCEPTED = "Certificate was issued by this board's root"
TOOL_CERT_REFUSED = "was not issued by this board's root"

PASS, FAIL, SETUP = "PASS", "FAIL", "SETUP"


# ----------------------------------------------------------------- helpers
def hex_token(text, nchars):
    """The first run of exactly `nchars` hex characters, lowercased, or ""."""
    m = re.search(r"(?<![0-9a-fA-F])([0-9a-fA-F]{%d})(?![0-9a-fA-F])" % nchars, text)
    return m.group(1).lower() if m else ""


def iap_genkey(iap, keydir, name):
    """Run `IAPTool genkey` in keydir. Returns the .pem path, or None."""
    out, rc = run_capture([iap, "genkey", name], cwd=keydir)
    pem = Path(keydir) / (name + ".pem")
    if rc != 0 or not pem.exists():
        Fail("genkey %s failed:" % name)
        for line in nonblank_lines(out)[-6:]:
            print("    " + line)
        return None
    return pem


def iap_pubkey(iap, pem):
    """The public half of a private key, as the 128 hex characters the board speaks."""
    out, rc = run_capture([iap, "pubkey", str(pem)])
    pub = hex_token(out, 128)
    if rc != 0 or not pub:
        Fail("could not read the public key of %s:" % pem)
        for line in nonblank_lines(out)[-6:]:
            print("    " + line)
        return ""
    return pub


def iap_issue_cert(iap, root_pem, leaf_pub, cert_path):
    """Issue a leaf certificate for leaf_pub, signed by root_pem. Writes cert_path."""
    out, rc = run_capture([iap, "cert", leaf_pub, "--key=%s" % root_pem])
    cert = hex_token(out, 256)
    if rc != 0 or not cert:
        Fail("could not issue a certificate from %s:" % root_pem)
        for line in nonblank_lines(out)[-6:]:
            print("    " + line)
        return False
    Path(cert_path).write_text(cert + "\n", encoding="ascii")
    return True


def app_digest(cli, nbytes, tag):
    """SHA-256 of the application region, read back over SWD. None when unreadable.

    A digest rather than a word-by-word dump: the claim under test is "not one
    byte moved", and comparing two digests of the same span answers exactly that
    without printing half a megabyte. mode=HOTPLUG so reading does not reset a
    board that may be running the application.
    """
    out_path = Path(get_scratch_file("t2_app_%s.bin" % tag))
    try:
        out_path.unlink()
    except OSError:
        pass
    out, rc = run_capture([cli, "-c", "port=SWD", "mode=HOTPLUG",
                           "-u", hex(APP_BASE), hex(nbytes), str(out_path)])
    if rc != 0 or not out_path.exists():
        Fail("could not read the application region over SWD:")
        for line in nonblank_lines(out)[-6:]:
            print("    " + line)
        return None
    data = out_path.read_bytes()
    if len(data) != nbytes:
        Warn("  read back %d bytes, asked for %d" % (len(data), nbytes))
    return hashlib.sha256(data).hexdigest()


def reset_and_capture(cli, ports, seconds):
    """Reset over ST-Link with the log ports already open, and return what came out."""
    open_ports = open_log_ports(ports)
    run_capture([cli, "-c", "port=SWD", "mode=UR", "-rst"])
    text = "\n".join(read_log_ports(open_ports, seconds).values())
    for line in nonblank_lines(text):
        print("    | " + line)
    return text


def upload(iap, image, ip, key, cert, ports, tail_seconds):
    """One upload through the shipping tool. Returns (exit code, tool text, board text).

    ⚠️ The board is still working when IAPTool exits -- it is verifying the
    staged image, erasing and copying. Do not reset here; keep listening instead,
    which is what catches the board rebooting into the application.
    """
    out_path = get_scratch_file("t2_rot.out")
    err_path = get_scratch_file("t2_rot.err")
    open_ports = open_log_ports(ports)
    argv = [iap, "ether", str(image), ip, "--key=%s" % key, "--cert=%s" % cert]
    print("  IAPTool ether %s %s --key=%s --cert=%s"
          % (Path(image).name, ip, Path(key).name, Path(cert).name))
    rc, buf = run_while_draining(argv, open_ports, out_path, err_path)
    tail = read_log_ports(open_ports, tail_seconds)      # also closes them
    for k in buf:
        buf[k] += tail.get(k, "")
    tool = (Path(out_path).read_text(encoding="utf-8", errors="replace")
            + Path(err_path).read_text(encoding="utf-8", errors="replace"))
    for line in nonblank_lines(tool):
        print("    T | " + line)
    serial = "\n".join(buf.values())
    for line in nonblank_lines(serial):
        print("    B | " + line)
    return rc, tool, serial


def move_to_bootloader(iap, ip, port, key, cert, ports):
    """Get a board that is running the application back into the bootloader.

    Offers an image larger than the application region: IAPTool performs its real
    authenticated reboot first, and the board then refuses the image at the size
    check, before erasing or staging anything. Everything here is shipping code,
    which is why this is preferred over poking the handoff record over SWD.

    Needs an identity the board still trusts, so it only works BEFORE the root is
    rotated.
    """
    big = Path(get_scratch_file("t2_rot_oversize.bin"))
    if not big.exists() or big.stat().st_size < OVERSIZE_BYTES:
        with open(str(big), "wb") as fh:
            fh.truncate(OVERSIZE_BYTES)
    upload(iap, big, ip, key, cert, ports, 4)
    # The refusal is the expected outcome here, so IAPTool's exit code says
    # nothing useful. Ask the board which mode it is in instead: getpubkey is a
    # bootloader command, so an answer to it IS the answer.
    if not wait_for_board(ip, timeout=30.0, port=port):
        return False
    return re.fullmatch(r"[0-9a-fA-F]{128}",
                        tcp_command(ip, port, "getpubkey")) is not None


# ------------------------------------------------------------------ setup
def build_precondition(st, args):
    """Claimed board + an application signed by a leaf of the CURRENT root.

    Everything that goes wrong in here is a SETUP result: none of it is what the
    three cases are about, and a run that never reached the starting line has not
    disproved anything.
    """
    Section("Precondition 1/4  is the board claimed, and is --current-key its root?")
    if not wait_for_board(st["ip"], timeout=60.0, port=args.port):
        Fail("the board did not answer UDP discovery at %s" % st["ip"])
        return False
    gen = tcp_command(st["ip"], args.port, "getowner")
    root = tcp_command(st["ip"], args.port, "getpubkey").lower()
    print("  generation: %s" % gen)
    print("  root:       %s" % root)
    if not re.fullmatch(r"\d+", gen):
        Fail("getowner did not answer with a number: %s" % gen)
        Fail("  is the board sitting in the bootloader?")
        return False
    if int(gen) == 0:
        Fail("this board is unclaimed, and setowner needs an existing owner.")
        Fail("  The board answers: setowner refused: board is unclaimed - use takeown")
        Fail("  Claim it first: python tools/run_takeown.py")
        return False
    if not re.fullmatch(r"[0-9a-fA-F]{128}", root):
        Fail("the board did not answer getpubkey with a key: %s" % root)
        return False

    current_pub = iap_pubkey(st["iap"], args.current_key)
    if not current_pub:
        return False
    if current_pub != root:
        Fail("--current-key is not the key this board trusts.")
        Fail("  board trusts: %s" % root)
        Fail("  %s is:        %s" % (Path(args.current_key).name, current_pub))
        return False
    Ok("  claimed at generation %s, and --current-key is its root" % gen)

    Section("Precondition 2/4  issue a leaf certificate from the CURRENT root")
    old_leaf = iap_genkey(st["iap"], st["keydir"], "old_leaf")
    if old_leaf is None:
        return False
    old_leaf_pub = iap_pubkey(st["iap"], old_leaf)
    if not old_leaf_pub:
        return False
    old_cert = Path(str(old_leaf) + ".cert")
    if not iap_issue_cert(st["iap"], args.current_key, old_leaf_pub, old_cert):
        return False
    st["old_leaf"], st["old_cert"] = old_leaf, old_cert
    Ok("  leaf key %s, certificate %s" % (old_leaf.name, old_cert.name))

    Section("Precondition 3/4  upload the application signed by that leaf")
    rc, tool, serial = upload(st["iap"], st["bin"], st["ip"], old_leaf, old_cert,
                              st["ports"], args.tail_seconds)
    if rc != 0:
        Fail("the precondition upload failed (IAPTool exit %d)" % rc)
        return False
    if TOOL_CERT_ACCEPTED not in tool:
        Fail("IAPTool never said %r." % TOOL_CERT_ACCEPTED)
        Fail("  Without that line the app may be signed by the root itself rather")
        Fail("  than by a delegated leaf, which is not the precondition these cases need.")
        return False
    if LOG_IMAGE_ACCEPTED not in serial:
        Fail("the board did not say %r" % LOG_IMAGE_ACCEPTED)
        return False
    Ok("  the board accepted an image signed by a leaf of the current root")

    Section("Precondition 4/4  the board actually runs it")
    log = reset_and_capture(st["cli"], st["ports"], args.seconds)
    if LOG_CAPTURE_PROOF not in log:
        Fail("no boot log captured (%d bytes) - check the log ports above" % len(log))
        return False
    if LOG_APP_STARTED not in log:
        Fail("the board did not start the application (no %r)" % LOG_APP_STARTED)
        return False
    Ok("  the board boots the leaf-signed application")

    st["baseline"] = app_digest(st["cli"], st["compare_bytes"], "baseline")
    if st["baseline"] is None:
        return False
    print("  application region %s bytes, SHA-256 %s..."
          % (format(st["compare_bytes"], ",d"), st["baseline"][:16]))
    return True


# ------------------------------------------------------------------ cases
def case_t2_12(st, args):
    """Rotate the root, then check the installed app is refused at the next boot."""
    Section("T2-12  after the rotation, the installed app is refused at boot")

    print("  moving the board into the bootloader so setowner can be served")
    if not move_to_bootloader(st["iap"], st["ip"], args.port, st["old_leaf"],
                              st["old_cert"], st["ports"]):
        Fail("could not get the board into the bootloader")
        return SETUP

    st["new_root"] = (Path(args.new_root_key) if args.new_root_key
                      else iap_genkey(st["iap"], st["keydir"], "new_root"))
    if st["new_root"] is None or not st["new_root"].exists():
        Fail("no incoming root key")
        return SETUP
    st["new_root_pub"] = iap_pubkey(st["iap"], st["new_root"])
    if not st["new_root_pub"]:
        return SETUP

    # Driven through run_setowner.py rather than re-implemented: that script is
    # case T2-03 and already checks the handover against the board rather than
    # against the tool. A handover that does not happen is a setup result here --
    # whether setowner works is T2-03's question, not this one's.
    print("  handing the board to %s" % st["new_root"].name)
    rc = subprocess.call([python_exe(), str(HERE / "run_setowner.py"),
                          "--ip", st["ip"], "--port", args.port,
                          "--current-key", str(args.current_key),
                          "--new-key", str(st["new_root"])])
    # Whether the handover happened is asked of the board, not of the exit code.
    # The two can disagree, and the one that decides what the rest of this run
    # means is the board.
    now = tcp_command(st["ip"], args.port, "getpubkey").lower()
    if now != st["new_root_pub"]:
        Fail("the board still does not report the new root (run_setowner.py exit %d)" % rc)
        Fail("  Nothing about revocation was tested. setowner itself is case T2-03.")
        return SETUP
    if rc != 0:
        Warn("  run_setowner.py exited %d, but the board DID take the new root" % rc)
    Ok("  the board's root is now %s..." % now[:16])

    log = reset_and_capture(st["cli"], st["ports"], args.seconds)
    if LOG_CAPTURE_PROOF not in log:
        Warn("  no boot log captured (%d bytes); nothing was proved this round" % len(log))
        return SETUP
    Ok("  boot log captured")

    rejected = LOG_APP_REJECTED in log
    started = LOG_APP_STARTED in log
    (Ok if rejected else Fail)("  %r %s"
                               % (LOG_APP_REJECTED, "present" if rejected else "MISSING"))
    if started:
        Fail("  the board started the app anyway - the old leaf was NOT revoked")
    else:
        Ok("  the board stayed in the bootloader")
    return PASS if (rejected and not started) else FAIL


def case_t2_13(st, args):
    """The old leaf cannot upload either, and the application region is untouched."""
    Section("T2-13  the old leaf cannot upload, and the app region is untouched")

    before = app_digest(st["cli"], st["compare_bytes"], "t2_13_before")
    if before is None:
        return SETUP
    if before != st["baseline"]:
        Warn("  the application region already differs from the precondition baseline")
        Warn("  (not a failure of this case, but say so before reading the rest)")

    rc, tool, _ = upload(st["iap"], st["bin"], st["ip"], st["old_leaf"],
                         st["old_cert"], st["ports"], args.tail_seconds)
    after = app_digest(st["cli"], st["compare_bytes"], "t2_13_after")
    if after is None:
        return SETUP

    refused = rc != 0
    if refused:
        Ok("  IAPTool exit %d - refused" % rc)
    else:
        Fail("  IAPTool exit 0 - the old leaf uploaded after the root was rotated")
    if TOOL_CERT_REFUSED in tool:
        Ok("  refused for the right reason: %r" % TOOL_CERT_REFUSED)
    elif refused:
        # Still a refusal, but for a reason nobody named. Worth seeing: a case
        # that passes on an unrelated error is a case that tests nothing.
        Warn("  refused, but not with %r - read the tool output above" % TOOL_CERT_REFUSED)

    unchanged = after == before
    if unchanged:
        Ok("  application region unchanged (SHA-256 %s...)" % after[:16])
    else:
        Fail("  the application region CHANGED: %s... -> %s..." % (before[:16], after[:16]))
    return PASS if (refused and unchanged) else FAIL


def case_t2_14(st, args):
    """The positive control: a leaf issued by the NEW root uploads and boots."""
    Section("T2-14  a leaf issued by the NEW root uploads and boots")

    new_leaf = iap_genkey(st["iap"], st["keydir"], "new_leaf")
    if new_leaf is None:
        return SETUP
    new_leaf_pub = iap_pubkey(st["iap"], new_leaf)
    if not new_leaf_pub:
        return SETUP
    new_cert = Path(str(new_leaf) + ".cert")
    if not iap_issue_cert(st["iap"], st["new_root"], new_leaf_pub, new_cert):
        return SETUP
    st["new_leaf"], st["new_cert"] = new_leaf, new_cert
    Ok("  leaf key %s, certificate %s" % (new_leaf.name, new_cert.name))

    rc, tool, serial = upload(st["iap"], st["bin"], st["ip"], new_leaf, new_cert,
                              st["ports"], args.tail_seconds)
    accepted = rc == 0 and LOG_IMAGE_ACCEPTED in serial
    if accepted:
        Ok("  the board accepted the image")
    else:
        Fail("  the board did not accept the image (IAPTool exit %d)" % rc)
    if TOOL_CERT_ACCEPTED not in tool:
        Warn("  IAPTool never said %r - was the certificate used at all?"
             % TOOL_CERT_ACCEPTED)

    log = reset_and_capture(st["cli"], st["ports"], args.seconds)
    if LOG_CAPTURE_PROOF not in log:
        Warn("  no boot log captured (%d bytes); nothing was proved this round" % len(log))
        return SETUP
    Ok("  boot log captured")

    started = LOG_APP_STARTED in log
    if started:
        Ok("  the board started the application")
    else:
        Fail("  the board did not start the application (no %r)" % LOG_APP_STARTED)
    return PASS if (accepted and started) else FAIL


# ------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--bin", required=True,
                    help="the application image to upload (a real .bin, not a stand-in)")
    ap.add_argument("--current-key", required=True,
                    help="private half of the root this board trusts right now")
    ap.add_argument("--new-root-key", default="",
                    help="the incoming root's private key; generated when omitted")
    ap.add_argument("--ip", default="")
    ap.add_argument("--port", default="56865")
    ap.add_argument("--stop-after", choices=["T2-12", "T2-13", "T2-14"], default="T2-14",
                    help="stopping before T2-14 leaves the board without a runnable app")
    ap.add_argument("--compare-bytes", default="0",
                    help="bytes of the application region to read back; 0 = all of it")
    ap.add_argument("--seconds", type=int, default=10, help="boot log capture window")
    ap.add_argument("--tail-seconds", type=int, default=15,
                    help="how long to keep listening after IAPTool exits")
    ap.add_argument("--keydir", default="",
                    help="where the generated keys and certificates go")
    ap.add_argument("--ports", nargs="*", default=None)
    args = ap.parse_args()

    ip = args.ip or getattr(cfg, "BOARD_IP", "")
    if not ip:
        Fail("need --ip (or set BOARD_IP in config/machine.py)")
        return 2
    image = Path(args.bin)
    if not image.exists():
        Fail("no such image: %s" % image)
        return 2
    current_key = Path(args.current_key)
    if not current_key.exists():
        Fail("no such key: %s" % current_key)
        return 2
    args.current_key = current_key
    if args.new_root_key and not Path(args.new_root_key).exists():
        Fail("no such key: %s" % args.new_root_key)
        return 2

    try:
        iap = get_iap_tool()
    except SystemExit:
        # get_iap_tool exits 1 when the board package holds no IAPTool. That is a
        # setup problem, and this script's exit codes say so with 2, not 1.
        iap = get_go_bin("IAPTool")
        if not iap.exists():
            Fail("no IAPTool: not in the board package, and not built in Output/")
            return 2
        Warn("using the locally built IAPTool, not the one in the board package")

    compare = int(str(args.compare_bytes), 0) or APP_MAX_SIZE
    if compare > APP_MAX_SIZE:
        Fail("--compare-bytes is larger than the application region (%d)" % APP_MAX_SIZE)
        return 2

    keydir = (Path(args.keydir) if args.keydir
              else get_scratch_dir() / ("rotate-root-" + uuid.uuid4().hex[:8]))
    keydir.mkdir(parents=True, exist_ok=True)

    cli = get_programmer_cli()
    st = {"ip": ip, "bin": image, "iap": iap, "cli": cli, "keydir": keydir,
          "ports": args.ports if args.ports else cfg.LOG_PORTS,
          "compare_bytes": compare}

    Section("Setup")
    print("  board:   %s" % ip)
    print("  image:   %s  (%s B)" % (image, format(image.stat().st_size, ",d")))
    print("  IAPTool: %s" % iap)
    print("  key dir: %s" % keydir)
    if not args.keydir:
        Warn("  that is a temp directory. After this run the board's root lives there")
        Warn("  and nothing else can hand the board on. Move it, or pass --keydir.")

    Section("Target check")
    assert_target_reachable(cli)

    if not build_precondition(st, args):
        Section("Verdict")
        Warn("SETUP - the precondition was never reached, so nothing was tested.")
        print("  keys and certificates: %s" % keydir)
        return 2

    results = [("T2-12", case_t2_12(st, args))]
    if args.stop_after != "T2-12" and results[-1][1] != SETUP:
        results.append(("T2-13", case_t2_13(st, args)))
    if args.stop_after == "T2-14" and results[-1][1] != SETUP:
        # Runs even when T2-12 or T2-13 failed: it is the control that tells a
        # correct refusal apart from a board nobody can upload to, and it is also
        # what leaves the board with a runnable application.
        results.append(("T2-14", case_t2_14(st, args)))

    Section("Verdict")
    ran = dict(results)
    for name in ("T2-12", "T2-13", "T2-14"):
        if name in ran:
            {PASS: Ok, FAIL: Fail, SETUP: Warn}[ran[name]]("  %-7s %s" % (name, ran[name]))
        else:
            Warn("  %-7s NOT RUN" % name)

    if any(s == FAIL for s in ran.values()):
        if ran.get("T2-14") != PASS:
            Warn("  T2-14 did not pass either, so a refusal above may only mean this")
            Warn("  board accepts nothing. Fix the control before reading the rest.")
        Fail("FAIL - at least one criterion did not hold.")
        rc = 1
    elif any(s == SETUP for s in ran.values()) or len(ran) < 3:
        Warn("INCONCLUSIVE - a case could not be judged or was not run.")
        rc = 2
    else:
        Ok("PASS - rotating the root revoked the old leaf, and a new one works.")
        rc = 0

    if args.stop_after != "T2-14" or ran.get("T2-14") != PASS:
        Warn("  The board may have no runnable application. See RECOVERY at the top")
        Warn("  of this file.")
    print("")
    print("  keys and certificates: %s" % keydir)
    return rc


if __name__ == "__main__":
    sys.exit(main())
