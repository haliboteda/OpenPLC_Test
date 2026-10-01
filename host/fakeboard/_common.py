"""Shared by the suites that drive the real IAPTool against fake_board.py.

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
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "tools"))

from common import (EXE, GOOS_DIR, Fail, Ok, Section, Warn, cfg,  # noqa: E402
                    get_go_bin, have_cmd, nonblank_lines, python_exe,
                    read_text, run_capture)

FAKE_BOARD = HERE / "fake_board.py"
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
    """The port the fake board serves and the staged IAPTool dials."""
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




def start_fake_board(scratch, case_id, argv_tail):
    """Launch the stand-in board with its output captured to a log file.

    Returns (process, log path, open file handles). fake_board.py prints with
    flush=True, which is what makes killing it safe: a block-buffered child would
    lose the very lines a case asserts on.
    """
    log = scratch / ("board_%s.log" % case_id)
    out_fh = open(str(log), "wb")
    err_fh = open(str(log) + ".err", "wb")
    proc = subprocess.Popen([python_exe(), str(FAKE_BOARD)] + [str(a) for a in argv_tail],
                            stdout=out_fh, stderr=err_fh)
    return proc, log, (out_fh, err_fh)


def stop_fake_board(proc, handles, log=None):
    """Stop the stand-in board and make sure it is really gone.

    ⚠️ kill() alone is not enough, and the same trap has
    the same hole -- it just loses the race less often because it is slower.

    Two races, both of which produced a FAIL whose stated reason had nothing to
    do with the case:

    1. The board's listening sockets stay bound until the process actually dies.
       fake_board.py sets SO_REUSEADDR on its TCP socket, so the NEXT case's
       board binds the same port happily and both are listening at once -- which
       of them accepts is undefined. The symptom was a case failing with "board
       was never sent a flash command" while its log held nothing but the startup
       line, because the connections had been served by the previous case's
       process and logged to the previous case's file. wait() closes that window.

    2. IAPTool exits as soon as it has sent the last byte, while the board is
       still printing what it received. Killing it at that moment truncates the
       log the assertions read. settle waits for the log to stop growing first.
    """
    if log is not None:
        settle_log(log)
    proc.kill()
    proc.wait()
    for fh in handles:
        fh.close()


def settle_log(log, quiet=0.25, timeout=3.0):
    """Wait until a log file stops growing, bounded.

    fake_board.py prints with flush=True, so "stopped growing" really does mean
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


def wait_for_listener(port):
    """Wait for the stand-in board rather than sleeping a fixed amount."""
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", int(port)), timeout=1):
                return True
        except OSError:
            time.sleep(0.1)
    return False
