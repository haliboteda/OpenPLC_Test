"""Case T1-34: the Arduino IDE's upload command, run against the bootloader
stand-in ($TEST/host/bootstand: the real bootloader and app-side IAP code).

What the IDE runs is `arduino-cli upload -l network -p <ip>` with
upload_method=ethMethod, which calls the IAPTool inside the board package with
no options. Three cases:

  unclaimed  a factory board (no root, in its bootloader), no user key
             -> a key is generated in the user config dir, the board is
                claimed for it, upload succeeds
  claimed    board running an app, trusts an owner key that sits in the user
             config dir -> upload succeeds
  wrong-key  the user config dir holds some other key
             -> the board ignores the reboot request, arduino-cli fails with
                "did not accept the reboot request"

Success is judged from the board's side too: it must have accepted the reboot
and received the whole image.

Isolation: the package's IAPTool is copied to a scratch directory without the
package's keys/ directory, and APPDATA / TEMP point into scratch, so the real
user key and the upload lock are never touched. The stand-in has its own UID, so no real
board's discovery reply can be taken for it.

Criterion and what this cannot test: $PROD/docs/modules/M1-firmware-upgrade.md, T1-34.

    python run_ide_upload.py           run all three cases
    python run_ide_upload.py --keep    keep the scratch directory

Exit 0 = all three behaved, 1 = at least one did not, 2 = prerequisites missing.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (DISCOVERY_PORT, Fail, Ok, Section, build_stand_in,  # noqa: E402
                     cfg, nonblank_lines, read_text, resolve_port, start_stand_in,
                     stop_stand_in, wait_for_serving)
from common import EXE, get_iap_tool  # noqa: E402

FQBN = ("OpenPLC_Alpha:stm32:OPEN-PLC:pnum=PLC_H743,usb=CDCgen,xusb=FS,"
        "upload_method=ethMethod,knxrole=dual_device")
EXAMPLE = "DO_Outputs"
# Not a real STM32 UID: IAPTool matches boards by UID, so a real board on the
# LAN can never be mistaken for this one.
FAKE_UID = "fa4eb0a2d0000000000000a1"
# What the stand-in's application half reports (its CMake APP_VERSION): older
# than any sketch, so the version gate never refuses.
BOARD_APP_VERSION = "0.0.1"
# discovery takes ~1.3 s to start on Windows; arduino-cli's default wait is 1 s.
DISCOVERY_TIMEOUT = "10s"
LOCK_NAME = "openplc-iap-upload.lock"


def cli_argv(*args):
    return [cfg.ARDUINO_CLI, "--config-file", cfg.ARDUINO_CLI_CONFIG] + list(args)


def run(argv, env=None, cwd=None):
    proc = subprocess.run([str(a) for a in argv], cwd=None if cwd is None else str(cwd),
                          env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, errors="replace")
    return proc.stdout or "", proc.returncode


def compile_example(build):
    sketch = Path(cfg.CORE_LIVE) / "libraries" / "OpenPLC_Ports" / "examples" / EXAMPLE
    if not sketch.exists():
        Fail("example not found: %s" % sketch)
        return None
    out, rc = run(cli_argv("compile", "--fqbn", FQBN, "--build-path", build, sketch))
    if rc != 0:
        Fail("compile failed:")
        for line in nonblank_lines(out)[-10:]:
            print("    " + line)
        return None
    binary = build / (EXAMPLE + ".ino.bin")
    return binary if binary.exists() else None


def stage_tool(tool_dir):
    """The package's IAPTool with its config and no keys."""
    src = get_iap_tool()
    tool_dir.mkdir(parents=True)
    shutil.copy2(str(src), str(tool_dir / ("IAPTool" + EXE)))
    shutil.copy2(str(src.parent / "local_config.json"), str(tool_dir / "local_config.json"))
    return tool_dir / ("IAPTool" + EXE)


def pubkey_of(iap, pem, env):
    out, _ = run([iap, "pubkey", pem], env=env)
    m = re.search(r"\b[0-9a-f]{128}\b", out)
    return m.group(0) if m else None


