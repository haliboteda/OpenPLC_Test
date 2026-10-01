"""Shared by everything under tools/. Import it first:

    from common import cfg, Section, Ok, Warn, Fail, get_go_bin, ...

Every test script under tools/ and host/ is Python (user, 2026-09-01). Anything
more than one script needs lives here rather than being copied -- config loading,
toolchain discovery, serial ports, the hands-on prompt, running a child while
draining the ports.

Run it directly to see what this machine resolves to:

    python tools/common.py --probe
"""

import glob
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# ---------------------------------------------------------------- platform
# The platform names and the output helpers live in platform_info.py so that
# init_machine.py, which runs before config/machine.py exists, can share them.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from platform_info import (PLATFORM, IS_WIN, EXE, GOOS_DIR, A15_DIR,  # noqa: E402,F401
                           CUBE_PLUG, Section, Ok, Warn, Fail, _emit)


# ---------------------------------------------------------------- config
def _load_machine():
    """Import config/machine.py, the one file that differs between machines."""
    path = Path(__file__).resolve().parent.parent / "config" / "machine.py"
    if not path.exists():
        print("config/machine.py is missing.", file=sys.stderr)
        print("Generate it -- this machine's paths are detected, not typed:",
              file=sys.stderr)
        print("    python3 tools/init_machine.py        (python on Windows)",
              file=sys.stderr)
        sys.exit(1)
    import importlib.util
    spec = importlib.util.spec_from_file_location("machine", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cfg = _load_machine()


# ---------------------------------------------------------------- files
def read_text(path):
    """Read a whole file, raw.

    newline="" keeps CRLF intact, so a regex capturing to end-of-line does not
    pick up a trailing \\r. A UTF-8 BOM is stripped: left in, it would break a
    pattern anchored at the first character.
    """
    with open(str(path), "r", encoding="utf-8", errors="replace", newline="") as fh:
        text = fh.read()
    return text[1:] if text.startswith("﻿") else text


# ---------------------------------------------------------------- document roots
# The product-level documents live in the AI-Skills checkout, not in any of the
# six product repos. Three checks need to find them (P8, P9 and
# check_status_sync), so the locating happens once, here.
#
# SKILLS_REPO comes from config/machine.py because AI-Skills is shared across
# projects and is not a sibling of the product repos here -- it sits one level
# further out. The two-candidate probe below is the fallback for a config written
# before SKILLS_REPO existed, and is why these are functions, not constants.
def skills_repo():
    """The AI-Skills checkout, or None if this machine has no clone of it."""
    configured = getattr(cfg, "SKILLS_REPO", "")
    if configured and Path(configured).is_dir():
        return Path(configured)
    boot = Path(getattr(cfg, "BOOT_REPO", "") or ".")
    for cand in (boot.parent / "AI-Skills", boot.parent.parent / "AI-Skills"):
        if cand.is_dir():
            return cand
    return None


# Points one level ABOVE docs/, so a citation reads $PROD/docs/tables/STATUS.md. Pointing
# it at docs/ itself made $PROD/docs/path/to/x.md resolve to .../docs/docs/path/to/x.md, which P9
# caught at once.
def docs_repo():
    """The OpenPLC_Docs checkout, or None if this machine has no clone of it."""
    configured = getattr(cfg, "DOCS_REPO", "")
    if configured and Path(configured).is_dir():
        return Path(configured)
    boot = Path(getattr(cfg, "BOOT_REPO", "") or ".")
    cand = boot.parent / "OpenPLC_Docs"
    return cand if cand.is_dir() else None


def prod_docs():
    """$PROD -- where the product-level documents live: what the relationship
    between the repositories is, and what the product as a whole is.

    In its own repository rather than in one of the product repos, because its
    subject is all of them. Every repo's CLAUDE.md points here by name; there is
    no plugin involved -- reading a document needs a path, not a loading
    mechanism.

    Moved out of the AI-Skills checkout on 2026-09-16."""
    return docs_repo()


# ---------------------------------------------------------------- paths
def get_scratch_dir():
    """Scratch files (redirected stdout, oversized test images, phase-1 state).

    tempfile honours TMPDIR/TEMP/TMP and falls back to /tmp, so there is no
    platform test here to get wrong.
    """
    return Path(tempfile.gettempdir())


def get_scratch_file(name):
    return get_scratch_dir() / name


def get_go_bin(name):
    """Where `go build -o Output/<GOOS>/...` puts a binary for THIS host.

    IAPTool is built in its own repo (it is the thing under test); everything
    else -- TestCase -- is built here.
    """
    repo = cfg.IAPTOOL_REPO if name == "IAPTool" else cfg.TEST_REPO
    return Path(repo) / "Output" / GOOS_DIR / (name + EXE)


def get_output_dir():
    """This repo's Output/: test keys, probe images, run logs. Gitignored."""
    return Path(cfg.TEST_REPO) / "Output"


def target_voltage(cli=None):
    """Volts the ST-Link measures on VTREF, or None when SWD cannot reach it.

    This is the only signal here that speaks about POWER rather than about
    software or network state. UDP silence -- from a script or a person --
    also happens when the board is merely busy, or when the local network
    hiccups without the board losing power at all. Measured 2026-09-18:
    "the board stopped answering UDP" was mistaken for "the power went",
    which cannot be told apart without an independent electrical signal.
    """
    cli = str(cli) if cli else str(get_programmer_cli())
    try:
        out = subprocess.run([cli, "-c", "port=SWD", "mode=HOTPLUG"],
                             capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return None
    if re.search(r"No STM32 target found|Error", out, re.I) and \
       not re.search(r"Voltage", out):
        return None
    m = re.search(r"Voltage\s*:\s*([\d.]+)", out)
    return float(m.group(1)) if m else None


def local_ip_for(target_ip):
    """The local IP whose PHYSICAL interface can reach target_ip, or None.

    None means either "let the OS route it normally" (no physical interface
    shares that subnet -- the board is behind a router) or "could not tell"
    (netifquery failed to build or run). Both cases are handled the same way
    by every caller: fall back to an unbound socket.

    Shells out to tools/netifquery rather than reimplementing the
    per-OS physical-vs-virtual classification here in Python. That logic
    already exists in three platform-specific Go files
    (netiface/iface_*.go); writing a fourth copy is how the same
    rule ends up unmaintained in one of its homes. See
    $PROD/docs/tables/DECISIONS.md decision 51.

    A VPN or other virtual adapter holding a better-metric default route is
    exactly what this exists to route around: measured 2026-09-18, an
    unbound socket to a board on the LAN went out through a VPN tunnel
    instead, and on Windows the VPN endpoint completed the TCP handshake and
    then reset it -- indistinguishable from the board itself failing.
    """
    try:
        r = subprocess.run(
            ["go", "run", "./tools/netifquery", target_ip],
            capture_output=True, text=True, timeout=15, cwd=cfg.TEST_REPO,
        )
    except Exception:
        return None
    if r.returncode != 0:
        return None
    ip = r.stdout.strip()
    return ip or None


def _newest(pattern):
    hits = sorted(glob.glob(str(pattern)))
    return Path(hits[-1]) if hits else None


def get_iap_tool():
    """IAPTool ships inside the Arduino board package, one directory per platform.

    Its version is independent of the core's, so resolve both by wildcard: a
    package update must not require editing config/.
    """
    override = getattr(cfg, "IAPTOOL", "")
    if override and Path(override).exists():
        return Path(override)
    a15 = getattr(cfg, "A15", "")
    if not a15:
        Fail("IAPTOOL not set and A15 not set in config/machine.py")
        sys.exit(1)
    hit = _newest(Path(a15) / "packages" / "OpenPLC_Alpha" / "tools" / "STM32Tools"
                  / "*" / A15_DIR / ("IAPTool" + EXE))
    if not hit:
        Fail("IAPTool not found under %s for platform '%s' "
             "(looked in .../STM32Tools/*/%s/)" % (a15, PLATFORM, A15_DIR))
        sys.exit(1)
    return hit


def get_programmer_cli():
    """STM32_Programmer_CLI lives in a versioned plugin directory, so resolve it
    by wildcard and take the newest. Hardcoding the version breaks on every
    CubeIDE update -- which is the class of thing config/ exists to avoid.

    The non-Windows plugin suffixes are ST's documented naming and are NOT
    verified; if the lookup fails on Linux, the printed glob is the thing to
    compare against the real install.
    """
    pattern = (Path(cfg.CUBEIDE) / "STM32CubeIDE" / "plugins"
               / ("com.st.stm32cube.ide.mcu.externaltools.cubeprogrammer.%s_*" % CUBE_PLUG)
               / "tools" / "bin" / ("STM32_Programmer_CLI" + EXE))
    hit = _newest(pattern)
    if not hit:
        Fail("STM32_Programmer_CLI not found. Looked for: %s" % pattern)
        sys.exit(1)
    return hit


def get_cube_ide_exe():
    """The headless launcher, whose name differs per platform."""
    name = "stm32cubeidec.exe" if IS_WIN else "stm32cubeide"
    exe = Path(cfg.CUBEIDE) / "STM32CubeIDE" / name
    if not exe.exists():
        Fail("%s not found at %s" % (name, exe))
        sys.exit(1)
    return exe


# ---------------------------------------------------------------- processes
def have_cmd(name):
    """Is this command on PATH?

    Only for names expected on PATH. Settings that deliberately are NOT on PATH
    (HOST_CC) hold an absolute path instead, so test those with Path.exists().
    """
    import shutil
    return shutil.which(name) is not None


def python_exe():
    """This interpreter, for launching sibling scripts.

    Never the literal "python": that is wrong on any machine where only python3
    exists. sys.executable is correct and is guaranteed to be the interpreter
    already running.
    """
    return sys.executable


def run_capture(argv, cwd=None, empty_stdin=False):
    """Run a program and return (merged stdout+stderr, exit code).

    One string, never wrapped at a console width -- the callers match assertions
    against whole lines.

    empty_stdin gives the child an immediately-empty PIPE on stdin. It must be a
    pipe and not DEVNULL: on Windows
    DEVNULL is NUL, NUL *is* a character device, and Go's os.Stdin.Stat() reports
    ModeCharDevice for it -- so a tool checking "am I attached to a terminal"
    decides yes, prints its prompt, reads EOF and takes the "operator declined"
    branch instead of the "no terminal" branch. A case that needs it asserts
    the latter, and DEVNULL made it fail for a reason that had nothing to do with
    what the case is about.
    """
    # input="" is what opens the pipe; subprocess.run refuses to be given both
    # `stdin` and `input`, so the two cases pass different keyword sets.
    kwargs = {"input": ""} if empty_stdin else {}
    proc = subprocess.run([str(a) for a in argv],
                          cwd=None if cwd is None else str(cwd),
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, errors="replace", **kwargs)
    return proc.stdout or "", proc.returncode


def run_emit(argv, cwd=None):
    """Run a program, pass its output straight through, return its exit code.

    For children whose output belongs in this script's own output.

    Two things have to be right. Ordering: print() is
    block-buffered when stdout is a pipe, so an inherited child would overtake
    lines printed before it -- hence capture-then-write under this script's
    control, with a flush first. And bytes, not text: a MinGW binary printing
    "\\r\\n" to a text-mode stdout emits "\\r\\r\\n", and universal-newline
    translation would turn that stray "\\r" into an extra blank line the child
    never wrote.
    """
    proc = subprocess.run([str(a) for a in argv],
                          cwd=None if cwd is None else str(cwd),
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if proc.stdout:
        sys.stdout.flush()
        sys.stdout.buffer.write(proc.stdout)
        sys.stdout.buffer.flush()
    return proc.returncode


def nonblank_lines(text):
    """`$text -split "\\r?\\n" | Where-Object { $_.Trim() }`.

    Used wherever a failing case dumps a captured log, so the dump keeps the same
    shape on both sides of the comparison.
    """
    return [ln for ln in re.split(r"\r?\n", text) if ln.strip()]


# ---------------------------------------------------------------- serial
def _import_serial():
    """pyserial is the one dependency that is not in the standard library.

    Report it by name rather than dying on an ImportError traceback: a missing
    optional dependency must read as "install this", not as a crash.
    """
    try:
        import serial  # noqa: F401
        return serial
    except ImportError:
        Fail("pyserial is not installed -- serial capture is unavailable.")
        Warn("  pip install pyserial")
        if not IS_WIN:
            Warn("  on Debian:  apt install python3-serial   (or use a venv)")
            Warn("  and make sure the user is in the dialout group for /dev/tty* access.")
        return None


def get_port_holder_hint():
    """Which process is holding a serial port. Neither OS offers a direct answer,
    so this is a best-effort name match against the usual terminals -- enough to
    say "close sscom" instead of "access denied".
    """
    known = ("sscom", "putty", "xshell", "securecrt", "mobaxterm", "ttermpro", "teraterm",
             "realterm", "termite", "hterm", "accessport", "xcom", "uartassist", "arduino",
             "minicom", "picocom", "screen", "cu", "tio")
    try:
        if IS_WIN:
            out = subprocess.run(["tasklist", "/fo", "csv", "/nh"],
                                 capture_output=True, text=True, timeout=10).stdout
            names = [line.split(",")[0].strip('"') for line in out.splitlines() if line]
        else:
            out = subprocess.run(["ps", "-eo", "comm="],
                                 capture_output=True, text=True, timeout=10).stdout
            names = out.split()
    except Exception:
        return None
    hits = sorted({n for n in names for k in known if k in n.lower()})
    return ", ".join(hits) if hits else None


def open_log_ports(ports):
    """Open every port that can be opened; report the ones that cannot and name
    the likely culprit. Returns a dict of name -> Serial.
    """
    serial = _import_serial()
    if serial is None:
        return {}
    open_ports = {}
    for p in ports:
        if not p:
            continue
        try:
            h = serial.Serial(p, cfg.LOG_BAUD, timeout=0.3)
            open_ports[p] = h
            print("listening on %s @ %d" % (p, cfg.LOG_BAUD))
        except Exception as e:
            msg = str(e)
            Warn("cannot open %s: %s" % (p, msg))
            if re.search(r"denied|Permission", msg, re.I):
                who = get_port_holder_hint()
                if who:
                    Warn("  a serial terminal is holding it: %s -- close it and retry" % who)
                if not IS_WIN:
                    Warn("  or the user is not in the dialout group")
    return open_ports


def decode_serial(data):
    """Bytes off a serial port, as text.

    ⚠️ ASCII with '?' for anything above 0x7F.

    Decoding as UTF-8 with errors="replace" instead produces U+FFFD, which is
    wrong here:

      1. U+FFFD cannot be encoded by a GBK console, so printing a capture raised
         UnicodeEncodeError and took the whole script down. Board captures
         routinely carry bytes above 0x7F, so this is not a corner case.

    Found 2026-08-22 by running the serial half for the first time -- nothing had
    ever imported it, and all thirteen board scripts are about to.
    """
    return data.decode("ascii", errors="replace").replace("�", "?")


# The board says this when it came up with BOOT0 held through the startup
# window. It is how a script knows the operator did the gesture: the BOARD
# reports it, so nobody has to walk back to the keyboard to confirm.
LOG_BOOT0_UPLOAD = "UPLOAD Mod ... (BOOT0 held)"


def wait_for_boot0_upload_mode(ports, timeout):
    """Watch the log ports until the board says BOOT0 was held.

    Returns (seen, text). Without this a script races the human: it prints
    "hold BOOT0 now" and then queries the board microseconds later, which on
    an unattended run means the answer is always "not held".
    """
    import time
    open_ports = open_log_ports(ports)
    if not open_ports:
        return False, ""
    text = ""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for h in open_ports.values():
            try:
                n = h.in_waiting
                if n:
                    text += decode_serial(h.read(n))
            except Exception:
                pass
        if LOG_BOOT0_UPLOAD in text:
            close_ports(open_ports)
            return True, text
        time.sleep(0.1)
    close_ports(open_ports)
    return False, text


def read_log_ports(open_ports, seconds, until=None, until_count=1):
    """Drain the given ports for `seconds` and return name -> captured text.

    `until` is a compiled regex (or pattern string): once it has matched
    `until_count` times across everything captured so far, stop early. A
    capture that waits out a fixed window when it already has what it came
    for is the difference between an operator standing there for ten minutes
    and one who is done in twenty seconds -- and a window guessed too short
    loses the event entirely.
    """
    import re
    import time
    if isinstance(until, str):
        until = re.compile(until)
    buf = {k: "" for k in open_ports}
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for k, h in open_ports.items():
            try:
                n = h.in_waiting
                if n:
                    buf[k] += decode_serial(h.read(n))
            except Exception:
                pass
        if until is not None:
            seen = sum(len(until.findall(v)) for v in buf.values())
            if seen >= until_count:
                break
        time.sleep(0.1)
    for h in open_ports.values():
        try:
            h.close()
        except Exception:
            pass
    return buf


# ---------------------------------------------------------------- board
def wait_for_board(ip, timeout=60.0, port=None):
    """Wait until the board answers UDP discovery. Returns True, or False on timeout.

    ⚠️ Needed after every reset that is followed by anything on the network. The
    board takes a second or two to bring the link up and take a DHCP lease, and a
    script that starts talking before then gets "No response, exiting" -- which
    reads exactly like a dead board.

    This asks the same question IAPTool asks, on the same port, so "answered" here
    means the next tool will get an answer too. A plain ICMP ping would come back
    while the IAP server was still not listening.
    """
    import socket
    if port is None:
        port = 56865
    deadline = time.monotonic() + timeout
    payload = b"openplc_server_where_r_y"
    while time.monotonic() < deadline:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.settimeout(1.0)
            s.sendto(payload, (ip, int(port)))
            s.recvfrom(512)
            return True
        except OSError:
            pass
        finally:
            s.close()
    return False


def assert_target_reachable(cli):
    """Refuses to go further when SWD cannot reach the MCU, and says why. A target
    voltage of 0.00V means the board is unpowered or ST-Link VTREF is not wired --
    VTREF being the one people forget while the other three lines are all correct.
    """
    probe = subprocess.run([str(cli), "-c", "port=SWD", "mode=HOTPLUG"],
                           capture_output=True, text=True).stdout
    m = re.search(r"Voltage\s*:\s*(.+)$", probe, re.M)
    volt = m.group(1).strip() if m else "unknown"
    print("target voltage: %s" % volt)
    # Case-insensitive on purpose; matching case-sensitively here
    # would let a differently-cased message through as "target reachable".
    if re.search("No STM32 target found", probe, re.I):
        Fail("SWD cannot reach the MCU.")
        if volt.startswith("0.00"):
            Warn("  0.00V -> board unpowered, or ST-Link VTREF/VDD not wired.")
        if not IS_WIN:
            Warn("  on Linux this is also what a missing ST-Link udev rule looks like.")
        sys.exit(1)


# ---------------------------------------------------------------- probe
def warn_config_platform():
    """Say so when config/machine.py was filled in for the other platform.

    The template ships with a Windows value and a commented-out Linux value for
    every path, and asks you to delete the one you are not on. Copy it on Debian
    without editing and every path stays a Windows path -- which shows up below
    as eight unrelated MISSING lines and one actively wrong hint, telling you to
    check a serial adapter when the real answer is that COM5 is not a device
    name on this machine. First Debian run, 2026-08-20, hit exactly that.

    Prints nothing when the config matches the platform, so a correct machine's
    The ENV step is unchanged.
    """
    named = [(n, getattr(cfg, n, "")) for n in
             ("BOOT_REPO", "CORE_REPO", "TEST_REPO", "IAPTOOL_REPO", "CORE_LIVE", "CUBEIDE", "IDE", "A15")]
    if IS_WIN:
        wrong = [n for n, v in named if isinstance(v, str) and v.startswith("/")]
        other = "Linux"
    else:
        wrong = [n for n, v in named
                 if isinstance(v, str) and (re.match(r'^[A-Za-z]:[\\/]', v) or "\\" in v)]
        other = "Windows"
    if not wrong:
        return
    Warn("  config/machine.py still holds %s paths, but this machine is %s."
         % (other, PLATFORM))
    Warn("    %s" % ", ".join(wrong))
    Warn("    The template carries both; delete the block you are NOT on, or the")
    Warn("    wrong assignment silently wins. Everything below is downstream of this.")


def probe(verbose=True):
    """What this machine actually has. This is selfcheck's ENV step.

    Nothing here fails the run: CubeIDE and a serial port are needed to reach the
    board, not to pass the host-side checks.

    Returns the list of missing things, by label.
    """
    if verbose:
        Section("ENV  this machine")
        print("  %-19s %s   (Python %d.%d.%d)" % (
            "platform", PLATFORM,
            sys.version_info[0], sys.version_info[1], sys.version_info[2]))
        warn_config_platform()

    missing = []

    def show(label, path, why):
        if path and Path(path).exists():
            if verbose:
                print("  %-19s %s" % (label, path))
        else:
            if verbose:
                Warn("  %-19s MISSING - %s" % (label, why))
            missing.append(label)

    def show_cmd(label, cmd, why):
        from shutil import which
        found = which(cmd)
        # which() returns the extension cased as PATHEXT spells it, which is
        # upper case by default -- so this would print go.EXE rather than
        # go.exe, a cosmetic difference in every captured output.
        if found and IS_WIN:
            p = Path(found)
            found = str(p.with_suffix(p.suffix.lower()))
        if found:
            if verbose:
                print("  %-19s %s" % (label, found))
        else:
            if verbose:
                Warn("  %-19s MISSING - %s" % (label, why))
            missing.append(label)

    show("BOOT_REPO", cfg.BOOT_REPO, "bootloader repo; set it in config/machine.py")
    show("CORE_REPO", cfg.CORE_REPO, "Arduino core repo; set it in config/machine.py")
    show("TEST_REPO", cfg.TEST_REPO, "this repo; set it in config/machine.py")
    show("IAPTOOL_REPO", cfg.IAPTOOL_REPO, "IAPTool under test; set it in config/machine.py")
    show("CORE_LIVE", cfg.CORE_LIVE, "install the board package in the Arduino IDE first")
    show_cmd("go", "go", "T1-15 / H3 and every IAPTool build need it")
    show_cmd("python", "python3" if not IS_WIN else "python", "T1-18a-T1-18g / T1-19-T1-20 need it")
    show("arduino-cli", cfg.ARDUINO_CLI, "P4 and command-line app builds need it")
    show("CubeIDE", cfg.CUBEIDE, "needed to build and flash the bootloader, not for the checks below")

    # The programmer and IAPTool are resolved by wildcard, so report what the
    # lookup found rather than what config says -- that is the value the scripts
    # will use.
    hit = _newest(Path(cfg.CUBEIDE) / "STM32CubeIDE" / "plugins"
                  / ("com.st.stm32cube.ide.mcu.externaltools.cubeprogrammer.%s_*" % CUBE_PLUG)
                  / "tools" / "bin" / ("STM32_Programmer_CLI" + EXE))
    if hit:
        if verbose:
            print("  %-19s %s" % ("programmer CLI", hit))
    else:
        if verbose:
            Warn("  %-19s MISSING - no '%s' plugin under CubeIDE" % ("programmer CLI", CUBE_PLUG))
        missing.append("programmer CLI")

    hit = _newest(Path(getattr(cfg, "A15", "")) / "packages" / "OpenPLC_Alpha" / "tools"
                  / "STM32Tools" / "*" / A15_DIR / ("IAPTool" + EXE)) if getattr(cfg, "A15", "") else None
    if hit:
        if verbose:
            print("  %-19s %s" % ("shipped IAPTool", hit))
    else:
        if verbose:
            Warn("  %-19s MISSING - no IAPTool for '%s' under the board package"
                 % ("shipped IAPTool", A15_DIR))
        missing.append("shipped IAPTool")

    if verbose:
        print("  %-19s %s" % ("log ports (config)", ", ".join(cfg.LOG_PORTS)))
        for p in cfg.LOG_PORTS:
            if IS_WIN or not p:
                continue
            # COMn is not a device name here. Saying "check the adapter and the
            # dialout group" for one sends the reader after hardware when the
            # config is what needs editing.
            if re.match(r'^COM\d+$', p, re.I):
                Warn("  %-19s %s is a Windows port name -- config/machine.py still has "
                     "the Windows block" % ("", p))
            elif not Path(p).exists():
                Warn("  %-19s %s does not exist -- check the adapter and the dialout group" % ("", p))

    if verbose:
        if missing:
            Warn("  -> %d thing(s) missing on this machine: %s" % (len(missing), ", ".join(missing)))
            Warn("     see open_plc_cube_ide/CLAUDE.md for what each one is and where to get it")
        else:
            Ok("  everything config points at exists")
    return missing


# ---------------------------------------------------------------- board scripts

def banner(lines):
    """The hands-on prompt. Same shape every time, because the operator scans for
    the icon rather than reading the paragraph. Reasons go OUTSIDE the box; the
    box holds the action and nothing else.

    Goes through _emit: the pineapple is not encodable on a GBK console, and a
    hands-on prompt that dies with UnicodeEncodeError leaves the operator with a
    traceback instead of the instruction.
    """
    _emit("")
    _emit("=" * 68)
    for i, line in enumerate(lines):
        _emit(("  🍍 " if i == 0 else "     ") + line)
    _emit("=" * 68)
    _emit("")
    # Flush: stdout is block-buffered whenever it is not a terminal, and a
    # hands-on prompt sitting in a buffer is a prompt nobody acts on. Found
    # 2026-09-01 running T1-17 with the output piped -- the unplug banner never
    # appeared while the script sat waiting for the unplug.
    sys.stdout.flush()


def tcp_command(ip, port, cmd, timeout=8.0):
    """One request, one reply, then close.

    The board serves a single client, so each exchange opens and closes its own
    connection rather than holding the port. Returns the reply text, or a
    "<<no reply: ...>>" marker the caller can print and fail on.
    """
    import socket
    try:
        with socket.create_connection((ip, int(port)), timeout=timeout) as sk:
            sk.sendall((cmd + "\n").encode("ascii"))
            time.sleep(0.4)
            sk.settimeout(timeout)
            try:
                data = sk.recv(4096)
            except Exception:
                data = b""
        return data.decode("ascii", errors="replace").strip()
    except Exception as e:
        return "<<no reply: %s>>" % e


def run_while_draining(argv, open_ports, out_path, err_path, tail_seconds=0.0):
    """Run a child to completion while emptying the serial ports as it goes.

    Without the draining the driver's buffer overruns on a long upload and the
    interesting lines are exactly the ones lost. `tail_seconds` keeps listening
    after the child exits, which is what catches the board rebooting into the
    application once an upload is accepted.

    Returns (exit_code, {port: text}). The ports are left open for the caller.
    """
    buf = {k: "" for k in open_ports}

    def sip():
        for k, h in open_ports.items():
            try:
                n = h.in_waiting
                if n:
                    buf[k] += decode_serial(h.read(n))
            except Exception:
                pass

    with open(out_path, "wb") as fo, open(err_path, "wb") as fe:
        proc = subprocess.Popen([str(a) for a in argv], stdout=fo, stderr=fe)
        while proc.poll() is None:
            sip()
            time.sleep(0.06)
    deadline = time.monotonic() + tail_seconds
    while time.monotonic() < deadline:
        sip()
        time.sleep(0.06)
    sip()
    return proc.returncode, buf


def close_ports(open_ports):
    for h in open_ports.values():
        try:
            h.close()
        except Exception:
            pass


def emit_file(path):
    """Print a captured child's output file, if it has anything in it."""
    try:
        sys.stdout.write(Path(path).read_text(encoding="utf-8", errors="replace"))
    except OSError:
        pass


if __name__ == "__main__":
    if "--probe" in sys.argv:
        probe()
        sys.exit(0)
    print(__doc__)
