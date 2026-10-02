"""Shared by the suites that drive the real IAPTool against the bootloader
stand-in ($TEST/host/bootstand, the real bootloader code built for the PC).

Everything below used to be copied into each script by hand. That is the shape
treats as a defect: two copies drift, and the port is the moment to stop
carrying them. Anything here that only one suite needs does not belong here.

The leading underscore keeps `import _common` from colliding with tools/common.py,
which is on sys.path ahead of this directory.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "tools"))

from common import (EXE, GOOS_DIR, Fail, Ok, Section, Warn, cfg,  # noqa: E402
                    get_go_bin, have_cmd, nonblank_lines, python_exe,
                    read_text, run_capture)

STAND_IN = HERE.parent / "bootstand"
STAND_IN_BUILD = STAND_IN / "build"
# Not the product port 56865: something else on a bench may hold it (on the
# development PC, airtcp binds 56865/TCP). The staged IAPTool is given the same
# port, so the two cannot drift. See HOW-TO-RUN-TESTS.md, host/fakeboard.
TEST_PORT = "61865"
# The board package's discovery tool always asks this UDP port
# ($CORE_REPO/tools/discovery/network_discovery.go, discoveryPort).
DISCOVERY_PORT = "56865"


def parse_pubkey(text):
    """The public key IAPTool genkey printed ("Public key: <128 hex>"), or ""."""
    m = re.search(r"Public key: ([0-9a-fA-F]{128})", text)
    return m.group(1).lower() if m else ""


def isolated_env(scratch):
    """An environment whose user config dir is inside scratch, so the tool never
    reads or writes the real user's key at the default location."""
    home = scratch / "userhome"
    home.mkdir(exist_ok=True)
    return dict(os.environ, APPDATA=str(home), XDG_CONFIG_HOME=str(home),
                HOME=str(home))


def user_key_path(env):
    """Where IAPTool looks for, and generates, its default key under env
    (Go's os.UserConfigDir()/openplc/keys/fw_signing_key.pem)."""
    if sys.platform == "win32":
        base = Path(env["APPDATA"])
    elif sys.platform == "darwin":
        base = Path(env["HOME"]) / "Library" / "Application Support"
    else:
        base = Path(env["XDG_CONFIG_HOME"])
    return base / "openplc" / "keys" / "fw_signing_key.pem"


def run_env(argv, env, cwd=None):
    """run_capture with an explicit environment: (merged output, exit code)."""
    proc = subprocess.run([str(a) for a in argv],
                          cwd=None if cwd is None else str(cwd), env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, errors="replace")
    return proc.stdout or "", proc.returncode


def resolve_port():
    """The port the stand-in serves and the staged IAPTool dials."""
    return TEST_PORT


def build_iap_tool():
    """The locally built IAPTool, building it first if it is not there yet.

    Deliberately not get_iap_tool(): these suites test the tool in this working
    tree, not the copy shipped inside the board package.
    """
    iap_tool = get_go_bin("IAPTool")
    if iap_tool.exists():
        return iap_tool
    Warn("IAPTool not built, building it now")
    proc = subprocess.run(["go", "build", "-o",
                           "Output/%s/IAPTool%s" % (GOOS_DIR, EXE), "."],
                          cwd=str(cfg.IAPTOOL_REPO))
    if proc.returncode != 0 or not iap_tool.exists():
        Fail("cannot build IAPTool")
        sys.exit(2)
    return iap_tool


def stage_iap_tool(scratch, iap_tool, port):
    """Put a copy of IAPTool in a scratch directory whose config names no key.

    IAPTool resolves local_config.json and its fallback keys/ directory relative
    to its own executable, not the working directory. The "host has no private
    key" cases are therefore unreachable while running the checked-out copy --
    omitting --key just falls back to the signing_key in the repo's config, which
    is how the first version of run-cases silently tested nothing. It also keeps
    the run from depending on whatever the checked-out config happens to say.

    The config is written without a BOM: a BOM-writing editor's UTF-8
    utf8 emits one and Go's json.Unmarshal rejects it, so IAPTool would exit
    before doing anything -- which reads as a broken tool rather than a broken
    config file.
    """
    iap_run = scratch / ("IAPTool" + EXE)
    shutil.copy2(str(iap_tool), str(iap_run))
    scratch_cfg = {
        "server_port": port,
        "signing_key": "",
    }
    (scratch / "local_config.json").write_text(json.dumps(scratch_cfg),
                                               encoding="utf-8")
    return iap_run


