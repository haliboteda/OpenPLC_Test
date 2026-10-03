"""Protocol and ownership cases on the bootloader stand-in: the existing Go
cases (TestCase) and IAPTool-driven checks that used to need a real board.

    python run_protocol.py                 all cases
    python run_protocol.py --only T2-15    one group (see GROUPS below)
    python run_protocol.py --keep          keep the scratch directory

The stand-in runs the real IAP code; what it replaces (lwIP and the PHY, USB,
real flash timing, the BOOT0 button) is why each case still has a real-board
row in M1 / M2. The split is in $PROD/maps/sim-coverage/SIM-05-findings.md.

Exit 0 = all passed, 1 = at least one failed, 2 = prerequisites missing.
"""

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (EXE, Fail, Ok, Section, build_iap_tool,  # noqa: E402
                     build_stand_in, cfg, fixed_bytes, have_cmd, nonblank_lines,
                     resolve_port, run_env, user_key_path)
from run_lifecycle import Bench, judge, LOG_APP, LOG_FLASHED, LOG_INVALID  # noqa: E402
import run_journal_reclaim as s15  # noqa: E402  (tools/ is on sys.path via _common)
from common import tcp_command  # noqa: E402

APP_OFFSET = 0x20000          # IAP_APP_ADDRESS - flash base; flash.bin starts at 0x08000000
SLOTS = re.compile(r"Bootloader state: (\d+)/\d+ metadata slots used")


def app_digest(b, state, size):
    data = (b.scratch / state / "flash.bin").read_bytes()
    return hashlib.sha256(data[APP_OFFSET:APP_OFFSET + size]).hexdigest()


def build_testcase(scratch):
    exe = scratch / ("TestCase" + EXE)
    out, rc = run_env(["go", "build", "-o", exe, "."], None, cwd=cfg.TEST_REPO)
    if rc != 0:
        Fail("cannot build TestCase:\n%s" % out)
        sys.exit(2)
    return exe


def tc(b, exe, case, image, key):
    out, rc = run_env([exe, case, "--ip=127.0.0.1", "--port=%s" % b.port,
                       "--bin=%s" % image, "--key=%s" % key, "--iaptool=%s" % b.iap],
                      b.env, cwd=b.scratch)
    return rc, out


def group_testcase(b, exe, image, owner_pub, owner_key):
    """T1-01 T1-02 T1-05..T1-12 T1-24: the Go cases, as written for a real board.
    T1-09 uploads an image, so it runs last."""
    # T1-09 runs IAPTool without --key, so the key must be at the default place.
    default_key = user_key_path(b.env)
    default_key.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(owner_key), str(default_key))
    order = ["T1-01", "T1-02", "T1-05", "T1-06", "T1-07", "T1-08", "T1-10",
             "T1-11", "T1-12", "T1-24", "T1-09"]
    results, _ = b.board("tc", ["--fresh", "--root", owner_pub],
                         lambda: [(c,) + tc(b, exe, c, image, owner_key) for c in order],
                         lifetime=900)
    failed = 0
    for case, rc, out in results or []:
        Section("%s  Go case on the stand-in" % case)
        failed += judge([] if rc == 0 else ["TestCase exited %d: %s"
                                            % (rc, " | ".join(nonblank_lines(out)[-3:]))])
    return failed


