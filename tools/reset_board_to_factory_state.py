"""Put a board back into the state it leaves the factory in, and prove it got there.

    python tools/reset_board_to_factory_state.py                   erase, flash, verify
    python tools/reset_board_to_factory_state.py --elf <boot.elf>  flash this image
    python tools/reset_board_to_factory_state.py --check-only      verify only; never writes
    python tools/reset_board_to_factory_state.py --seconds 20      watch the boot log longer

Factory state (decision 72): the bootloader on an otherwise blank chip, plus the
board's calibration values. No root, no application, no firmware metadata.
Every end-to-end path starts here, so a path's result means nothing unless the
starting point is known -- which is why this refuses to report PASS on log
evidence alone.

⚠️ THIS ERASES THE WHOLE CHIP except the calibration values, which are read out
first and written back. Ownership and application are gone afterwards.

The verdict needs BOTH kinds of evidence, because either alone can lie:

  flash reads  -- read back over SWD; independent of the serial port entirely
  boot log     -- what the bootloader says about itself

An empty capture is INCONCLUSIVE, never PASS: a log that was never captured
looks exactly like a log that said nothing.
"""

import argparse
import re
import struct
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from common import (Fail, Ok, Section, Warn, assert_target_reachable,  # noqa: E402
                    cfg, get_programmer_cli, get_scratch_file, open_log_ports,
                    read_log_ports)
from flash_bootloader import looks_like_bootloader  # noqa: E402

# Sector 15 layout: $PROD/docs/modules/M1/SECTOR-15.md; must match
# $BOOT/IAPServer/bootloader_state.c and owner_slot.h.
CALIB_BASE = 0x081E0000
CALIB_SIZE = 8 * 1024
ROOT_AREA_BASE = 0x081E2000
ROOT_AREA_SIZE = 8 * 1024
META_BASE = 0x081E4000
MARKER_ADDR = 0x081FFFE0
MARKER_MAGIC = 0x4C353153          # "S15L", IAP_MARKER_MAGIC
MARKER_LAYOUT = 1
APP_BASE = 0x08020000

# What the bootloader prints on a board with no root and no firmware metadata
# ($BOOT/IAPServer/owner_slot.c, bootloader_state.c).
LOG_NO_ROOT = "Owner slot: empty - no root"
LOG_METADATA_EMPTY = re.compile(r"Bootloader state: 0/\d+ metadata slots used, metadata absent")
# Proves the capture worked at all.
LOG_CAPTURE_PROOF = "Bootloader state:"


