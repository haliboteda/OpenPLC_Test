"""Board-lifecycle cases on the bootloader stand-in: power cut in the erase
window, the version gate, factory reset and what follows it, revocation.

    python run_lifecycle.py                 all cases
    python run_lifecycle.py --only T2-19    one case (T2-20 runs inside T2-19)
    python run_lifecycle.py --keep          keep the scratch directory

Each case drives the real IAPTool against the real bootloader code
($TEST/host/bootstand) and judges what the board's own log says. The stand-in
keeps its flash between runs, so "reset the board" is "start the supervisor
again on the same state". What only a real board can show -- the button, the
real flash timing, CDC -- is listed per case in M1 / M2.

Exit 0 = all passed, 1 = at least one failed, 2 = prerequisites missing.
"""

import argparse
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (STAND_IN, Fail, Ok, Section, build_iap_tool,  # noqa: E402
                     build_stand_in, fixed_bytes, have_cmd, isolated_env,
                     nonblank_lines, parse_pubkey, read_text, resolve_port,
                     run_env, stage_iap_tool, start_stand_in, stop_stand_in,
                     user_key_path, wait_for_serving)

LOG_INVALID = "App signature invalid or absent"
LOG_FLASHED = "Checksum and signature OK"
LOG_APP = re.compile(r"\[stand-in\] app \S+ serving on")
REVOKE_FREE = re.compile(r"(\d+)/\d+ revoke slot\(s\) free, (\d+) leaf\(s\) revoked")


class Bench:
    """One scratch directory, one staged IAPTool, keys made on the spot."""

    def __init__(self, scratch, iap_tool, port):
        self.scratch, self.port = scratch, port
        self.iap = stage_iap_tool(scratch, iap_tool, port)
        self.env = isolated_env(scratch)
        self.runs = 0

    def tool(self, *args):
        out, _ = run_env([self.iap] + [str(a) for a in args], self.env, cwd=self.scratch)
        return out

    def genkey(self, name):
        pub = parse_pubkey(self.tool("genkey", name))
        if len(pub) != 128:
            Fail("IAPTool genkey printed no public key")
            sys.exit(2)
        return pub, self.scratch / (name + ".pem")

    def cert(self, leaf_pub, root_key, leaf_key):
        out = self.tool("cert", leaf_pub, "--key=%s" % root_key)
        line = next((ln.strip() for ln in out.splitlines()
                     if re.fullmatch(r"[0-9a-f]{256}", ln.strip())), None)
        if line is None:
            Fail("IAPTool cert produced no certificate:\n%s" % out)
            sys.exit(2)
        Path(str(leaf_key) + ".cert").write_text(line + "\n", encoding="utf-8")

    def board(self, state, argv, action, build_dir=None, lifetime=90):
        """Power the stand-in on `state`, run action() once it serves, power
        it off. Returns (action's result, the board's log)."""
        self.runs += 1
        tail = ["--state", self.scratch / state, "--lifetime", str(lifetime),
                "--port", self.port] + argv
        kw = {} if build_dir is None else {"build_dir": build_dir}
        proc, log, handles = start_stand_in(self.scratch, "%s_%d" % (state, self.runs), tail, **kw)
        if not wait_for_serving(log):
            stop_stand_in(proc, handles)
            Fail("the stand-in never came up")
            print(read_text(log))
            return None, ""
        result = action()
        stop_stand_in(proc, handles, log=log)
        return result, read_text(log)

    def upload(self, image, key=None, *extra):
        argv = ["ether", image, "127.0.0.1"] + list(extra)
        if key is not None:
            argv.append("--key=%s" % key)
        return self.tool(*argv)


def judge(problems):
    if problems:
        Fail("FAIL - " + "; ".join(problems))
        return 1
    Ok("PASS")
    return 0


