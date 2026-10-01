"""The five user paths, end to end, from factory state (decision 72).

    python3 tools/run_five_paths.py --elf <boot.elf>   the whole round
    python3 tools/run_five_paths.py --from 3           resume at path 3
    python3 tools/run_five_paths.py --only 1 2         just these
    python3 tools/run_five_paths.py --dry-run          print the plan, touch nothing

Every case in this round can be run on its own. What the round adds is the
story: the paths share state, and the order is part of what is under test --
path 2's factory reset only means something because path 1 claimed the board,
and path 5's certificate is issued by the root path 4 handed the board to.

Plan and criteria: $PROD/docs/engineering/HOW-TO-RUN-TESTS.md, section
"五条用户路径 · 从出厂态跑一整轮". This script is that section, executable.

⚠️ YOU HAVE TO BE AT THE BOARD ONCE, in path 2: reset it and hold BOOT0 for
more than 10 s. The script waits for the board itself to report the factory
reset, so there is nothing to type. Everything after path 2 runs unattended.

⚠️ A USB cable to the board's CDC port is needed for path 1 (--cdc, or
CDC_PORT in config/machine.py).

⚠️ DESTRUCTIVE. It mass-erases the board and claims it twice. Do not point it
at anything you are not finished with.

The automatic claims run IAPTool with its user config directory moved into
--keydir, so the keys it generates land there and this machine's real
signing key is never read or overwritten.

Exit code is the verdict: 0 every path held, 1 something failed, 2 a
precondition was never reached (nothing was proven either way).
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from common import (Fail, Ok, Section, Warn, banner, cfg,  # noqa: E402
                    get_iap_tool, get_programmer_cli, open_log_ports,
                    python_exe, read_log_ports, run_capture, tcp_command,
                    wait_for_board)

PASS, FAIL, SETUP = "PASS", "FAIL", "SETUP"
IAP_PORT = 56865

# IAPTool's claim messages (auth.go claimIfUnclaimed) and the board's
# (owner_slot.c, IAP_server.c).
TOOL_NO_ROOT = "This board has no root yet"
TOOL_CLAIMED = re.compile(r"Claimed\. From now on this board runs only firmware signed by (\S+)")
LOG_FACTORY_RESET = "FACTORY RESET DONE"
LOG_APP_REFUSED = "App signature invalid or absent"
LOG_APP_RUNS = "APP Mod"


class Round(object):
    """Runs the steps and remembers what each one left behind."""

    def __init__(self, args):
        self.args = args
        self.ip = args.ip or cfg.BOARD_IP
        self.cdc = args.cdc or getattr(cfg, "CDC_PORT", "")
        self.results = []
        self.keys = {}          # name -> .pem path
        self.keydir = Path(args.keydir)

    # ---------------------------------------------------------------- plumbing
    def tool(self, *argv, env=None, capture=False):
        """A tools/ script, as a child. Returns (exit code, output or "")."""
        cmd = [python_exe(), str(HERE / argv[0])] + [str(a) for a in argv[1:]]
        print("    $ %s" % " ".join(Path(c).name if c.endswith(".py") else c
                                    for c in cmd[1:]))
        if self.args.dry_run:
            return 0, ""
        if not capture:
            return subprocess.run(cmd, env=env).returncode, ""
        proc = subprocess.run(cmd, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT)
        out = proc.stdout.decode("utf-8", errors="replace")
        print(out)
        return proc.returncode, out

    def record(self, step, verdict, note=""):
        self.results.append((step, verdict, note))
        {PASS: Ok, FAIL: Fail, SETUP: Warn}[verdict]("  %s %s %s" % (step, verdict, note))
        return verdict == PASS

    def genkey(self, name):
        """A fresh key, kept where the operator can find it afterwards."""
        where = self.keydir / name
        where.mkdir(parents=True, exist_ok=True)
        if not self.args.dry_run:
            run_capture([str(get_iap_tool()), "genkey", name], cwd=where)
        pem = where / ("%s.pem" % name)
        self.keys[name] = pem
        print("    key %s -> %s" % (name, pem))
        return pem

    def empty_config_env(self, name):
        """Environment in which IAPTool's user config directory is a fresh,
        empty one under --keydir: the automatic claim then has to generate
        its key, and does so there."""
        home = (self.keydir / name).resolve()
        home.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env["APPDATA"] = str(home)              # Windows
        env["XDG_CONFIG_HOME"] = str(home)      # Linux
        if sys.platform == "darwin":
            env["HOME"] = str(home)             # ~/Library/Application Support
        return env, home

    def auto_claim(self, step, name, transport):
        """Upload v1 to a board with no root and prove the tool claimed it.

        Returns the key the board now trusts, or None.
        """
        env, home = self.empty_config_env(name)
        argv = ["upload_and_watch.py", "--bin", self.args.v1,
                "--expect-banner", "IAP_PROBE_APP up v1"] + transport
        rc, out = self.tool(*argv, env=env, capture=True)
        if self.args.dry_run:
            self.record(step, PASS, "(dry run)")
            return home / "openplc" / "keys" / "fw_signing_key.pem"
        m = TOOL_CLAIMED.search(out)
        if TOOL_NO_ROOT not in out or not m:
            self.record(step, FAIL, "IAPTool did not claim the board")
            return None
        key = Path(m.group(1))
        if home not in key.resolve().parents:
            # A signing key configured elsewhere (local_config.json) was found
            # first, so "no key on this machine" was never the situation.
            self.record(step, SETUP, "IAPTool used %s instead of generating one; "
                        "clear signing_key in local_config.json" % key)
            return None
        if not key.exists():
            self.record(step, FAIL, "the key IAPTool printed does not exist: %s" % key)
            return None
        self.record(step, PASS if rc == 0 else FAIL,
                    "generated %s, claimed, v1 runs" % key)
        return key if rc == 0 else None

    def ensure_bootloader(self, key):
        """Get the board out of its application and into the bootloader.

        takeown, setowner and getpubkey are answered only by the bootloader,
        and a board that just finished an upload is running the application.
        Without this a step "fails" having never reached the board.
        """
        if self.args.dry_run:
            print("    $ enter_bootloader.py (if an application is running)")
            return True
        rc, _ = self.tool("enter_bootloader.py", "--key", key, "--seconds", "6")
        if rc != 0:
            Warn("  the application did not step aside for %s" % Path(key).name)
        return rc == 0


def path_0(r):
    Section("0 · factory state")
    Warn("  this mass-erases the board (calibration values are kept)")
    argv = ["reset_board_to_factory_state.py"]
    if r.args.elf:
        argv += ["--elf", r.args.elf]
    rc, _ = r.tool(*argv)
    if rc == 2:
        return r.record("0", SETUP, "factory state could not be proven")
    return r.record("0", PASS if rc == 0 else FAIL)


def path_1(r):
    Section("1 · first upload over USB claims the factory board")
    if not r.cdc:
        return r.record("1/T2-35", SETUP, "no CDC port: pass --cdc or set CDC_PORT")
    key = r.auto_claim("1/T2-35", "claim_usb", ["--cdc", r.cdc])
    if key:
        r.keys["first_owner"] = key
    return key is not None


def path_2(r):
    Section("2 · factory reset, then the next upload claims it again")
    first = r.keys.get("first_owner")

    banner(["RESET THE BOARD, THEN HOLD BOOT0 FOR MORE THAN 10 SECONDS,",
            "until the system LED stays lit. Then let go.",
            "Path 2 proves a factory reset returns the board to no root."])
    if r.args.dry_run:
        print("    (waits for %r on the log ports)" % LOG_FACTORY_RESET)
    else:
        handles = open_log_ports(list(r.args.ports or cfg.LOG_PORTS))
        buf = read_log_ports(handles, r.args.boot0_timeout, until=LOG_FACTORY_RESET)
        if LOG_FACTORY_RESET not in "\n".join(buf.values()):
            return r.record("2-a/T2-05", SETUP, "no factory reset seen within %d s"
                            % r.args.boot0_timeout)
    ok = r.record("2-a/T2-05", PASS, "the board reported %r" % LOG_FACTORY_RESET)

    # The application path 1 installed is signed by a root the board no
    # longer has: after a plain reset it must not start.
    if not r.args.dry_run:
        handles = open_log_ports(list(r.args.ports or cfg.LOG_PORTS))
        run_capture([str(get_programmer_cli()), "-c", "port=SWD", "mode=UR", "-rst"])
        log = "\n".join(read_log_ports(handles, 12, until=LOG_APP_REFUSED).values())
        refused = LOG_APP_REFUSED in log and LOG_APP_RUNS not in log
        wait_for_board(r.ip, timeout=30)
        none = tcp_command(r.ip, IAP_PORT, "getpubkey").strip() == "none"
        ok &= r.record("2-b/T2-09", PASS if refused and none else FAIL,
                       "path 1's app no longer starts; getpubkey says none")

    key = r.auto_claim("2-c/T2-36", "claim_ether", ["--ip", r.ip])
    if key:
        if first and not r.args.dry_run and Path(first).read_bytes() == key.read_bytes():
            return r.record("2-c/T2-36", FAIL, "the re-claim reused path 1's key")
        r.keys["owner"] = key
    return ok and key is not None


def path_3(r):
    Section("3 · a claimed board keeps taking uploads over Ethernet")
    owner = r.keys.get("owner") or (Path(r.args.owner_key) if r.args.owner_key else None)
    if not owner:
        return r.record("3-a/T2-02", SETUP, "no owner key: run path 2 or pass --owner-key")
    if not r.ensure_bootloader(owner):
        return r.record("3-a/T2-02", SETUP, "could not reach the bootloader")

    stranger = r.genkey("stranger")
    ok = r.record("3-a/T2-02", PASS if r.tool(
        "run_takeown.py", "--key", stranger, "--expect-refused")[0] == 0 else FAIL,
        "a board with a root refuses a second claim")

    ok &= r.record("3-b/upgrade", PASS if r.tool(
        "upload_and_watch.py", "--bin", r.args.v2, "--ip", r.ip, "--key", owner,
        "--expect-banner", "IAP_PROBE_APP up v2")[0] == 0 else FAIL,
        "v1 -> v2 over Ethernet, signed by the owner")
    return ok


def path_4(r):
    Section("4 · change the root with setowner")
    owner = r.keys.get("owner") or (Path(r.args.owner_key) if r.args.owner_key else None)
    if not owner:
        return r.record("4-a/T2-03", SETUP, "no owner key: run path 2 or pass --owner-key")
    if not r.ensure_bootloader(owner):
        return r.record("4-a/T2-03", SETUP, "could not reach the bootloader")

    ok = r.record("4-a/T2-03-", PASS if r.tool(
        "run_setowner.py", "--current-key", owner, "--bad-signature")[0] == 0 else FAIL,
        "a bad signature changes nothing")
    owner2 = r.genkey("owner2")
    ok &= r.record("4-b/T2-03", PASS if r.tool(
        "run_setowner.py", "--current-key", owner, "--new-key", owner2)[0] == 0 else FAIL,
        "the current owner handed the board to a new root")
    if not ok:
        return False
    r.keys["owner2"] = owner2

    ok &= r.record("4-c/T2-10", PASS if r.tool(
        "run_old_root_image_is_refused.py", "--bin", r.args.v1,
        "--old-key", owner, "--current-key", owner2)[0] == 0 else FAIL,
        "the old root's image is refused, app region untouched")
    ok &= r.record("4-d/upload", PASS if r.tool(
        "upload_and_watch.py", "--bin", r.args.v1, "--ip", r.ip, "--key", owner2,
        "--expect-banner", "IAP_PROBE_APP up v1")[0] == 0 else FAIL,
        "the new root's image installs and runs")
    return ok


def path_5(r):
    Section("5 · a colleague uploads with a leaf certificate")
    root = r.keys.get("owner2") or (Path(r.args.owner_key) if r.args.owner_key else None)
    if not root:
        return r.record("5/T2-11", SETUP, "no root key: run path 4 or pass --owner-key")
    if not r.ensure_bootloader(root):
        return r.record("5/T2-11", SETUP, "could not reach the bootloader")
    return r.record("5/T2-11", PASS if r.tool(
        "run_delegated_cert_on_real_board.py", "--bin", r.args.v2,
        "--root-key", root)[0] == 0 else FAIL,
        "the colleague's own key plus a certificate from the root")


PATHS = {0: path_0, 1: path_1, 2: path_2, 3: path_3, 4: path_4, 5: path_5}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ip", default="")
    ap.add_argument("--cdc", default="", help="the board's USB CDC port, for path 1")
    ap.add_argument("--elf", default="",
                    help="bootloader ELF path 0 flashes (default: $BOOT/Debug)")
    ap.add_argument("--v1", default="../Output/probe-images/iap_probe_v1.bin")
    ap.add_argument("--v2", default="../Output/probe-images/iap_probe_v2.bin")
    ap.add_argument("--owner-key", default="",
                    help="resume after path 2 with this owner key")
    ap.add_argument("--keydir", default="../Output/five-paths-keys")
    ap.add_argument("--ports", nargs="*", default=None)
    ap.add_argument("--from", dest="start", type=int, default=0,
                    help="resume at this path; 0 is factory state")
    ap.add_argument("--only", nargs="*", type=int, default=None)
    ap.add_argument("--boot0-timeout", type=int, default=600,
                    help="how long path 2 waits for the factory reset. Generous on "
                         "purpose: the countdown starts when the step is reached, "
                         "which is before anybody has read the message.")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would run and touch nothing")
    args = ap.parse_args()

    wanted = sorted(args.only) if args.only is not None else \
        [n for n in sorted(PATHS) if n >= args.start]

    Section("the round")
    print("  board  %s" % (args.ip or cfg.BOARD_IP))
    print("  paths  %s" % ", ".join(str(n) for n in wanted))
    print("  images %s -> %s" % (args.v1, args.v2))
    if args.dry_run:
        Warn("  dry run: nothing below touches the board")
    if 2 in wanted:
        Warn("  you will be asked once, in path 2, to hold BOOT0 for 10 s")

    r = Round(args)
    for n in wanted:
        if not PATHS[n](r) and not args.dry_run:
            Warn("  path %d did not hold; the rest would run on a board in an "
                 "unknown state, so stopping here" % n)
            break

    Section("verdict")
    for step, v, note in r.results:
        print("  %-24s %-6s %s" % (step, v, note))
    bad = [v for _, v, _ in r.results if v == FAIL]
    setup = [v for _, v, _ in r.results if v == SETUP]
    if bad:
        Fail("%d step(s) failed" % len(bad))
        return 1
    if setup:
        Warn("%d step(s) never reached their precondition" % len(setup))
        return 2
    Ok("the whole round held")
    return 0


if __name__ == "__main__":
    sys.exit(main())
