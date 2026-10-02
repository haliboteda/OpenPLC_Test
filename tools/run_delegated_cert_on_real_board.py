"""T2-11 -- a REAL board accepts a delegated certificate and runs the firmware it names.

    python tools/run_delegated_cert_on_real_board.py
    python tools/run_delegated_cert_on_real_board.py --bin ../Output/iap_probe_app.bin
    python tools/run_delegated_cert_on_real_board.py --ip 192.168.0.3

Requirement R2-03: the board has understood certificate chains since version one.

⚠️ THIS IS THE GAP THIS CASE EXISTS TO CLOSE. T1-18d/e/f run against the FAKE
board, so they prove what the TOOL decides. T1-16 compiles the real bootloader
sources on the PC, so it proves what the BOARD'S LOGIC decides. Nothing covered
the span between them -- a real board taking a delegated certificate and
executing the image it vouches for -- except a manual recipe with no case id and
no exit code, so nobody could say whether it had ever been run.

The admin role is played by whoever holds the private half of the root the board
currently trusts. On an unclaimed board that is the published key in the
repository, which is exactly why this case can run straight after a factory
reset: the mechanism under test is identical whoever owns the root, because
iap_cert_verify() only ever asks "did the root I trust sign this paper".

What runs:
  1. ask the board which root it trusts          (the board, not the tool, decides)
  2. check we hold that root's private half      -- otherwise SETUP, not failure
  3. generate a fresh "colleague" leaf key
  4. issue a certificate for it, signed by the root   (never touches the board)
  5. upload an image signed by the LEAF, presenting that certificate
  6. reset and require the board to start the application

⚠️ RECOVERY: this leaves an application on the board. To get back to factory
state: python tools/reset_board_to_factory_state.py

Exit 0 = the board ran a leaf-signed image, 1 = it did not, 2 = setup.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from common import (Fail, Ok, Section, Warn, assert_target_reachable,  # noqa: E402
                    cfg, get_iap_tool, get_output_dir, get_programmer_cli, get_scratch_dir,
                    open_log_ports, read_log_ports)

APP_STARTED = "** APP Mod"
APP_REJECTED = "App signature invalid or absent"
# Printed on every boot whatever happens next, so its absence means the capture
# failed rather than the board having stayed quiet.
CAPTURE_PROOF = "Bootloader state:"
# The board's own commit sequence, printed after IAPTool has stopped sending.
COMMIT_VERIFY = "Transfer complete, verifying"
COMMIT_ERASE = "Erasing application region"
COMMIT_WAIT_S = 10


def run(argv, **kw):
    p = subprocess.run([str(a) for a in argv], stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True,
                       errors="replace", **kw)
    return (p.stdout or ""), p.returncode


def out(argv, **kw):
    return run(argv, **kw)[0]


def out_of(argv):
    return run(argv)[0]


def board_trusted_root(tool, ip):
    """The 128-hex key the board says it verifies against, or None."""
    out = out_of([tool, "getowner", ip])
    m = re.search(r"Trusted key:\s*([0-9a-fA-F]{128})", out)
    if not m:
        print(out.strip())
        return None
    return m.group(1).lower()


def key_public_hex(tool, key_path):
    out = out_of([tool, "pubkey", str(key_path)])
    m = re.search(r"\b([0-9a-fA-F]{128})\b", out)
    return m.group(1).lower() if m else None


def boot_once(cli, ports, seconds):
    open_ports = open_log_ports(ports)
    subprocess.run([str(cli), "-c", "port=SWD", "mode=UR", "-rst"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return "\n".join(read_log_ports(open_ports, seconds).values())


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--ip", default=cfg.BOARD_IP)
    ap.add_argument("--bin", default=None, help="application image to upload")
    ap.add_argument("--root-key", default=None,
                    help="private half of the root the board trusts")
    ap.add_argument("--seconds", type=int, default=12)
    ap.add_argument("--ports", nargs="*", default=None)
    args = ap.parse_args()

    ports = args.ports if args.ports else cfg.LOG_PORTS
    tool = get_iap_tool()
    cli = get_programmer_cli()
    app = Path(args.bin) if args.bin else get_output_dir() / "iap_probe_app.bin"
    if not args.root_key:
        Fail("pass --root-key: the private half of the root this board trusts "
             "(the key that claimed it)")
        return 2
    root_key = Path(args.root_key)

    Section("Setup")
    if not app.exists():
        Fail("no application image at %s" % app)
        return 2
    if not root_key.exists():
        Fail("no root private key at %s" % root_key)
        return 2
    assert_target_reachable(cli)

    # Ask the BOARD which root is in force. Never assume it from the repository:
    # a claimed board trusts something the repository has never seen.
    trusted = board_trusted_root(tool, args.ip)
    if trusted is None:
        Fail("the board did not report a trusted key -- is %s reachable?" % args.ip)
        return 2
    Ok("  board trusts        %s..." % trusted[:32])

    have = key_public_hex(tool, root_key)
    if have is None:
        Fail("could not read the public half of %s" % root_key)
        return 2
    if have != trusted:
        Warn("SETUP - we do not hold the private half of the root this board trusts.")
        Warn("  board trusts  %s..." % trusted[:32])
        Warn("  we hold       %s..." % have[:32])
        Warn("  Pass --root-key, or put the board back with:")
        Warn("    python tools/reset_board_to_factory_state.py")
        return 2
    Ok("  we hold its private half")

    Section("Issue a delegated certificate (the board is not touched)")
    work = Path(get_scratch_dir()) / "t2_11"
    work.mkdir(parents=True, exist_ok=True)
    leaf = work / "colleague.pem"
    if leaf.exists():
        leaf.unlink()
    out = out_of([tool, "genkey", str(work / "colleague")])
    if not leaf.exists():
        Fail("genkey did not produce %s" % leaf)
        print(out.strip())
        return 2
    leaf_pub = key_public_hex(tool, leaf)
    if leaf_pub is None:
        Fail("could not read the colleague's public key")
        return 2
    Ok("  colleague leaf key  %s..." % leaf_pub[:32])

    cert_out = out_of([tool, "cert", leaf_pub, "--key=%s" % root_key])
    m = re.search(r"\b([0-9a-fA-F]{256})\b", cert_out)
    if not m:
        Fail("no 128-byte certificate on stdout")
        print(cert_out.strip())
        return 2
    cert_file = Path(str(leaf) + ".cert")
    cert_file.write_text(m.group(1) + "\n", encoding="utf-8")
    Ok("  certificate issued  %d hex chars, signed by the board's root" % len(m.group(1)))

    Section("Upload, signed by the LEAF, presenting that certificate")
    # ⚠️ IAPTool returns when it has finished SENDING. The board still has to
    # verify, erase and write its metadata after that, and a hard reset landing
    # in that window interrupts the commit -- the board then comes up with
    # "metadata absent" and the case looks like a signature failure when nothing
    # was wrong with the signature. So the serial port is opened BEFORE the
    # upload and the board's own commit lines are what we wait for.
    open_ports = open_log_ports(ports)
    up, rc = run([tool, "ether", str(app), args.ip,
                  "--key=%s" % leaf, "--cert=%s" % cert_file])
    for line in up.splitlines():
        if re.search(r"Fail|Refus|Error|Certificate|complete", line, re.I):
            print("    %s" % line)
    if rc != 0:
        Fail("  IAPTool exited %d" % rc)
    commit = read_log_ports(open_ports, COMMIT_WAIT_S)
    commit_text = "\n".join(commit.values())
    if commit_text:
        print(commit_text)

    verified = COMMIT_VERIFY in commit_text
    erased = COMMIT_ERASE in commit_text
    Ok("  board verified the image") if verified else Warn("  no %r seen" % COMMIT_VERIFY)
    Ok("  board erased and wrote it") if erased else Warn("  no %r seen" % COMMIT_ERASE)
    uploaded = rc == 0 and verified and erased

    Section("Reset and see what runs")
    log = boot_once(cli, ports, args.seconds)
    if log:
        print(log)

    Section("T2-11 verdict")
    if CAPTURE_PROOF not in log:
        Warn("INCONCLUSIVE - no boot log captured (%d bytes); nothing was proved." % len(log))
        Warn("  A log that never arrived looks exactly like a board that said nothing.")
        return 2

    started = APP_STARTED in log
    rejected = APP_REJECTED in log
    Ok("  application started") if started else Fail("  application did NOT start")
    if rejected:
        Fail("  board reported: %s" % APP_REJECTED)

    if uploaded and started and not rejected:
        Ok("PASS - a real board accepted a delegated certificate and ran the image it names.")
        Ok("       This is the span T1-18d/e/f (bootloader stand-in) and T1-16 (host) cannot reach.")
        return 0
    Fail("FAIL - the delegated certificate did not carry an image into execution.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