def group_app(b, exe, image, owner_pub, owner_key):
    """T1-23 T1-26 (one upload), T1-14 (a refused upload leaves the app),
    T1-13 (a damaged app is caught at the next boot)."""
    failed = 0
    _, log0 = b.board("app", ["--fresh", "--root", owner_pub], lambda: None)
    _, log1 = b.board("app", [], lambda: b.upload(image, owner_key))
    _, log2 = b.board("app", [], lambda: None)

    Section("T1-23  the image is verified before the app region is erased")
    v, e = log1.find("Transfer complete, verifying"), log1.find("Erasing application region")
    failed += judge([] if 0 <= v < e else ["verify did not come before erase"])

    Section("T1-26  one successful upload takes 7 metadata slots")
    s0, s2 = SLOTS.search(log0), SLOTS.search(log2)
    used = (int(s2.group(1)) - int(s0.group(1))) if s0 and s2 else None
    failed += judge([] if used == 7 else ["slots used went up by %s, not 7" % used])

    Section("T1-14  a refused upload leaves the installed app as it was")
    size = image.stat().st_size
    before = app_digest(b, "app", size)
    (rc, out), _ = b.board("app", ["--gesture", "upload"],
                           lambda: tc(b, exe, "T1-11", image, owner_key))
    _, log = b.board("app", [], lambda: None)
    p = []
    if rc != 0:
        p.append("TestCase T1-11 exited %d: %s" % (rc, " | ".join(nonblank_lines(out)[-4:])))
    if app_digest(b, "app", size) != before:
        p.append("the app region changed")
    if not LOG_APP.search(log):
        p.append("the app did not start afterwards")
    failed += judge(p)

    Section("T1-13  a damaged app is refused at the next boot")
    flash = b.scratch / "app" / "flash.bin"
    data = bytearray(flash.read_bytes())
    data[APP_OFFSET + 100] ^= 0xFF
    flash.write_bytes(bytes(data))
    _, log = b.board("app", [], lambda: None)
    p = []
    if LOG_INVALID not in log:
        p.append("the boot did not report %r" % LOG_INVALID)
    if LOG_APP.search(log):
        p.append("the damaged app started")
    failed += judge(p)
    return failed


def group_reclaim(b, image, owner_pub, owner_key):
    """T1-28: a full metadata area is reclaimed by the next upload; the
    calibration area comes through unchanged and the app starts."""
    Section("T1-28  a full metadata area is reclaimed, calibration kept")
    b.board("meta", ["--fresh", "--root", owner_pub], lambda: b.upload(image, owner_key))
    flash = b.scratch / "meta" / "flash.bin"
    data = bytearray(flash.read_bytes())
    base = s15.SECTOR_ADDR - 0x08000000
    calib = fixed_bytes(0x2000, 11, 3)
    data[base:base + len(calib)] = calib
    lo, hi = base + s15.META_OFFSET, base + s15.META_OFFSET + s15.METADATA_BYTES
    data[lo:hi], _, added = s15.fill(bytes(data[lo:hi]), 2)
    flash.write_bytes(bytes(data))
    _, log1 = b.board("meta", [], lambda: None)
    _, log2 = b.board("meta", ["--gesture", "upload"], lambda: b.upload(image, owner_key))
    _, log3 = b.board("meta", [], lambda: None)
    after = flash.read_bytes()[base:base + len(calib)]
    p = []
    if added == 0 or s15.JOURNAL_FULL not in log1:
        p.append("the board did not report the metadata area full")
    if s15.RECLAIMING not in log2:
        p.append("the upload did not reclaim sector 15")
    if after != calib:
        p.append("the calibration area changed")
    if not LOG_APP.search(log3):
        p.append("the app did not start after the reclaim")
    return judge(p)


def group_rootless_flashboot(b):
    """T1-32, the board's half: a board with no root refuses flashboot.
    Sent raw, since IAPTool refuses before it gets that far."""
    Section("T1-32  a board with no root refuses flashboot")
    cmd = "flashboot 4096 deadbeef %s %s %s" % ("00" * 64, "00" * 128, "00" * 64)
    reply, log = b.board("t132", ["--fresh"], lambda: tcp_command("127.0.0.1", b.port, cmd))
    p = []
    if reply != "Refused":
        p.append("the board answered %r, not Refused" % reply)
    if "has no root" not in log:
        p.append("the board did not say it has no root")
    return judge(p)


def group_claim(b, image, owner_pub, owner_key):
    """T2-01: takeown on a board with no root."""
    Section("T2-01  takeown binds a rootless board to the key")
    out, log = b.board("claim", ["--fresh"],
                       lambda: (b.tool("takeown", "127.0.0.1", "--key=%s" % owner_key),
                                b.tool("getowner", "127.0.0.1")))
    p = []
    if "Claimed at generation" not in out[0]:
        p.append("takeown did not claim")
    if owner_pub[:16] not in out[1].lower() and owner_pub not in log:
        p.append("getowner does not name the key")
    return judge(p)