def case_power_cut(b, image, owner_pub, owner_key):
    """T1-22: power fails right after the first erase of an upload; the next
    boot finds no valid app and stays put; a second upload brings it back."""
    Section("T1-22  power cut in the erase window")

    def act():
        first = b.upload(image, owner_key)
        second = b.upload(image, owner_key)
        return first, second

    (first, second), log = b.board("t122", ["--fresh", "--root", owner_pub,
                                            "--fail-after-erase", "1"], act)
    p = []
    if "power cut after erase #1" not in log:
        p.append("the injected power cut did not happen")
    cut = log.find("power cut after erase #1")
    if cut < 0 or LOG_INVALID not in log[cut:]:
        p.append("the boot after the cut did not report %r" % LOG_INVALID)
    if cut < 0 or LOG_FLASHED not in log[cut:]:
        p.append("the second upload was not accepted")
    if cut < 0 or not LOG_APP.search(log[cut:]):
        p.append("the application did not start after the second upload")
    return judge(p)


def case_version_gate(b, image, owner_pub, owner_key, build_v999):
    """T1-38: the board runs 9.9.9; 1.0.0 is refused, --force lets it through
    once, a second --force is refused."""
    Section("T1-38  version gate: older refused, --force once")
    old = b.scratch / "old.bin"
    shutil.copy2(str(image), str(old))
    old.with_suffix(".version").write_text("1.0.0", encoding="utf-8")

    def install():
        return b.upload(image, owner_key)   # no .version: let through

    _, log = b.board("t138", ["--fresh", "--root", owner_pub], install, build_v999)
    if not LOG_APP.search(log):
        return judge(["the 9.9.9 application never started"])

    def tries():
        refused = b.upload(old, owner_key)
        forced = b.upload(old, owner_key, "--force")
        again = b.upload(old, owner_key, "--force")
        return refused, forced, again

    (refused, forced, again), log = b.board("t138", [], tries, build_v999)
    p = []
    if "older" not in refused.lower():
        p.append("1.0.0 over 9.9.9 was not refused as older")
    if log.count(LOG_FLASHED) != 1:
        p.append("expected exactly one image written (the forced one), got %d"
                 % log.count(LOG_FLASHED))
    if "FORCE FLASH" not in forced:
        p.append("--force did not say FORCE FLASH")
    if "force flash has already been used once" not in again:
        p.append("the second --force was not refused")
    if p:
        for name, out in (("refused", refused), ("forced", forced), ("again", again)):
            print("  IAPTool (%s):" % name)
            for line in nonblank_lines(out)[-6:]:
                print("    %s" % line)
    return judge(p)


def case_factory_reset(b, image, owner_pub, owner_key):
    """T2-05, T2-09, T2-36 on one board: claimed with an app; BOOT0 held ten
    seconds; then a plain reset; then an upload from a host with no key."""
    _, log = b.board("t205", ["--fresh", "--root", owner_pub],
                     lambda: b.upload(image, owner_key))
    if not LOG_APP.search(log):
        Fail("setup: the claimed board did not start its application")
        return 3

    failed = 0
    Section("T2-05  factory reset leaves no root")
    owner_out, log = b.board("t205", ["--gesture", "factory"],
                             lambda: b.tool("getowner", "127.0.0.1"))
    p = []
    if "FACTORY RESET DONE" not in log:
        p.append("the board did not report FACTORY RESET DONE")
    if "[stand-in] root none" not in log:
        p.append("the board still has a root")
    if "No root" not in owner_out:
        p.append("getowner did not answer 'No root'")
    failed += judge(p)

    Section("T2-09  factory reset invalidates the installed app")
    _, log = b.board("t205", [], lambda: None)
    p = []
    if LOG_INVALID not in log:
        p.append("the plain reset did not report %r" % LOG_INVALID)
    if LOG_APP.search(log):
        p.append("the old application started")
    failed += judge(p)

    Section("T2-36  claimed again by a newly generated key")
    user_key = user_key_path(b.env)
    if user_key.exists():
        user_key.unlink()
    out, log = b.board("t205", [], lambda: b.upload(image))
    p = []
    if "Claimed." not in out:
        p.append("IAPTool did not claim the board")
    m = re.search(r"\[stand-in\] root ([0-9a-f]{128})", log)
    new_root = m.group(1) if m else ""
    if not new_root:
        p.append("the board was not claimed")
    elif new_root == owner_pub:
        p.append("the board was claimed for the first owner's key again")
    elif user_key.exists() and new_root not in b.tool("pubkey", user_key):
        p.append("the board was claimed for a key other than the generated one")
    if LOG_FLASHED not in log:
        p.append("the upload did not follow the claim")
    failed += judge(p)
    return failed


