"""T1-21 / T1-22 -- pull the power mid-upgrade and see what survives.

    python3 tools/run_s4.py --case a        transfer window: flash is untouched
    python3 tools/run_s4.py --case b        erase/write window: the risky one
    python3 tools/run_s4.py --case b --retry 3

Both cases need a person to pull the plug, and the whole point is WHEN. SDRAM
staging moved the risk: during the transfer the application region is never
touched, and only the few seconds spent erasing and copying out of SDRAM can
leave it half-written. So:

    T1-21  cut between "Staging in SDRAM." and "Erasing application region"
         expect: the old application still boots.            (R1-27, R1-25)
    T1-22  cut after  "Erasing application region"
         expect: the board reports the app invalid, and a re-upload fixes it. (R1-27)

It never asks you to press Enter, and it never trusts you about the timing. It
reads the board's own log to know which window it is in, tells you the moment
the window opens, and then waits for the board to come back on its own. If you
miss the window it says so and offers to retry rather than recording a pass for
a cut that landed somewhere else -- that is the difference between testing R1-27
and testing nothing.

⚠ ST-Link may be feeding the target. STLINK-V3 can supply 3.3V, and if it is,
removing the board's own supply does not power-cycle the MCU and both cases are
meaningless. This script measures the target voltage before and during the cut
and refuses to record a pass if the rail never actually fell.

Exit 0 = the case passed, 1 = it failed, 2 = the run could not be set up.
"""

import argparse
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (cfg, Section, Ok, Warn, Fail, banner, get_iap_tool,  # noqa: E402
                    get_programmer_cli, get_scratch_file, local_ip_for,
                    open_log_ports, target_voltage)

# Exactly the strings the bootloader prints. Anchored on IAPServer/IAP_server.c
# so a wording change breaks the test loudly instead of making it wait forever:
#   :350  ". Staging in SDRAM."                     transfer starts, flash safe
#   :92   "Erasing application region ("            the risky window opens
#   :431  "Checksum and signature OK. Rebooting..." the upgrade finished
#   :573  "App signature invalid or absent"         the T1-22 verdict
STAGING = "Staging in SDRAM"
ERASING = "Erasing application region"
FINISHED = "Checksum and signature OK"
APP_INVALID = "App signature invalid or absent"
BOOT_BANNER = "Checking Starting Mod"
APP_MOD = "APP Mod"


class LogTail:
    """Streams the log ports into one growing buffer on a background thread.

    common.read_log_ports() drains for a fixed number of seconds and closes the
    ports, which cannot drive a decision that has to be made while the transfer
    is still running.
    """

    def __init__(self, handles):
        self.handles = handles
        self.text = ""
        self.last_byte_at = time.monotonic()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            for h in self.handles.values():
                try:
                    n = h.in_waiting
                    if n:
                        chunk = h.read(n).decode("utf-8", "replace")
                        with self._lock:
                            self.text += chunk
                            self.last_byte_at = time.monotonic()
                except Exception:
                    pass
            time.sleep(0.05)

    def stop(self):
        self._stop.set()
        for h in self.handles.values():
            try:
                h.close()
            except Exception:
                pass

    def seen(self, needle, since=0):
        with self._lock:
            return self.text.find(needle, since)

    def snapshot(self):
        with self._lock:
            return self.text

    def quiet_for(self):
        with self._lock:
            return time.monotonic() - self.last_byte_at


_local_ip_cache = {}


def _cached_local_ip(ip):
    """local_ip_for(ip), resolved at most once per address.

    This is called from a poll loop (wait_for ticks every 0.2s by default),
    and local_ip_for shells out to `go run` -- too slow to call every tick.
    Caching per-ip, not once globally, still lets the fallback (no matching
    physical interface -> None, i.e. let the OS route it) come out right if
    this script is ever pointed at more than one address in ip's lifetime.
    """
    if ip not in _local_ip_cache:
        _local_ip_cache[ip] = local_ip_for(ip)
    return _local_ip_cache[ip]


def discovery_answers(ip, port=56865, timeout=0.6):
    # Pinned to the physical interface on the board's subnet. Without it a
    # VPN or other virtual adapter holding a better-metric default route can
    # take the packet -- measured 2026-09-18, this is what made a live board
    # read as absent. See $PROD/docs/tables/DECISIONS.md decision 51.
    local_ip = _cached_local_ip(ip)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if local_ip:
        s.bind((local_ip, 0))
    s.settimeout(timeout)
    try:
        s.sendto(b"openplc_server_where_r_y", (ip, int(port)))
        s.recvfrom(512)
        return True
    except Exception:
        return False
    finally:
        s.close()