def group_rotate(b, image, owner_pub, owner_key):
    """T2-11 delegated leaf accepted; T2-03 bad-signature handover refused;
    T2-12 after a root change the old leaf's app is refused at boot;
    T2-14 a leaf of the new root uploads and starts."""
    failed = 0
    leaf_pub, leaf_key = b.genkey("leaf_old")
    b.cert(leaf_pub, owner_key, leaf_key)
    Section("T2-11  the board accepts and runs a leaf-certified upload")
    _, log = b.board("rot", ["--fresh", "--root", owner_pub], lambda: b.upload(image, leaf_key))
    failed += judge([] if LOG_FLASHED in log and LOG_APP.search(log)
                    else ["the leaf's image was not accepted and started"])

    Section("T2-03  a handover with a bad signature is refused")
    script = Path(cfg.TEST_REPO) / "tools" / "run_setowner.py"
    (out, rc), _ = b.board("rot", ["--gesture", "upload"],
                           lambda: run_env([sys.executable, script, "--current-key", owner_key,
                                            "--ip", "127.0.0.1", "--port", b.port,
                                            "--bad-signature"], b.env, cwd=b.scratch))
    failed += judge([] if rc == 0 else ["run_setowner --bad-signature exited %d: %s"
                                        % (rc, " | ".join(nonblank_lines(out)[-3:]))])

    new_pub, new_key = b.genkey("owner_new")
    b.board("rot", ["--gesture", "upload"],
            lambda: b.tool("setowner", "127.0.0.1", "--current-key=%s" % owner_key,
                           "--new-key=%s" % new_key))
    Section("T2-12  after the root changes, the old leaf's app is refused at boot")
    _, log = b.board("rot", [], lambda: None)
    failed += judge([] if LOG_INVALID in log and not LOG_APP.search(log)
                    else ["the old leaf's app still started"])

    Section("T2-14  a leaf of the new root uploads and starts")
    nl_pub, nl_key = b.genkey("leaf_new")
    b.cert(nl_pub, new_key, nl_key)
    _, log = b.board("rot", [], lambda: b.upload(image, nl_key))
    failed += judge([] if LOG_FLASHED in log and LOG_APP.search(log)
                    else ["the new leaf's image was not accepted and started"])
    return failed


def group_revoke(b, image, owner_pub, owner_key):
    """T2-15 revoking does not stop installed firmware; T2-26 getapprevoked;
    T2-16 the revoked leaf is refused; T2-17 another leaf is not;
    T2-25 setowner --wipe frees the revoke slots."""
    failed = 0
    a_pub, a_key = b.genkey("leaf_a")
    b.cert(a_pub, owner_key, a_key)
    c_pub, c_key = b.genkey("leaf_c")
    b.cert(c_pub, owner_key, c_key)
    b.board("rev", ["--fresh", "--root", owner_pub], lambda: b.upload(image, a_key))
    b.board("rev", ["--gesture", "upload"],
            lambda: b.tool("revoke", "127.0.0.1", "--key=%s" % owner_key, "--leaf=%s" % a_pub))

    Section("T2-15  the app a revoked leaf installed keeps running")
    _, log = b.board("rev", [], lambda: None)
    failed += judge([] if LOG_APP.search(log) else ["the installed app did not start"])

    Section("T2-26  getapprevoked names the revoked signer")
    out, _ = b.board("rev", ["--gesture", "upload"],
                     lambda: b.tool("getapprevoked", "127.0.0.1"))
    failed += judge([] if out.startswith("REVOKED") or "\nREVOKED" in out
                    else ["getapprevoked did not answer REVOKED"])

    Section("T2-16  the revoked leaf's next upload is refused")
    _, log = b.board("rev", ["--gesture", "upload"], lambda: b.upload(image, a_key))
    failed += judge([] if LOG_FLASHED not in log else ["the revoked leaf's image was written"])

    Section("T2-17  another leaf of the same root is not affected")
    _, log = b.board("rev", ["--gesture", "upload"], lambda: b.upload(image, c_key))
    out, _ = b.board("rev", ["--gesture", "upload"],
                     lambda: b.tool("getapprevoked", "127.0.0.1"))
    p = []
    if LOG_FLASHED not in log:
        p.append("leaf_c's image was refused")
    if "still trusted" not in out:
        p.append("getapprevoked did not answer 'still trusted' after leaf_c's upload")
    failed += judge(p)

    Section("T2-25  setowner --wipe frees every revoke slot")
    new_pub, new_key = b.genkey("owner_wipe")
    b.board("rev", ["--gesture", "upload"],
            lambda: b.tool("setowner", "127.0.0.1", "--current-key=%s" % owner_key,
                           "--new-key=%s" % new_key, "--wipe"))
    _, log = b.board("rev", ["--gesture", "upload"], lambda: None)
    failed += judge([] if re.search(r"96/96 revoke slot\(s\) free, 0 leaf\(s\) revoked", log)
                    else ["the revoke slots were not all freed"])
    return failed


