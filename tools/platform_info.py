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
GOOS_DIR = {"windows": "windows", "linux": "linux", "macos": "darwin"}[PLATFORM]     # IAPTool / PortTool build.py output layout
A15_DIR = {"windows": "win", "linux": "linux", "macos": "macosx"}[PLATFORM]          # Arduino15 packages/*/tools/STM32Tools/*/
CUBE_PLUG = {"windows": "win32", "linux": "linux64", "macos": "macos64"}[PLATFORM]   # CubeIDE externaltools plugin suffix

# (plugins dir, headless launcher) relative to a CubeIDE install root. Linux and
# macOS layouts follow ST's installers and are not verified on a real machine.
_CUBE_LAYOUTS = {
    "windows": [("STM32CubeIDE/plugins", "STM32CubeIDE/stm32cubeidec.exe")],
    "linux": [("plugins", "stm32cubeide")],
    "macos": [("STM32CubeIDE.app/Contents/Eclipse/plugins", "STM32CubeIDE.app/Contents/MacOS/STM32CubeIDE"),
              ("Contents/Eclipse/plugins", "Contents/MacOS/STM32CubeIDE")],   # root is the .app itself
}


def cube_layout(root):
    """(plugins dir, launcher) under the CubeIDE install root: the first layout
    whose plugins dir exists, this platform's first; else this platform's own,
    so a caller can name the path it expected."""
    from pathlib import Path
    own = _CUBE_LAYOUTS[PLATFORM]
    others = [l for p, ls in _CUBE_LAYOUTS.items() if p != PLATFORM for l in ls]
    for plugins, launcher in own + others:
        if (Path(root) / plugins).is_dir():
            return Path(root) / plugins, Path(root) / launcher
    return Path(root) / own[0][0], Path(root) / own[0][1]


# The arduino-cli bundled with Arduino IDE 2.x, relative to the IDE install root
# (on macOS that root is the .app's Contents, whose subdirectory is "Resources").
IDE_CLI_REL = ("Resources" if PLATFORM == "macos" else "resources") + \
    "/app/lib/backend/resources/arduino-cli" + EXE


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


# ---------------------------------------------------------------- discovery
# Here rather than in common.py because init_machine.py detects BOARD_UID with
# it before config/machine.py exists.
DISCOVERY_PORT = 56865


def parse_identity(reply):
    """'STM32H743_<UID>_<ROLE>_<core ver>_<app ver>' -> (uid, role), or None."""
    parts = reply.split("_")
    if len(parts) < 3 or parts[0] != "STM32H743":
        return None
    return parts[1], parts[2]


def _ipv4_interfaces():
    """{local IPv4 address: its subnet-directed broadcast} for every interface
    except loopback; stdlib only (GetIpAddrTable on Windows, ioctl elsewhere)."""
    import socket
    import struct
    pairs = []
    if IS_WIN:
        import ctypes
        from ctypes import wintypes

        class ROW(ctypes.Structure):   # MIB_IPADDRROW
            _fields_ = [("dwAddr", wintypes.DWORD), ("dwIndex", wintypes.DWORD),
                        ("dwMask", wintypes.DWORD), ("dwBCastAddr", wintypes.DWORD),
                        ("dwReasmSize", wintypes.DWORD), ("unused1", wintypes.USHORT),
                        ("wType", wintypes.USHORT)]
        get = ctypes.windll.iphlpapi.GetIpAddrTable
        size = wintypes.ULONG(0)
        get(None, ctypes.byref(size), False)
        buf = ctypes.create_string_buffer(size.value)
        if get(buf, ctypes.byref(size), False) != 0:
            return {}
        n = ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD))[0]
        rows = ctypes.cast(ctypes.addressof(buf) + 4, ctypes.POINTER(ROW))
        # dwAddr / dwMask hold the bytes in network order.
        pairs = [(struct.pack("<I", rows[i].dwAddr), struct.pack("<I", rows[i].dwMask))
                 for i in range(n)]
    else:
        import fcntl
        # SIOCGIFADDR / SIOCGIFNETMASK; struct ifreq puts the IPv4 address at
        # bytes 20..24 on both Linux and BSD.
        req_addr, req_mask = ((0xc0206921, 0xc0206925) if PLATFORM == "macos"
                              else (0x8915, 0x891b))
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            for _, name in socket.if_nameindex():
                ifreq = struct.pack("256s", name.encode()[:15])
                try:
                    pairs.append((fcntl.ioctl(s.fileno(), req_addr, ifreq)[20:24],
                                  fcntl.ioctl(s.fileno(), req_mask, ifreq)[20:24]))
                except OSError:
                    continue   # no IPv4 address on this interface
        finally:
            s.close()
    result = {}
    for addr, mask in pairs:
        if addr[0] == 127 or addr == b"\0\0\0\0" or mask == b"\xff\xff\xff\xff":
            continue
        bcast = bytes(a | (~m & 0xff) for a, m in zip(addr, mask))
        result[socket.inet_ntoa(addr)] = socket.inet_ntoa(bcast)
    return result


def discover_boards(seconds, want_uid=None, interval=1.0):
    """Broadcast openplc_discover until `seconds` pass (or want_uid answers).

    Returns {uid: (ip, reply)}. Sends from every local IPv4 address to its
    subnet's broadcast and to 255.255.255.255: Windows sends a limited broadcast
    only through the bound interface and BSD only via the default route, and
    the board's LAN is not necessarily the default route.
    """
    import socket
    import time
    try:
        local = _ipv4_interfaces()
    except Exception:
        local = {}
    socks = []
    for addr in sorted(local) + [None]:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            if addr:
                s.bind((addr, 0))
            s.setblocking(False)
            targets = ["255.255.255.255"] + ([local[addr]] if addr else [])
            socks.append((s, targets))
        except OSError:
            s.close()
    found = {}
    deadline = time.monotonic() + seconds
    try:
        while time.monotonic() < deadline:
            for s, targets in socks:
                for dest in targets:
                    try:
                        s.sendto(b"openplc_discover", (dest, DISCOVERY_PORT))
                    except OSError:
                        pass
            round_end = min(deadline, time.monotonic() + interval)
            while time.monotonic() < round_end:
                for s, _ in socks:
                    try:
                        data, (ip, _port) = s.recvfrom(512)
                    except OSError:
                        continue
                    reply = data.decode("ascii", "replace").strip("\x00\r\n ")
                    ident = parse_identity(reply)
                    if ident:
                        found[ident[0]] = (ip, reply)
                if want_uid and want_uid in found:
                    return found
                time.sleep(0.05)
    finally:
        for s, _ in socks:
            s.close()
    return found