def fixed_bytes(n, step, offset):
    """Filler with no meaning: nothing on either side inspects the image content
    in the phase under test, and fixed content keeps the run reproducible."""
    return bytes((i * step + offset) % 256 for i in range(n))




def build_stand_in():
    """Configures and builds the stand-in from the bootloader and board package
    repos named in config/machine.py. Exits 2 when that is not possible, since
    no case can run without it.

    cmake and ninja are looked up on PATH and then beside HOST_CC, where a
    MinGW install keeps them."""
    host_cc = getattr(cfg, "HOST_CC", "") or ""
    env = dict(os.environ)
    if host_cc:
        env["PATH"] = str(Path(host_cc).parent) + os.pathsep + env.get("PATH", "")
    cmake = shutil.which("cmake", path=env["PATH"])
    if cmake is None or not host_cc:
        Fail("cmake or HOST_CC not found - the stand-in cannot be built")
        sys.exit(2)
    gen = ["-G", "Ninja"] if shutil.which("ninja", path=env["PATH"]) else []
    configure = [cmake, "-S", str(STAND_IN), "-B", str(STAND_IN_BUILD)] + gen + [
        "-DCMAKE_C_COMPILER=%s" % host_cc,
        "-DBOOT_ROOT=%s" % Path(cfg.BOOT_REPO).as_posix(),
        "-DCORE_ROOT=%s" % Path(cfg.CORE_REPO).as_posix()]
    for argv in (configure, [cmake, "--build", str(STAND_IN_BUILD)]):
        proc = subprocess.run(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, errors="replace")
        if proc.returncode != 0:
            Fail("building the stand-in failed:")
            print(proc.stdout)
            sys.exit(2)


def start_stand_in(scratch, case_id, argv_tail):
    """Launch the stand-in with its output captured to a log file.

    Returns (process, log path, open file handles). Its state (flash, RAMs)
    lives in scratch/state_<case_id> unless argv_tail names another --state.
    """
    log = scratch / ("board_%s.log" % case_id)
    out_fh = open(str(log), "wb")
    err_fh = open(str(log) + ".err", "wb")
    argv = [python_exe(), str(STAND_IN / "bootstand.py"), "--build", str(STAND_IN_BUILD)]
    tail = [str(a) for a in argv_tail]
    if "--state" not in tail:
        argv += ["--state", str(scratch / ("state_%s" % case_id))]
    proc = subprocess.Popen(argv + tail, stdout=out_fh, stderr=err_fh)
    return proc, log, (out_fh, err_fh)


def stop_stand_in(proc, handles, log=None):
    """Stop the stand-in and make sure it is really gone.

    Two races, both of which once produced a FAIL whose stated reason had
    nothing to do with the case:

    1. Listening sockets stay bound until the process dies, and SO_REUSEADDR
       lets the next case bind the same port while they do -- which of the two
       accepts is then undefined. wait() closes that window; the supervisor's
       job object takes the running half down with it.
    2. IAPTool exits as soon as it has sent the last byte, while the board is
       still printing what it did with it. settle_log waits for the log first.
    """
    if log is not None:
        settle_log(log)
    proc.kill()
    proc.wait()
    for fh in handles:
        fh.close()


def settle_log(log, quiet=0.25, timeout=3.0):
    """Wait until a log file stops growing, bounded.

    The stand-in writes unbuffered, so "stopped growing" really does mean
    "has written everything it is going to write" -- there is no buffer left to
    lose. Returns when quiet seconds pass with no new bytes, or at timeout.
    """
    deadline = time.time() + timeout
    last = -1
    stable_since = None
    while time.time() < deadline:
        size = log.stat().st_size if log.exists() else 0
        if size != last:
            last = size
            stable_since = time.time()
        elif stable_since is not None and (time.time() - stable_since) >= quiet:
            return
        time.sleep(0.05)


def wait_for_serving(log, timeout=15.0):
    """Wait until the stand-in says a half is serving the network. Not a TCP
    probe: the application half has no TCP listener, and a probe connection
    would be a session the bootloader half has to deal with."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if log.exists() and " serving on " in read_text(log):
            return True
        time.sleep(0.1)
    return False