def group_flashboot(b, image, owner_pub, owner_key):
    """T1-30: a bootloader image signed by a leaf is refused, nothing written."""
    Section("T1-30  a leaf cannot replace the bootloader")
    leaf_pub, leaf_key = b.genkey("leaf_fb")
    b.cert(leaf_pub, owner_key, leaf_key)
    boot = b.scratch / "boot.bin"
    boot.write_bytes(fixed_bytes(4096, 13, 5))
    before = (b.scratch / "fb" / "flash.bin")
    out, log = b.board("fb", ["--fresh", "--root", owner_pub],
                       lambda: b.tool("flashboot", boot, "127.0.0.1", "--key=%s" % leaf_key))
    p = []
    if "untouched" not in log:
        p.append("the board did not report the bootloader untouched")
    head = before.read_bytes()[:4096]
    if head == boot.read_bytes():
        p.append("sector 0 was written")
    return judge(p)


GROUPS = {
    "T1-01": "tc", "T1-02": "tc", "T1-05": "tc", "T1-06": "tc", "T1-07": "tc", "T1-08": "tc",
    "T1-09": "tc", "T1-10": "tc", "T1-11": "tc", "T1-12": "tc", "T1-24": "tc",
    "T1-13": "app", "T1-14": "app", "T1-23": "app", "T1-26": "app",
    "T1-28": "meta", "T1-30": "fb", "T1-32": "noroot", "T2-01": "claim",
    "T2-03": "rot", "T2-11": "rot", "T2-12": "rot", "T2-14": "rot",
    "T2-15": "rev", "T2-16": "rev", "T2-17": "rev", "T2-25": "rev", "T2-26": "rev",
}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", default="")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    if not have_cmd("go"):
        Fail("go is not on PATH")
        return 2
    if args.only and args.only not in GROUPS:
        Fail("no case %s here" % args.only)
        return 2
    iap_tool = build_iap_tool()
    build_stand_in()

    scratch = Path(tempfile.mkdtemp(prefix="protocol-"))
    print("scratch: %s" % scratch)
    b = Bench(scratch, iap_tool, resolve_port())
    exe = build_testcase(scratch)
    owner_pub, owner_key = b.genkey("owner")
    image = scratch / "app.bin"
    image.write_bytes(fixed_bytes(32768, 31, 7))   # TestCase wants more than one chunk

    runs = {
        "tc": lambda: group_testcase(b, exe, image, owner_pub, owner_key),
        "app": lambda: group_app(b, exe, image, owner_pub, owner_key),
        "meta": lambda: group_reclaim(b, image, owner_pub, owner_key),
        "noroot": lambda: group_rootless_flashboot(b),
        "claim": lambda: group_claim(b, image, owner_pub, owner_key),
        "rot": lambda: group_rotate(b, image, owner_pub, owner_key),
        "rev": lambda: group_revoke(b, image, owner_pub, owner_key),
        "fb": lambda: group_flashboot(b, image, owner_pub, owner_key),
    }
    failed = 0
    for name, run in runs.items():
        if args.only and GROUPS[args.only] != name:
            continue
        failed += run()

    if args.keep:
        print("kept: %s" % scratch)
    else:
        shutil.rmtree(str(scratch), ignore_errors=True)
    Section("result")
    if failed:
        Fail("%d case(s) failed" % failed)
        return 1
    Ok("all protocol cases behaved as expected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