def usable_image(path, iaptool):
    """The image exists, and something can sign it.

    Deliberately not "the .sig sidecar exists": IAPTool signs in memory whenever
    a key is reachable -- --key, then signing_key in local_config.json, then
    keys/fw_signing_key.pem beside the executable -- and only falls back to a
    sidecar when there is no key anywhere. Demanding the sidecar would refuse a
    perfectly usable image.
    """
    p = Path(path)
    if not p.is_file():
        return None, "%s does not exist" % p
    has_key = (Path(iaptool).parent / "keys" / "fw_signing_key.pem").exists()
    has_sidecar = any(Path(str(p)[:-4] + s).exists() or
                      p.with_suffix(p.suffix + s).exists()
                      for s in (".sig",))
    if not has_key and not has_sidecar:
        return None, ("no signing key beside %s and no .sig beside %s -- "
                      "pass --key, or run: IAPTool sign %s"
                      % (Path(iaptool).name, p.name, p))
    return p, ""


def pad_image(src, target_bytes):
    """A zero-padded copy, so the transfer takes long enough to interrupt.

    83 KB crosses the wire in well under a second, which no hand can hit. Zeros
    appended after the image are unused flash: the vector table and code sit at
    the start, so a padded application still boots. That matters because if the
    T1-21 window is missed the erase goes ahead and this image IS written -- and
    then the board should be left running something real, not a blob.
    """
    src = Path(src)
    data = src.read_bytes()
    if len(data) >= target_bytes:
        return src
    out = Path(get_scratch_file("s4_padded_" + src.name))
    out.write_bytes(data + b"\x00" * (target_bytes - len(data)))
    print("  padded %s: %d -> %d bytes  (%s)"
          % (src.name, len(data), target_bytes, out))
    return out