def read_words(cli, addr, nbytes):
    """Read `nbytes` bytes over SWD as 32-bit words. Returns a list of ints, or None.

    ⚠️ STM32_Programmer_CLI's -r32 length is in BYTES, not words -- passing a
    word count silently reads a quarter of the region and reports it blank.
    """
    out = subprocess.run(
        [str(cli), "-c", "port=SWD", "mode=HOTPLUG", "-r32", hex(addr), hex(nbytes)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, errors="replace").stdout or ""
    words = []
    for line in out.splitlines():
        m = re.match(r"^0x[0-9A-Fa-f]{8}\s*:\s*(.*)$", line.strip())
        if m:
            words.extend(int(w, 16) for w in m.group(1).split())
    return words if words else None


def read_bytes(cli, addr, nbytes):
    """The same read as bytes, or None if the whole region did not come back."""
    words = read_words(cli, addr, nbytes)
    if words is None or len(words) * 4 < nbytes:
        return None
    return struct.pack("<%dI" % (nbytes // 4), *words[:nbytes // 4])


def region_is_blank(cli, addr, nbytes, what):
    """True when every word reads back as erased flash."""
    got = read_words(cli, addr, nbytes)
    if got is None:
        Fail("  %-22s could not be read over SWD" % what)
        return False
    bad = [(i, w) for i, w in enumerate(got) if w != 0xFFFFFFFF]
    if bad:
        i, w = bad[0]
        Fail("  %-22s NOT blank: %d of %d words written, first at +0x%X = 0x%08X"
             % (what, len(bad), len(got), i * 4, w))
        return False
    Ok("  %-22s blank (%d words all 0xFFFFFFFF)" % (what, len(got)))
    return True


def snapshot(cli, title):
    """Report what is in the regions a factory board has empty. Returns their
    blankness, so the caller can show what the erase actually changed."""
    Section(title)
    root = region_is_blank(cli, ROOT_AREA_BASE, ROOT_AREA_SIZE, "root area")
    app = region_is_blank(cli, APP_BASE, 256, "application region")
    meta = region_is_blank(cli, META_BASE, 256, "metadata area")
    return root, app, meta


def run_cli(cli, *argv):
    """One STM32_Programmer_CLI call over SWD. True when it reported no error."""
    out = subprocess.run([str(cli), "-c", "port=SWD", "mode=UR"] + list(argv),
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, errors="replace").stdout or ""
    for line in out.splitlines():
        if re.search(r"Download|verified|Erasing|erased|Error|Reset", line):
            print(line)
    return "Error" not in out


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--check-only", action="store_true",
                    help="verify the current state; never erase or flash")
    ap.add_argument("--elf", default="",
                    help="bootloader ELF to flash (default: $BOOT/Debug)")
    ap.add_argument("--seconds", type=int, default=12)
    ap.add_argument("--ports", nargs="*", default=None)
    args = ap.parse_args()

    ports = args.ports if args.ports else cfg.LOG_PORTS
    elf = Path(args.elf) if args.elf else Path(cfg.BOOT_REPO) / "Debug" / "open_plc_cube_ide.elf"
    cli = get_programmer_cli()

    Section("Target check")
    assert_target_reachable(cli)

    before = snapshot(cli, "Before" if not args.check_only else "Current contents")
    calib = read_bytes(cli, CALIB_BASE, CALIB_SIZE)
    if calib is None:
        Fail("could not read the calibration area over SWD - stopping")
        return 1
    has_calib = calib != b"\xFF" * CALIB_SIZE
    print("  calibration area       %s" % ("written - kept" if has_calib else "blank"))

    if not args.check_only:
        if not elf.exists():
            Fail("no bootloader .elf at %s" % elf)
            Fail("  build it first, or pass --elf")
            return 1
        why = looks_like_bootloader(elf)
        if why:
            Fail("refusing to flash %s: %s" % (elf, why))
            return 1
        calib_file = Path(get_scratch_file("factory_calib.bin"))
        calib_file.write_bytes(calib)

        Section("Mass erase")
        Warn("erasing the whole chip - ownership and application are going away")
        if not run_cli(cli, "-e", "all"):
            Fail("the erase reported an error - stopping before flashing")
            return 1

        # Erase before flash, checked separately: a bootloader written on top of
        # a failed erase still boots and still prints the right lines, so the log
        # cannot tell the two apart. This is the only moment the difference is
        # visible.
        if not all(snapshot(cli, "After erase, before flashing")):
            Fail("the chip is not blank after a mass erase - stopping")
            return 1

        Section("Flash bootloader")
        if not run_cli(cli, "-w", str(elf)):
            Fail("the download reported an error")
            return 1
        if has_calib:
            # Sector 15 is blank after the mass erase, so writing the 8 KiB back
            # (which erases the sector first) loses nothing.
            Section("Write the calibration values back")
            if not run_cli(cli, "-w", str(calib_file), hex(CALIB_BASE)):
                Fail("writing the calibration values back failed; they are saved at %s"
                     % calib_file)
                return 1

    Section("Reset + capture boot log")
    open_ports = open_log_ports(ports)
    subprocess.run([str(cli), "-c", "port=SWD", "mode=UR", "-rst"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    buf = read_log_ports(open_ports, args.seconds)
    log = "\n".join(buf.values())
    for k, v in buf.items():
        Section("%s  (%d bytes)" % (k, len(v)))
        if v:
            print(v)

    # ---------------------------------------------------------------- verdict
    Section("Factory-state verdict")

    root_blank = region_is_blank(cli, ROOT_AREA_BASE, ROOT_AREA_SIZE, "root area")
    app_blank = region_is_blank(cli, APP_BASE, 256, "application region")
    meta_blank = region_is_blank(cli, META_BASE, 256, "metadata area")
    calib_ok = read_bytes(cli, CALIB_BASE, CALIB_SIZE) == calib
    (Ok if calib_ok else Fail)("  %-22s %s" % ("calibration area",
                                               "unchanged" if calib_ok else "CHANGED"))
    # The bootloader writes the layout marker on the first boot of a factory
    # sector; its presence says this bootloader accepted the sector's layout.
    marker = read_words(cli, MARKER_ADDR, 8) or []
    marker_ok = marker[:2] == [MARKER_MAGIC, MARKER_LAYOUT]
    (Ok if marker_ok else Fail)("  %-22s %s" % ("layout marker",
                                                "written by the bootloader" if marker_ok
                                                else "missing"))

    print("")
    no_root = LOG_NO_ROOT in log
    meta_empty = bool(LOG_METADATA_EMPTY.search(log))
    captured = LOG_CAPTURE_PROOF in log
    if not captured:
        Warn("  boot log            NOT captured (%d bytes)" % len(log))
    else:
        Ok("  boot log            captured")
        for found, label, want in ((no_root, "no root", LOG_NO_ROOT),
                                   (meta_empty, "metadata area empty",
                                    LOG_METADATA_EMPTY.pattern)):
            if found:
                Ok("  %-19s yes" % label)
            else:
                Fail("  %-19s NO - expected %r" % (label, want))

    print("")
    flash_ok = root_blank and app_blank and meta_blank and calib_ok and marker_ok
    log_ok = captured and no_root and meta_empty

    if flash_ok and log_ok:
        Ok("PASS - factory state, confirmed by flash reads AND the boot log.")
        print("       Before this run: root area %s, app %s, metadata %s."
              % tuple("blank" if b else "written" for b in before))
        return 0
    if flash_ok and not captured:
        Warn("INCONCLUSIVE - flash reads say factory state, but no boot log arrived.")
        Warn("  A log that was never captured looks exactly like a log that said")
        Warn("  nothing, so this is not a PASS. Check the log ports listed above.")
        return 2
    Fail("FAIL - this board is NOT in factory state.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
