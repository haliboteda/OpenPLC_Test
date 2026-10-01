"""Platform names and console output, with no dependency on config/machine.py.

common.py re-exports all of it; init_machine.py imports it directly, because it
runs before config/machine.py exists and common.py exits when that file is
missing.
"""

import os
import sys

# sys.platform says "win32" even on 64-bit Windows, and "darwin" for macOS.
# Normalise once, here, so nothing else ever tests sys.platform again.
if sys.platform.startswith("win"):
    PLATFORM = "windows"
elif sys.platform == "darwin":
    PLATFORM = "macos"
else:
    PLATFORM = "linux"

IS_WIN = PLATFORM == "windows"
EXE = ".exe" if IS_WIN else ""

# Three tool families, three different names for the same three platforms.
# Keeping the mapping here is the whole point: no script spells any of them out.
GOOS_DIR = {"windows": "windows", "linux": "linux", "macos": "darwin"}[PLATFORM]     # compile_tool.sh output layout
A15_DIR = {"windows": "win", "linux": "linux", "macos": "macosx"}[PLATFORM]          # Arduino15 packages/*/tools/STM32Tools/*/
CUBE_PLUG = {"windows": "win32", "linux": "linux64", "macos": "macos64"}[PLATFORM]   # CubeIDE externaltools plugin suffix


# ANSI colours: cyan section, green ok, yellow warn, red fail. Windows
# consoles only understand them once virtual terminal processing is on, which
# python does not enable for us; NO_COLOR turns them off everywhere.
def _colour_ok():
    if os.environ.get("NO_COLOR"):
        return False
    if not sys.stdout.isatty():
        return False
    if IS_WIN:
        try:
            import ctypes
            k = ctypes.windll.kernel32
            k.SetConsoleMode(k.GetStdHandle(-11), 7)
        except Exception:
            return False
    return True


_COLOUR = _colour_ok()


# A Windows console on a legacy codepage (GBK here) cannot encode the warning
# signs these docstrings are full of, and argparse writes --help straight to
# stdout without a guard. Without this, `--help` dies with UnicodeEncodeError.
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except (AttributeError, ValueError):
    pass


def _paint(text, code):
    return "\033[%sm%s\033[0m" % (code, text) if _COLOUR else text


def _emit(text):
    """print(), but never crash on a console that cannot encode the text.

    A check whose whole job is to tell you what it found must not be silenced by
    the terminal it happens to run in (P8 once died that way on 2026-08-24), so
    unencodable characters are replaced rather than fatal.
    """
    try:
        print(text)
    except UnicodeEncodeError:
        enc = (sys.stdout.encoding or "ascii")
        print(text.encode(enc, "replace").decode(enc, "replace"))


def Section(t): _emit(""); _emit(_paint("===== " + t, "36"))
def Ok(t):      _emit(_paint(t, "32"))
def Warn(t):    _emit(_paint(t, "33"))
def Fail(t):    _emit(_paint(t, "31"))
