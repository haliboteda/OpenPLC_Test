"""Builds the bootloader headlessly.

    python tools/build_image.py

Size is checked against the linker script rather than a number typed here, for
the same reason case P9 checks paths: the number in the script is the one that
decides, and a copy of it would drift.

Exit 0 = built and fits, 1 = build failed or the image is too big, 2 = setup.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from common import cfg, get_cube_ide_exe, Section, Ok, Warn, Fail, read_text  # noqa: E402

PROJECT = "open_plc_cube_ide/Debug"

# Warnings that are a consequence of a deliberate design choice, matched on an
# exact substring so nothing else hides behind them.
KNOWN_WARNINGS = (
    # .RamFunc puts the flashboot erase routine in .data, which makes that LOAD
    # segment writable and executable. That is the point: the code has to keep
    # running while sector 0 is erased. Nothing marks RAM_D1 execute-never --
    # MPU region 0's sub-region 1 is disabled, so 0x24000000 falls back to the
    # default map, where SRAM is executable.
    # $PROD/docs/modules/M1/FLASHBOOT.md
    "LOAD segment with RWX permissions",
)

# The same project also builds the fixture image (PORTTOOL_ENABLE=1, its own
# linker script). Either leaking into this build makes something that is not a
# bootloader, so both are refused. See $PROD/docs/tables/DECISIONS.md 14.
FIXTURE_MARKER = "PORTTOOL_ENABLE=1: this image is the hardware test tool"
FIXTURE_LD = "STM32H743IIKX_FLASH_PORTTOOL.ld"
BOOT_LD = "STM32H743IIKX_FLASH.ld"


def flash_limit():
    """The bootloader's FLASH region, straight out of the linker script."""
    ld = Path(cfg.BOOT_REPO) / "STM32H743IIKX_FLASH.ld"
    m = re.search(r"FLASH\s*\(rx\)\s*:\s*ORIGIN\s*=\s*\S+?,\s*LENGTH\s*=\s*(\d+)K",
                  read_text(ld))
    if not m:
        Fail("no FLASH length in %s" % ld)
        sys.exit(2)
    return int(m.group(1)) * 1024


def build():
    Section("Build: bootloader")

    argv = [str(get_cube_ide_exe()), "--launcher.suppressErrors", "-nosplash",
            "-application", "org.eclipse.cdt.managedbuilder.core.headlessbuild",
            "-data", str(cfg.WORKSPACE)]
    # Clean: Debug/ may hold fixture objects, and make cannot see the macro moved.
    argv += ["-cleanBuild", PROJECT]

    out = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, errors="replace").stdout or ""

    # A compile error stops make before "Build Finished" is ever printed, so
    # both shapes of failure have to be looked for.
    if re.search(r"(?m)^make:.*Error \d+", out) or re.search(r"(?m):\d+:\d+: error:", out):
        Fail("the build failed")
        for line in out.splitlines():
            if re.search(r"error:|Error \d+|overflowed", line):
                print("    %s" % line)
        return None

    finished = re.findall(r"Build Finished\. (\d+) errors?, (\d+) warnings?", out)
    if not finished:
        Fail("the build never reported finishing - see the log above")
        print(out[-2000:])
        return None
    errors, warnings = finished[-1]
    if errors != "0":
        Fail("%s errors" % errors)
        return None

    if ("-T" + FIXTURE_LD) in out.replace('"', '') or (" " + FIXTURE_LD) in out:
        Fail("the linker was given %s, but this build wanted %s - check the "
             "PLC_LD_SCRIPT default in .settings/org.eclipse.cdt.core.prefs"
             % (FIXTURE_LD, BOOT_LD))
        return None
    if BOOT_LD in out:
        Ok("linked against %s" % BOOT_LD)
    else:
        # Every build here is a clean build, so there is always a link. No
        # linker line naming the script means the -T never arrived - most
        # likely PLC_LD_SCRIPT expanded to nothing.
        Fail("no linker line names %s - did PLC_LD_SCRIPT expand? See "
             "$PROD/docs/build/CUBEMX-RULES.md" % BOOT_LD)
        return None

    if FIXTURE_MARKER in out:
        Fail("this was supposed to be the bootloader, but it carries the fixture "
             "marker - check .cproject for a leftover PORTTOOL_ENABLE")
        return None

    expected = sum(
        1 for line in out.splitlines()
        if any(k in line for k in KNOWN_WARNINGS))
    if int(warnings) > expected:
        Warn("%s warnings (expected %d)" % (warnings, expected))
        for line in out.splitlines():
            if ("warning:" in line
                    and not any(k in line for k in KNOWN_WARNINGS)):
                print("    %s" % line)
    else:
        Ok("0 errors, %s warning(s) - as expected" % warnings)

    binary = Path(cfg.BOOT_REPO) / "Debug" / "open_plc_cube_ide.bin"
    if not binary.exists():
        Fail("no .bin at %s" % binary)
        return None
    size = binary.stat().st_size
    limit = flash_limit()
    if size > limit:
        Fail("%s is %d bytes, over the %d the linker script allows" %
             (binary.name, size, limit))
        return None
    Ok("bootloader: %d bytes, %d to spare in the %d-byte region" %
       (size, limit - size, limit))
    return size


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    return 0 if build() is not None else 1


if __name__ == "__main__":
    sys.exit(main())
