"""The bootloader stand-in's power supply and reset line.

Runs bootstand_boot and bootstand_app in turn, the way resets hand the board
from one image to the other: a firmware reset (exit 3) boots the bootloader
again, a jump to the application (exit 4) starts the application half. The
board's memories persist in --state across all of it; see
$PROD/docs/engineering/BOOTLOADER-STAND-IN.md.

    python bootstand.py --state DIR [--fresh] [--root HEX128] [--old-bootloader]
                        [--port N] [--discovery-port N] [--uid HEX24]
                        [--lifetime SECONDS] [--boot-window SECONDS]

  --fresh          start from erased flash and empty RAMs (a factory board)
  --root           setup only: claim the board for this public key on the first
                   boot, as a takeown would
  --old-bootloader answer getpubkey as v0.1.0-v0.1.2 did (T1-18c)
  --boot-window    silence before each boot, the board's 2 s BOOT0 window

Exits when --lifetime runs out or a half crashes. Killing this process kills
the running half with it (a job object on Windows, a process group elsewhere).

Exit 0 = ran its lifetime, 1 = a half crashed or failed to start.
"""

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXIT_RESET = 3
EXIT_APP = 4


def log(msg):
    print("[stand-in] " + msg, flush=True)


def kill_with_me():
    """Make every child die when this process does, however it dies."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in
                    ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                     "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class EXTENDED(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32.CreateJobObjectW.restype = wintypes.HANDLE
    job = k32.CreateJobObjectW(None, None)
    info = EXTENDED()
    info.BasicLimitInformation.LimitFlags = 0x2000   # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
    return job


def adopt(job, proc):
    if job is None:
        return
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = ctypes.c_void_p
    h = k32.OpenProcess(0x1F0FFF, False, proc.pid)
    k32.AssignProcessToJobObject(ctypes.c_void_p(job), ctypes.c_void_p(h))
    k32.CloseHandle(ctypes.c_void_p(h))


def exe(name, build):
    return str(Path(build) / (name + (".exe" if sys.platform == "win32" else "")))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", required=True)
    ap.add_argument("--build", default=str(HERE / "build"))
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--root")
    ap.add_argument("--old-bootloader", action="store_true")
    ap.add_argument("--port", default="61865")
    ap.add_argument("--discovery-port", default="0")
    ap.add_argument("--uid")
    ap.add_argument("--lifetime", type=float, default=60.0)
    ap.add_argument("--boot-window", type=float, default=2.0)
    args = ap.parse_args()

    state = Path(args.state)
    if args.fresh and state.exists():
        shutil.rmtree(str(state))
    state.mkdir(parents=True, exist_ok=True)

    common = ["--state", str(state), "--port", args.port, "--discovery-port", args.discovery_port]
    if args.uid:
        common += ["--uid", args.uid]

    job = kill_with_me()
    popen_kw = {} if sys.platform == "win32" else {"start_new_session": True}
    deadline = time.time() + args.lifetime
    half, cold = "boot", True
    child = None
    try:
        while time.time() < deadline:
            if half == "boot":
                time.sleep(args.boot_window)
                argv = [exe("bootstand_boot", args.build)] + common
                if cold:
                    argv.append("--cold")
                    if args.root:
                        argv += ["--claim", args.root]
                if args.old_bootloader:
                    argv.append("--old-bootloader")
            else:
                argv = [exe("bootstand_app", args.build)] + common
            cold = False
            child = subprocess.Popen(argv, **popen_kw)
            adopt(job, child)
            while child.poll() is None and time.time() < deadline:
                time.sleep(0.05)
            if child.poll() is None:
                break
            rc = child.returncode
            if rc == EXIT_RESET:
                half = "boot"
            elif rc == EXIT_APP:
                half = "app"
            else:
                log("%s half exited with %d" % (half, rc))
                return 1
        return 0
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.wait()


if __name__ == "__main__":
    sys.exit(main())