def case_revoke(b, image, root_pub, root_key):
    """T2-19 and T2-20: two leaves revoked, both refused, a third still
    accepted; revoking one again writes nothing."""
    leaves = {}
    for name in ("leaf_a", "leaf_b", "leaf_c"):
        pub, key = b.genkey(name)
        b.cert(pub, root_key, key)
        leaves[name] = (pub, key)

    def revoke_two():
        return [b.tool("revoke", "127.0.0.1", "--key=%s" % root_key, "--leaf=%s" % leaves[n][0])
                for n in ("leaf_a", "leaf_b")]

    b.board("t219", ["--fresh", "--root", root_pub], revoke_two)

    Section("T2-19  two leaves revoked, both take effect")

    def uploads():
        return [b.upload(image, leaves[n][1]) for n in ("leaf_a", "leaf_b", "leaf_c")]

    outs, log = b.board("t219", [], uploads)
    p = []
    m = REVOKE_FREE.search(log)
    free_after_two = m.group(1) if m else None
    if not m or m.group(2) != "2":
        p.append("the boot line does not say 2 leaf(s) revoked")
    if log.count(LOG_FLASHED) != 1:
        p.append("expected only leaf_c's image accepted, %d were" % log.count(LOG_FLASHED))
    if not LOG_APP.search(log):
        p.append("leaf_c's image did not start")
    failed = judge(p)

    Section("T2-20  revoking the same leaf again writes nothing")
    again, log = b.board("t219", ["--gesture", "upload"],
                         lambda: b.tool("revoke", "127.0.0.1", "--key=%s" % root_key,
                                        "--leaf=%s" % leaves["leaf_a"][0]))
    _, log2 = b.board("t219", ["--gesture", "upload"], lambda: None)
    p = []
    if "already revoked" not in again:
        p.append("the second revoke did not answer 'already revoked'")
    m2 = REVOKE_FREE.search(log2)
    if not m2 or m2.group(1) != free_after_two or m2.group(2) != "2":
        p.append("the revoke slots changed: %s -> %s"
                 % (free_after_two, m2.group(1) if m2 else "?"))
    failed += judge(p)
    return failed


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", default="")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    if not have_cmd("go"):
        Fail("go is not on PATH")
        return 2
    iap_tool = build_iap_tool()
    build_stand_in()
    # Under build/, which git ignores: an application half that reports 9.9.9.
    build_v999 = STAND_IN / "build" / "v999"
    if not args.only or args.only == "T1-38":
        build_stand_in(build_v999, ["-DAPP_VERSION=9.9.9"])

    scratch = Path(tempfile.mkdtemp(prefix="lifecycle-"))
    print("scratch: %s" % scratch)
    b = Bench(scratch, iap_tool, resolve_port())
    owner_pub, owner_key = b.genkey("owner")
    image = scratch / "app.bin"
    image.write_bytes(fixed_bytes(2048, 31, 7))

    cases = [
        (("T1-22",), lambda: case_power_cut(b, image, owner_pub, owner_key)),
        (("T1-38",), lambda: case_version_gate(b, image, owner_pub, owner_key, build_v999)),
        (("T2-05", "T2-09", "T2-36"), lambda: case_factory_reset(b, image, owner_pub, owner_key)),
        (("T2-19", "T2-20"), lambda: case_revoke(b, image, owner_pub, owner_key)),
    ]
    failed = 0
    for ids, run in cases:
        if args.only and args.only not in ids:
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
    Ok("all lifecycle cases behaved as expected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
