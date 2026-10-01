"""Builds a probe application whose boot banner names its version.

    python3 tools/build_probe_image.py --ver v1
    python3 tools/build_probe_image.py --ver v2

The five-path end-to-end plan needs two images that differ in something a
script can judge: every path has to show "the upgrade took", and the only
honest evidence is the banner changing from one to the other.

Why this is a script and not a command to remember: the five images built
before 2026-09-21 printed byte-identical banners, so none of them could serve,
and there was no recipe anywhere -- they were built by hand and their
differences lived only in their filenames.

Two things it refuses to hand over a broken image for:

  * the vector table must land at IAP_APP_ADDRESS. --build-property REPLACES
    the flag list rather than appending, so dropping VECT_TAB_OFFSET produces
    an image that flashes and verifies, then faults on the jump -- only BOOT0
    gets the board back. Measured 2026-09-18.
  * the version string must actually be in the binary. A macro that did not
    reach the compiler would leave the banner unchanged, and the upgrade
    criterion would then pass or fail for reasons nobody could see.

--ver stamps the banner only. Both images keep the sketch's own
OPENPLC_APP_VERSION(1, 0, 0), because the five-path test proves that each upload
path works, not that the version gate bites -- and a path that re-flashes an
earlier image would be refused if the versions differed. Only the .bin is handed
over anyway, so the .version file the gate reads never reaches it.

Exit 0 = built and checked, 1 = something was wrong with it, 2 = setup missing.
"""

import argparse
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from common import Fail, Ok, Section, cfg, get_output_dir, get_scratch_dir, run_capture  # noqa: E402

SKETCH = HERE.parent / "onboard" / "iap_probe"
FQBN = ("OpenPLC_Alpha:stm32:OPEN-PLC:pnum=PLC_H743,usb=CDCgen,xusb=FS,"
        "upload_method=cdcMethod,knxrole=dual_device")

# Where the app's vector table has to land. The linker gets it from
# build.flash_offset; P15 keeps that value 1024-aligned, and this keeps a given
# build from silently landing somewhere else.
EXPECTED_ISR_VECTOR = 0x08020000

OUT_DIR = get_output_dir() / "probe-images"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ver", required=True,
                    help="the version string to stamp into the boot banner, e.g. v1")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    if not re.fullmatch(r"[A-Za-z0-9._-]{1,16}", args.ver):
        Fail("--ver must be 1-16 chars of [A-Za-z0-9._-]; it goes into a C string")
        return 2
    cli = getattr(cfg, "ARDUINO_CLI", "")
    cli_cfg = getattr(cfg, "ARDUINO_CLI_CONFIG", "")
    if not cli or not Path(cli).exists():
        Fail("arduino-cli not found; set ARDUINO_CLI in config/machine.py")
        return 2

    Section("building %s" % args.ver)
    build_path = Path(get_scratch_dir()) / ("probe_" + args.ver)
    shutil.rmtree(str(build_path), ignore_errors=True)

    # Both defines in one string: --build-property replaces the whole flag
    # list, so VECT_TAB_OFFSET has to ride along or the image links wrong.
    # PROBE_VER goes over as a BARE token -- quotes do not survive the trip
    # into the compiler command line; the sketch stringifies it.
    flags = '-DVECT_TAB_OFFSET={build.flash_offset} -DPROBE_VER=%s' % args.ver
    out, rc = run_capture([cli, "compile",
                           "--config-file", cli_cfg, "--fqbn", FQBN,
                           "--build-property", "compiler.c.extra_flags=" + flags,
                           "--build-property", "compiler.cpp.extra_flags=" + flags,
                           "--build-path", str(build_path), str(SKETCH)])
    if rc != 0:
        Fail("did not compile:")
        for line in re.split(r"\r?\n", out)[-15:]:
            if line.strip():
                print("    %s" % line)
        return 1
    Ok("compiled")

    Section("where the vector table landed")
    maps = list(build_path.glob("*.map"))
    if not maps:
        Fail("no .map -- cannot tell where the vector table went, refusing")
        return 1
    where = None
    for line in maps[0].read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"^\.isr_vector\s+0x0*([0-9a-fA-F]+)\s+0x([0-9a-fA-F]+)", line)
        if m:
            where, size = int(m.group(1), 16), int(m.group(2), 16)
            break
    if where is None:
        Fail("the map does not name .isr_vector, refusing to hand this over")
        return 1
    if where != EXPECTED_ISR_VECTOR:
        Fail("vector table at 0x%08X, expected 0x%08X" % (where, EXPECTED_ISR_VECTOR))
        Fail("   this image would flash and verify, then fault on the jump")
        Fail("   VECT_TAB_OFFSET was probably dropped from the build properties")
        return 1
    Ok("0x%08X, %d bytes" % (where, size))

    Section("is the version really in the image")
    bins = list(build_path.glob("*.ino.bin"))
    if not bins:
        Fail("no .bin produced")
        return 1
    blob = bins[0].read_bytes()
    banner = ("IAP_PROBE_APP up " + args.ver).encode("ascii")
    if banner not in blob:
        Fail("the banner %r is not in the binary" % banner.decode())
        Fail("   the macro did not reach the compiler, so both images would")
        Fail("   print the same thing and the upgrade criterion would be blind")
        return 1
    Ok("found %r" % banner.decode())

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    dest = Path(args.out) if args.out else OUT_DIR / ("iap_probe_%s.bin" % args.ver)
    # --out may name a directory that does not exist yet; the compile is the
    # expensive part and losing it to a missing parent is pure waste.
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(str(bins[0]), str(dest))
    shutil.rmtree(str(build_path), ignore_errors=True)

    Section("result")
    Ok("%s  (%d bytes)" % (dest, dest.stat().st_size))
    print("  the board prints  IAP_PROBE_APP up %s  on both channels at boot" % args.ver)
    return 0


if __name__ == "__main__":
    sys.exit(main())