def wait_for(predicate, timeout, tick=0.2, on_tick=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        if on_tick:
            on_tick(deadline - time.monotonic())
        time.sleep(tick)
    return False


def ticker(label, expect=None):
    """A one-line live clock for wait_for's on_tick.

    The operator has to act inside a window of a few seconds and cannot see the
    serial log, so counting in their head against somebody else's message is the
    only cue they otherwise have -- which is how the first T1-22 attempts were
    missed by seconds in both directions. This puts the clock on their screen.
    """
    t0 = time.monotonic()

    def tick(_remaining):
        el = time.monotonic() - t0
        bar = "  %s  %5.1fs" % (label, el)
        if expect:
            bar += " / ~%.0fs" % expect
        sys.stdout.write("\r" + bar + "    ")
        sys.stdout.flush()
    return tick


def run_once(case, args, cli, iaptool, image, ip):
    """One attempt. Returns "pass", "fail", "missed" or "setup"."""
    ports = [p for p in (args.ports or cfg.LOG_PORTS) if p]
    handles = open_log_ports(ports)
    if not handles:
        Fail("no log port could be opened; S4 is judged from the board's log")
        return "setup"
    tail = LogTail(handles)
    tail.start()

    v0 = target_voltage(cli)
    print("target voltage before the cut: %s" % ("%.2fV" % v0 if v0 else "unknown"))

    Section("starting the ethernet upgrade")
    cmd = [str(iaptool), "ether", str(image), ip]
    if args.key:
        cmd.append("--key=%s" % args.key)
    print("  $ %s" % " ".join(cmd))
    out_file = get_scratch_file("s4_iaptool.out")
    with open(out_file, "w", encoding="utf-8") as fh:
        proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT, text=True)

    try:
        # --- wait for the window to open -------------------------------------
        if case == "a":
            Section("waiting for the transfer window")
            if not wait_for(lambda: tail.seen(STAGING) >= 0, args.window_timeout,
                            on_tick=ticker("uploading, window not open yet", 10)):
                Fail("never saw %r -- the upgrade did not get as far as staging"
                     % STAGING)
                print(Path(out_file).read_text(encoding="utf-8", errors="replace")[-800:])
                return "setup"
            print()
            banner(["PULL THE POWER NOW.",
                    "The board is receiving into SDRAM; flash is untouched.",
                    "Expected afterwards: the OLD application still boots."])
            missed = ERASING
        else:
            Section("waiting for the erase window")
            if not wait_for(lambda: tail.seen(ERASING) >= 0, args.window_timeout,
                            on_tick=ticker("transferring, window not open yet", 44)):
                Fail("never saw %r" % ERASING)
                print(Path(out_file).read_text(encoding="utf-8", errors="replace")[-800:])
                return "setup"
            print()
            banner(["PULL THE POWER NOW.",
                    "The board is erasing and copying out of SDRAM.",
                    "This window is only a few seconds -- go.",
                    "Expected afterwards: the app is reported INVALID."])
            missed = FINISHED

        # --- did they hit it? ------------------------------------------------
        # The rail, not the silence, decides. A board busy erasing is quiet and
        # does not answer discovery either, so in case b the silence test is true
        # from the instant the window opens -- treating it as the trigger meant
        # reading the voltage while the operator was still walking to the board,
        # finding 3.2V and giving up before they could touch anything.
        #
        # Silence stays as a cheap pre-filter: it keeps this from spawning the
        # programmer while the board is still printing. The voltage read is rate
        # limited because each one is a separate SWD attach worth seconds.
        state = {"v": None, "next_probe": 0.0}

        def rail_is_down():
            if tail.quiet_for() <= 2.5 or discovery_answers(ip):
                return False
            now = time.monotonic()
            if now < state["next_probe"]:
                return False
            state["next_probe"] = now + 4.0
            state["v"] = target_voltage(cli)
            return state["v"] is None or state["v"] <= 1.0

        cut_seen = wait_for(rail_is_down, args.cut_timeout,
                            on_tick=ticker(">>> CUT THE POWER NOW <<<  window open",
                                           34 if case == "a" else 20))
        print()

        if tail.seen(missed) >= 0:
            Warn("the log shows %r -- the window closed before the power went."
                 % missed)
            return "missed"
        if not cut_seen:
            if state["v"] is not None and state["v"] > 1.0:
                Fail("the rail never fell; last read %.2fV after %ds."
                     % (state["v"], args.cut_timeout))
                Warn("  Either the power was never pulled, or ST-Link is feeding")
                Warn("  the target -- disable its power output and try again.")
                return "setup"
            Warn("no sign of the power going within %ds" % args.cut_timeout)
            return "missed"

        print("target voltage during the cut: %s"
              % ("%.2fV" % state["v"] if state["v"] is not None
                 else "SWD cannot reach the target"))
        Ok("power is off")

        # --- wait for it to come back ---------------------------------------
        mark = len(tail.snapshot())
        banner(["PLUG THE POWER BACK IN."])
        if not wait_for(lambda: tail.seen(BOOT_BANNER, mark) >= 0, args.back_timeout):
            Fail("the board never printed %r after power returned" % BOOT_BANNER)
            return "fail"
        Ok("the board is booting")
        time.sleep(args.settle)
        after = tail.snapshot()[mark:]

        Section("what the board says after the cut")
        for line in [l for l in after.splitlines() if l.strip()][:25]:
            print("  " + line)

        # --- verdict ---------------------------------------------------------
        Section("verdict")
        if case == "a":
            if APP_INVALID in after:
                Fail("the application was reported invalid -- flash WAS touched")
                Fail("during the transfer window. That contradicts staging.")
                return "fail"
            if APP_MOD in after:
                Ok("T1-21 PASS: the old application booted, flash was untouched")
                return "pass"
            Warn("neither %r nor %r appeared; read the log above" % (APP_MOD, APP_INVALID))
            return "fail"

        if APP_INVALID not in after:
            Warn("the board did NOT report the app invalid.")
            Warn("  Either the cut landed after the copy finished, or it landed")
            Warn("  before the erase actually began. Retry; this window is narrow.")
            return "missed"
        Ok("the app is reported invalid, as expected for a cut during erase")

        if args.no_recover:
            Warn("--no-recover: stopping without proving the board can be restored")
            return "fail"

        Section("recovering: uploading the same image again")
        rec = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=args.recover_timeout)
        mark2 = len(tail.snapshot())
        got_back = wait_for(lambda: tail.seen(BOOT_BANNER, mark2) >= 0, 120)
        time.sleep(args.settle)
        after2 = tail.snapshot()[mark2:]
        if rec.returncode == 0 and got_back and APP_INVALID not in after2:
            Ok("T1-22 PASS: half-written app reported invalid, re-upload restored it")
            return "pass"
        Fail("the re-upload did not restore the board")
        print(rec.stdout[-600:])
        for line in [l for l in after2.splitlines() if l.strip()][:20]:
            print("  " + line)
        return "fail"
    finally:
        try:
            proc.kill()
        except Exception:
            pass
        tail.stop()


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--case", choices=["a", "b"], required=True)
    ap.add_argument("--bin", help="signed application image to upload")
    ap.add_argument("--ip", help="board address (default: BOARD_IP from config)")
    ap.add_argument("--ports", action="append", help="log port; repeatable")
    ap.add_argument("--key", help="passed to IAPTool as --key")
    ap.add_argument("--retry", type=int, default=1,
                    help="attempts allowed when the window is missed (default 1)")
    ap.add_argument("--window-timeout", type=int, default=180)
    ap.add_argument("--cut-timeout", type=int, default=600,
                    help="how long to wait for you to reach the board")
    ap.add_argument("--back-timeout", type=int, default=600)
    ap.add_argument("--settle", type=int, default=12,
                    help="seconds of log to collect after the board boots")
    ap.add_argument("--recover-timeout", type=int, default=300)
    ap.add_argument("--no-recover", action="store_true",
                    help="T1-22: stop before the recovery upload")
    ap.add_argument("--pad-to", type=int, metavar="BYTES",
                    help="zero-pad the image so both windows last long enough to "
                         "hit by hand. Measured 2026-09-01 at the cap, 1835008 "
                         "(= IAP_APP_MAX_SIZE): transfer window 33.7s, erase+write "
                         "window 20.2s. Use the cap for case b -- 1200000 leaves "
                         "the erase window too short to catch")
    args = ap.parse_args()

    Section("S4%s  power cut %s" % (args.case,
                                    "during the transfer" if args.case == "a"
                                    else "during erase and write"))

    ip = args.ip or getattr(cfg, "BOARD_IP", "")
    if not ip:
        Fail("no board address: pass --ip or set BOARD_IP in config/machine.py")
        return 2

    iaptool = get_iap_tool()
    if not iaptool:
        Fail("IAPTool not found")
        return 2
    cli = get_programmer_cli()
    if not cli:
        Fail("STM32_Programmer_CLI not found -- it is how the power cut is proved")
        return 2

    if not args.bin:
        Fail("--bin is required: a signed application image to upload")
        Warn("  sign one with:  IAPTool sign <app.bin>")
        return 2
    image, why = usable_image(args.bin, iaptool)
    if not image:
        Fail(why)
        return 2
    if args.pad_to:
        image = pad_image(image, args.pad_to)

    print("  board      %s" % ip)
    print("  image      %s" % image)
    print("  IAPTool    %s" % iaptool)

    if not discovery_answers(ip):
        Warn("the board does not answer discovery at %s right now." % ip)
        Warn("  That is normal while the application runs -- IAPTool reboots it")
        Warn("  into the bootloader itself. But if the upgrade below never")
        Warn("  starts, check the cable: the board must be on this network.")

    for attempt in range(1, args.retry + 1):
        if args.retry > 1:
            Section("attempt %d of %d" % (attempt, args.retry))
        # A missed attempt ends with the board unplugged, and the operator needs
        # a moment to put the power back. Starting the next upload immediately
        # just gets "No response" from IAPTool and burns the attempt on nothing.
        if attempt > 1 and not discovery_answers(ip):
            print("  waiting for the board to come back...")
            if not wait_for(lambda: bool(discovery_answers(ip)), 180):
                Fail("the board never came back at %s -- plug the power in" % ip)
                return 2
            Ok("  board is back")
        result = run_once(args.case, args, cli, iaptool, image, ip)
        if result == "pass":
            Section("record it")
            print("  $PROD/docs/modules/M1-firmware-upgrade.md  R1-27 row: status, result, date")
            return 0
        if result in ("fail", "setup"):
            return 1 if result == "fail" else 2
        Warn("window missed on attempt %d" % attempt)
    Fail("out of attempts -- the window was never hit")
    return 1


if __name__ == "__main__":
    sys.exit(main())