def find_stand_in_port(env):
    """The address discovery lists for FAKE_UID -- what the IDE's port menu shows."""
    out, _ = run(cli_argv("board", "list", "--format", "json",
                          "--discovery-timeout", DISCOVERY_TIMEOUT), env=env)
    try:
        ports = json.loads(out[out.index("{"):]).get("detected_ports", [])
    except ValueError:
        return None
    for p in ports:
        port = p.get("port", {})
        if port.get("protocol") == "network" and port.get("hardware_id", "").lower() == FAKE_UID:
            return port.get("address")
    return None


def prepare_claimed(scratch, state, iap, binary, owner_pub, owner_pem, port, env, tmp):
    """A stand-in claimed for owner_pub with binary installed, left in state."""
    Section("prepare a claimed board running an app")
    board, board_log, handles = start_stand_in(
        scratch, "prepare", ["--state", state, "--fresh", "--root", owner_pub,
                             "--lifetime", "120", "--port", port, "--uid", FAKE_UID])
    out, rc = "", -1
    try:
        if not wait_for_serving(board_log):
            Fail("the stand-in never came up")
            return False
        out, rc = run([iap, "ether", binary, "127.0.0.1", "--key=%s" % owner_pem], env=env)
        # The image is in once the bootloader has verified it and handed over.
        deadline = time.time() + 20
        while time.time() < deadline and "[stand-in] app " not in read_text(board_log):
            time.sleep(0.2)
    finally:
        stop_stand_in(board, handles, log=board_log)
        lock = tmp / LOCK_NAME
        if lock.exists():
            lock.unlink()
    if rc != 0 or "[stand-in] app " not in read_text(board_log):
        Fail("could not install the sketch on the stand-in (rc=%d):" % rc)
        for ln in nonblank_lines(out)[-10:]:
            print("    " + ln)
        return False
    Ok("claimed for the owner key, %s installed" % binary.name)
    return True


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    for name in ("ARDUINO_CLI", "ARDUINO_CLI_CONFIG", "CORE_LIVE"):
        if not getattr(cfg, name, "") or not Path(getattr(cfg, name)).exists():
            Fail("%s not set or missing in config/machine.py" % name)
            return 2

    started = time.time()
    scratch = Path(tempfile.mkdtemp(prefix="ide-upload-"))
    print("scratch: %s" % scratch)
    appdata, tmp = scratch / "appdata", scratch / "tmp"
    user_keys = appdata / "openplc" / "keys"
    for d in (user_keys, tmp, scratch / "build", scratch / "keys"):
        d.mkdir(parents=True)
    env = dict(os.environ, APPDATA=str(appdata), TEMP=str(tmp), TMP=str(tmp))

    try:
        return run_cases(scratch, env, user_keys, tmp, started)
    finally:
        if args.keep:
            print("kept: %s" % scratch)
        else:
            shutil.rmtree(str(scratch), ignore_errors=True)


