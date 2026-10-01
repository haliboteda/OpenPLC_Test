"""Build the bootloader headless, flash it over ST-Link, capture the boot log, and
give the BG1 verdict (is the SDRAM staging buffer usable).

    python tools/flash_bootloader.py                  build + flash + watch
    python tools/flash_bootloader.py --skip-build      flash what is already built
    python tools/flash_bootloader.py --elf <boot.elf>  flash this image, no build
    python tools/flash_bootloader.py --reset-only      only reset and watch; never writes flash
    python tools/flash_bootloader.py --seconds 20      watch longer

⚠️ CubeIDE must be CLOSED for a build: a headless build cannot take a locked
workspace. --reset-only and --skip-build do not care.

⚠️ Debug/ can hold $BOOT's fixture image (PORTTOOL_ENABLE=1),
which is not a bootloader. An image carrying the fixture's command table is
refused.

"""

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from common import (Fail, Ok, Section, Warn, assert_target_reachable,  # noqa: E402
                    cfg, get_cube_ide_exe, get_programmer_cli, open_log_ports,
                    read_log_ports, read_text)

# Sector 0 holds only bootloader code (the root area is in sector 15). Read the
# cap out of the linker script so the two cannot disagree.
DEFAULT_LIMIT = 131072

# "pt.caps" is a command only the fixture image parses; the bootloader
# answers "openplc_server_where_r_y". Both are string literals in the image.
FIXTURE_MARK = b"pt.caps"
BOOTLOADER_MARK = b"openplc_server_where_r_y"


def looks_like_bootloader(image_path):
    """None when the image is a bootloader, otherwise why it is not."""
    data = Path(image_path).read_bytes()
    if FIXTURE_MARK in data:
        return "it is the fixture image (PORTTOOL_ENABLE=1), not a bootloader"
    if BOOTLOADER_MARK not in data:
        return "it does not answer IAP discovery, so it is not this bootloader"
    return None


def linker_limit(boot_repo):
    ld = Path(boot_repo) / "STM32H743IIKX_FLASH.ld"
    if ld.exists():
        m = re.search(r"FLASH\s*\(rx\)\s*:\s*ORIGIN\s*=\s*\S+?,\s*LENGTH\s*=\s*(\d+)K",
                      read_text(ld))
        if m:
            return int(m.group(1)) * 1024
    return DEFAULT_LIMIT


def build(boot_repo):
    """Headless Eclipse build. Returns True to continue, False to stop."""
    Section("Build")
    out = subprocess.run(
        [str(get_cube_ide_exe()), "--launcher.suppressErrors", "-nosplash",
         "-application", "org.eclipse.cdt.managedbuilder.core.headlessbuild",
         "-data", str(cfg.WORKSPACE), "-build", "open_plc_cube_ide/Debug"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, errors="replace").stdout or ""

    for line in out.splitlines():
        if re.search(r"Build Finished|error:|Error ", line):
            print(line)

    m = re.search(r"Build Finished\. (\d+) errors", out)
    if m and m.group(1) != "0":
        Fail("build reported errors - stopping")
        return False

    # A compile error stops make before it ever prints "Build Finished", so the
    # check above sees nothing wrong and the OLD .elf gets flashed -- testing
    # stale firmware while believing it is the new one. Catch make's own failure
    # and the compiler's error lines directly.
    if re.search(r"(?m)^make:.*Error \d+", out) or re.search(r"(?m):\d+:\d+: error:", out):
        Fail("the build failed - NOT flashing (the .elf on disk is from an earlier build)")
        shown = 0
        for line in out.splitlines():
            if re.search(r"error:|Error \d+", line):
                print("    %s" % line)
                shown += 1
                if shown >= 10:
                    break
        return False

    elf = Path(boot_repo) / "Debug" / "open_plc_cube_ide.elf"
    if not elf.exists():
        Fail("no .elf produced - stopping")
        return False

    # Even with a clean build, refuse an .elf that predates this run: it would
    # mean make decided there was nothing to do while the sources say otherwise.
    age_min = (time.time() - elf.stat().st_mtime) / 60.0
    if age_min > 5:
        Warn("the .elf is %.0f min old - make thought nothing needed rebuilding" % age_min)
    return True


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--skip-build", action="store_true")
    ap.add_argument("--reset-only", action="store_true")
    ap.add_argument("--seconds", type=int, default=12)
    ap.add_argument("--ports", nargs="*", default=None)
    ap.add_argument("--elf", default="",
                    help="flash this bootloader ELF instead of building Debug/")
    args = ap.parse_args()

    if args.reset_only or args.elf:
        args.skip_build = True
    ports = args.ports if args.ports else cfg.LOG_PORTS

    boot_repo = cfg.BOOT_REPO
    elf = Path(args.elf) if args.elf else Path(boot_repo) / "Debug" / "open_plc_cube_ide.elf"
    bin_path = elf.with_suffix(".bin")
    cli = get_programmer_cli()

    if not args.skip_build:
        if not build(boot_repo):
            return 1

    if not args.reset_only:
        if not elf.exists():
            Fail("no bootloader image at %s" % elf)
            return 1
        why = looks_like_bootloader(elf)
        if why:
            Fail("refusing to flash %s: %s" % (elf, why))
            return 1

    if bin_path.exists():
        length = bin_path.stat().st_size
        limit = linker_limit(boot_repo)
        print("bin = {:,} B of {:,} usable ({:.1%} used, {:,} B free)".format(
            length, limit, length / limit, limit - length))
        if length > limit:
            Fail("the image no longer fits sector 0")
            return 1

    Section("Target check")
    assert_target_reachable(cli)

    Section("Reset + capture" if args.reset_only else "Flash + capture")
    open_ports = open_log_ports(ports)

    argv = ([str(cli), "-c", "port=SWD", "mode=UR", "-rst"] if args.reset_only
            else [str(cli), "-c", "port=SWD", "mode=UR", "-w", str(elf), "-rst"])
    out = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, errors="replace").stdout or ""
    keep = r"Error|Reset" if args.reset_only else r"Download|verified|Error|Reset"
    for line in out.splitlines():
        if re.search(keep, line):
            print(line)

    buf = read_log_ports(open_ports, args.seconds)
    for k, v in buf.items():
        Section("%s  (%d bytes)" % (k, len(v)))
        if v:
            print(v)

    Section("BG1 verdict")
    allof = "\n".join(buf.values())
    if "SDRAM staging buffer OK" in allof:
        Ok("PASS - staging buffer usable. Next: T1-07 (normal upload).")
    elif "SDRAM SELF-TEST FAILED" in allof:
        Fail("FAIL - SDRAM self-test failed. Fix FMC / power-up sequence before "
             "testing uploads.")
    elif "APP Mod" in allof:
        # MX_FMC_Init() lives in Phase 2 of main(), which only runs when the board
        # stays in the bootloader. A board with a valid application jumps at
        # main.c:185 and never reaches the self-test -- absence of the SDRAM line
        # here means "not reached", not "failed".
        Warn("INCONCLUSIVE - the board booted its application, so Phase 2 never ran.")
        Warn("  The SDRAM self-test only runs when the board stays in the bootloader.")
        Warn("  Hold BOOT0 through the startup window, or use IAPTool to request upload mode.")
    elif not allof.strip():
        Warn("No serial output at all.")
        Warn("  Either every log port was busy (see above), or nothing is wired to UART4.")
        Warn("  SWO/ITM carries the same log if the RS232 route is unavailable.")
    else:
        Warn("Serial output arrived but no SDRAM line - is this the new bootloader?")
    return 0


if __name__ == "__main__":
    sys.exit(main())
