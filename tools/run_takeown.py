"""T2-01 -- claim a board for a signing key, and check it took (requirement R2-02).

    python3 tools/run_takeown.py                    claim with a freshly generated key
    python3 tools/run_takeown.py --key owner.pem    claim with a specific key
    python3 tools/run_takeown.py --expect-refused   the board already has a root (negative case)

The claim is driven through the shipping tool -- `IAPTool takeown` -- because
that is the path a customer has. The check afterwards is NOT: it asks the board
directly over TCP, so the tool cannot be the one confirming its own work.

A board is claimable only while it has no root (new, or factory-reset); no
button is involved (decision 72, $PROD/docs/modules/M2-ownership.md). The way
back is a factory reset: hold BOOT0 for 10 s.

Exit 0 = the board ended up in the expected state, 1 = it did not, 2 = setup.
"""

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (cfg, Section, Ok, Fail,  # noqa: E402
                    Warn, get_go_bin, get_output_dir, run_capture, tcp_command)


def genkey(iap):
    """Run IAPTool genkey in a fresh directory and return the .pem path.

    NOT a temp directory: after the claim this key is the only one that can
    sign firmware for this board, and a cleaned temp directory costs a
    bootloader reflash. Output/ is gitignored, so the key does not reach git
    either. Criterion T2-01, $PROD/docs/modules/M2-ownership.md.
    """
    where = (get_output_dir() / "owner-keys" /
             datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    where.mkdir(parents=True, exist_ok=True)
    run_capture([iap, "genkey", "owner_key"], cwd=where)
    return where / "owner_key.pem"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ip", default="")
    ap.add_argument("--port", default="56865")
    ap.add_argument("--key", default="", help="the owner's private key (PEM)")
    ap.add_argument("--expect-refused", action="store_true")
    ap.add_argument("--boot0-timeout", type=int, default=0,
                    help="unused since decision 72 (claiming needs no BOOT0); "
                         "accepted so older callers still run")
    ap.add_argument("--ports", nargs="*", default=None)
    args = ap.parse_args()

    ip = args.ip or getattr(cfg, "BOARD_IP", "")
    if not ip:
        Fail("need --ip (or set BOARD_IP in config/machine.py)")
        return 2

    iap = get_go_bin("IAPTool")
    if not iap.exists():
        Fail("IAPTool not built")
        return 2

    Section("before")
    was = tcp_command(ip, args.port, "getpubkey")
    # The generation BEFORE, because the claim is judged on a delta, not on a
    # fixed number: owner_slot_claim() writes "past everything already
    # written", so a board that was factory-reset comes back at 6, not at 1.
    was_gen = tcp_command(ip, args.port, "getowner")
    print("  getpubkey: %s" % was)
    print("  getowner:  %s" % was_gen)
    has_root = re.fullmatch(r"[0-9a-fA-F]{128}", was) is not None
    if not has_root and was.strip() != "none":
        Fail("the board did not answer getpubkey with a key or 'none' -- is it in the bootloader?")
        return 2
    if has_root != args.expect_refused:
        Warn("SETUP - the board %s a root; %s needs it %s." % (
            "has" if has_root else "has no",
            "--expect-refused" if args.expect_refused else "a claim",
            "to have one" if args.expect_refused else "to have none (factory-reset it: hold BOOT0 10 s)"))
        return 2

    key = Path(args.key) if args.key else None
    if key is None:
        Section("generating a key to claim with")
        key = genkey(iap)
        # Plain ASCII: the console codepage mangles anything else, and a warning
        # that renders as mojibake is a warning nobody reads.
        print("  private key kept at: %s" % key)
        print("  NOTE: from now on that key is the only one that can sign firmware")
        print("        this board will run. Back it up - losing it costs a factory")
        print("        reset (hold BOOT0 10 s) and a new claim.")
    if not key.exists():
        Fail("no such key: %s" % key)
        return 2

    Section("takeown  (through IAPTool, the way a customer does it)")
    out, rc = run_capture([iap, "takeown", ip, "--key=%s" % key])
    print(out.strip())

    claimed = ""
    m = re.search(r"^\s*([0-9a-fA-F]{128})\s*$", out, re.M)
    if m:
        claimed = m.group(1).lower()

    Section("after  (asked of the board, not of the tool)")
    now = tcp_command(ip, args.port, "getpubkey")
    gen = tcp_command(ip, args.port, "getowner")
    print("  getpubkey: %s" % now)
    print("  getowner:  %s" % gen)

    Section("result")
    if args.expect_refused:
        if rc == 0:
            Fail("expected a refusal, but IAPTool reported success")
            return 1
        if "refused" not in out.lower() and "already trusts" not in out.lower():
            Fail("IAPTool failed for some other reason:\n%s" % out.strip())
            return 1
        Ok("refused, as expected")
        if now != was:
            Fail("  but the trusted key changed anyway!")
            return 1
        Ok("  and the trusted key is unchanged")
        return 0

    if rc != 0:
        Fail("takeown did not succeed:\n%s" % out.strip())
        return 1
    if not claimed:
        Fail("could not tell from IAPTool's output which key it claimed with")
        return 1
    if now.lower() != claimed:
        Fail("the board reports a different key than the one claimed")
        Fail("  claimed  %s" % claimed)
        Fail("  reports  %s" % now)
        return 1
    try:
        want = int(was_gen.strip()) + 1
        got = int(gen.strip())
    except ValueError:
        Fail("cannot read the generation: before=%r after=%r" % (was_gen, gen))
        return 1
    if got != want:
        Fail("the board reports generation %d; it was %s before, so %d was due"
             % (got, was_gen.strip(), want))
        return 1
    Ok("claimed: the board now reports the new key as its root, at generation %d" % got)
    print()
    print("Next: an application signed by any other key must now be refused - that is")
    print("what proves the new key is in use. To undo: factory reset (hold BOOT0 10 s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