def run_cases(scratch, env, user_keys, tmp, started):
    Section("compile %s" % EXAMPLE)
    binary = compile_example(scratch / "build")
    if binary is None:
        return 2
    print("  %s, %d bytes (%.0f s)" % (binary.name, binary.stat().st_size, time.time() - started))

    build_stand_in()
    tool_dir = scratch / "tool"
    iap = stage_tool(tool_dir)
    if iap is None:
        return 2
    port = resolve_port()
    cfg_path = tool_dir / "local_config.json"
    staged = json.loads(read_text(cfg_path))
    staged["server_port"] = port
    cfg_path.write_text(json.dumps(staged), encoding="utf-8")

    for name in ("owner", "other"):
        run([iap, "genkey", name], env=env, cwd=scratch / "keys")
    owner_pem, other_pem = scratch / "keys" / "owner.pem", scratch / "keys" / "other.pem"
    owner_pub = pubkey_of(iap, owner_pem, env) if owner_pem.exists() else None
    if not (owner_pub and other_pem.exists()):
        Fail("could not prepare keys with %s" % iap)
        return 2

    # A board that is claimed and runs an app is made the way a real one is:
    # claim a factory board for the owner key and upload the sketch once. The
    # cases that need it start from a copy of that state.
    claimed_state = scratch / "claimed_template"
    if not prepare_claimed(scratch, claimed_state, iap, binary, owner_pub, owner_pem, port, env, tmp):
        return 2

    # id, key the board trusts ("none" = factory board in its bootloader),
    # user key to place (or None), expect success, line
    cases = [
        ("unclaimed", "none", None, True, "Claimed."),
        ("claimed", owner_pub, owner_pem, True, "The board reset and is answering again"),
        ("wrong-key", owner_pub, other_pem, False, "did not accept the reboot request"),
    ]

    failed = 0
    ip = None
    for cid, trusted, user_key, expect_ok, line in cases:
        factory = trusted == "none"
        Section("%s  -- expecting: %s" % (cid, line))
        user_dest = user_keys / "fw_signing_key.pem"
        if user_dest.exists():
            user_dest.unlink()
        if user_key is not None:
            shutil.copy2(str(user_key), str(user_dest))

        state = scratch / ("state_%s" % cid)
        board_argv = ["--state", state, "--lifetime", "180", "--port", port,
                      "--discovery-port", DISCOVERY_PORT, "--uid", FAKE_UID]
        if factory:
            board_argv.append("--fresh")
        else:
            shutil.copytree(str(claimed_state), str(state))
        board, board_log, handles = start_stand_in(scratch, cid, board_argv)
        t0 = time.time()
        try:
            if not wait_for_serving(board_log):
                Fail("the stand-in never came up on %s" % port)
                failed += 1
                continue
            if ip is None:
                ip = find_stand_in_port(env)
                if ip is None:
                    Fail("discovery did not list the stand-in (uid %s)" % FAKE_UID)
                    return 2
                print("  stand-in listed at %s" % ip)
            out, rc = run(cli_argv("upload", "--fqbn", FQBN, "--input-dir", binary.parent,
                                   "-p", ip, "-l", "network",
                                   "--discovery-timeout", DISCOVERY_TIMEOUT,
                                   "--upload-property", "path=%s" % tool_dir), env=env)
        finally:
            stop_stand_in(board, handles, log=board_log)
            # A failed upload leaves its lock, which keeps discovery silent for 90 s.
            lock = tmp / LOCK_NAME
            if lock.exists():
                lock.unlink()

        blog = read_text(board_log)
        # Read off what the real code printed: the bootloader verified an image
        # of the announced size, the app half took a reboot request, and the app
        # half was serving again after the image.
        got = re.search(r"File size (\d+), checksum", blog)
        verified = blog.find("Checksum and signature OK")
        full = got is not None and int(got.group(1)) == binary.stat().st_size and verified >= 0
        rebooted = "ethernet upload requested" in blog
        came_back = verified >= 0 and blog.find("[stand-in] app ", verified) >= 0
        problems = []
        if line.lower() not in out.lower():
            problems.append("expected line not in arduino-cli output")
        if expect_ok:
            if rc != 0:
                problems.append("arduino-cli exited %d" % rc)
            if factory:
                if not re.search(r"\[stand-in\] root [0-9a-f]{128}", blog):
                    problems.append("board was never claimed")
                if not user_dest.exists():
                    problems.append("no key was generated at %s" % user_dest)
            else:
                if not rebooted:
                    problems.append("board never accepted a reboot request")
                if not came_back:
                    problems.append("board did not come back as an app after the image")
            if not full:
                problems.append("board did not receive the full image")
        else:
            if rc == 0:
                problems.append("arduino-cli exited 0")
            if "Rejected unauthenticated openplc_server_reboot request" not in blog or rebooted:
                problems.append("board did not refuse the reboot request")
            if "File size" in blog:
                problems.append("board was sent a flash command")

        if problems:
            Fail("FAIL (%.0f s): %s" % (time.time() - t0, "; ".join(problems)))
            for ln in nonblank_lines(out)[-15:]:
                print("    " + ln)
            failed += 1
        else:
            Ok("PASS (%.0f s, rc=%d%s)" % (time.time() - t0, rc,
                                         ", board received %d bytes" % binary.stat().st_size
                                         if full else ""))

    Section("result (%.0f s total)" % (time.time() - started))
    if failed:
        Fail("%d of %d case(s) failed" % (failed, len(cases)))
        return 1
    Ok("all %d IDE upload cases behaved as expected" % len(cases))
    return 0


if __name__ == "__main__":
    sys.exit(main())
