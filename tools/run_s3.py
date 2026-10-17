"""T1-13 -- the boot-time signature check, on its own.

    python3 tools/run_s3.py --bin <app.bin>               corrupt that app on the board, then restore it
    python3 tools/run_s3.py --bin <app.bin> --no-restore  leave the board broken (don't)

T1-11 also exercises the boot-time check, but only as a side effect of an upload
that failed -- and after SDRAM staging landed, a failed upload never touches the
application region at all, so T1-11's boot-time half now proves nothing about an
application that IS installed. This case is the only one that verifies the claim
in C3: the application's signature is re-checked on EVERY boot, against the bytes
actually in flash.

---------------------------------------------------------------------------
THIS CASE BREAKS THE BOARD ON PURPOSE. Read this before running it.
---------------------------------------------------------------------------
It rewrites the application region so the installed application no longer matches
its signature. The board then refuses to boot it until a valid image is flashed
again. --bin is that image, and it is flashed back at the end.

Run order is not negotiable: the restore is proven FIRST (the same image is
flashed and the board is seen to boot it) and only then is anything broken.
Backwards, a bad restore image leaves a board that will not boot and nothing
known-good to recover it with.

Why one byte and not an erase: erasing would leave blank flash, which only shows
the check notices a MISSING application. Flipping one byte in the middle shows
the hash actually covers the content.

Why erase-then-rewrite the whole image rather than poking the single byte: on
STM32H7 flash carries ECC per 256-bit word, and programming a word twice leaves
the stored ECC as the AND of both, which reads back as an uncorrectable error and
faults the CPU. The bootloader would crash while hashing instead of cleanly
reporting a bad signature -- a different failure, and a confusing one. Writing a
full, already-modified image into freshly erased flash avoids it.

Exit 0 = T1-13 passed and the board is back to normal, 1 = failed, 2 = setup.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (board_ip, Section, Ok, Warn, Fail,  # noqa: E402
                    assert_target_reachable, boot_log_next,
                    get_go_bin, get_iap_tool, get_programmer_cli,
                    get_scratch_file, nonblank_lines, read_boot_log_swd,
                    run_capture, run_while_draining, wait_boot_log_swd)

# IAP_APP_ADDRESS = FLASH_BASE | ADDRESS_VECTOR, Core/Inc/usbd_cdc_flash.h:50,58.
APP_ADDR = "0x08020000"
# Bootloader lines, read from the SRAM4 buffer over SWD: the board may stay in
# the bootloader, which has no USB port 1.
APP_MOD = "[sys][BL] ** APP Mod"
ACCEPTED = "[sys][BL] Checksum and signature OK"
REJECTED = "[sys][BL] ** App signature invalid or absent"


def boot_log(cli):
    """Reset over SWD and return what the board logged while booting."""
    mark = boot_log_next(read_boot_log_swd(cli) or [])
    run_capture([cli, "-c", "port=SWD", "mode=UR", "-rst"])
    all_text = wait_boot_log_swd(cli, mark, 8)
    for line in nonblank_lines(all_text):
        print("    | " + line)
    return all_text


def flash_and_boot(what, iap, cli, image_path, ip):
    """Flash the image and report whether the board then boots it.

    ⚠️ DO NOT reset the board when IAPTool exits. IAPTool is done once the last
    byte is sent -- the board is only then verifying the staged image, erasing
    the application region and copying it out of SDRAM, which takes seconds.
    Resetting there lands in the middle of the erase/write and leaves exactly
    the damage this case is otherwise trying to create deliberately.

    So: keep reading the boot log across the whole flash and let the board
    reboot itself, which it does on success. The board's own words are the
    verdict; "File transfer complete" is IAPTool's account of its own sending,
    and says nothing about what the board decided.
    """
    Section(what)
    out_path = get_scratch_file("s3_flash.out")
    err_path = get_scratch_file("s3_flash.err")
    mark = boot_log_next(read_boot_log_swd(cli) or [])

    run_while_draining([iap, "ether", str(image_path), ip], {}, out_path, err_path)
    # The board is still working here. Keep reading until it has written the
    # image and come back up.
    serial = wait_boot_log_swd(cli, mark, 15, until=APP_MOD)
    for line in nonblank_lines(serial):
        print("    | " + line)

    tool = (Path(out_path).read_text(encoding="utf-8", errors="replace")
            + Path(err_path).read_text(encoding="utf-8", errors="replace"))
    if "File transfer complete" not in tool:
        Fail("IAPTool did not finish sending the image")
        for line in tool.splitlines()[-8:]:
            print("    " + line)
        return False
    if ACCEPTED not in serial:
        Fail("the board did not accept the image (no 'Checksum and signature OK')")
        return False
    if APP_MOD not in serial:
        Fail("the board accepted the image but did not start the application")
        return False
    Ok("board accepted the image and booted it")
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bin", required=True)
    ap.add_argument("--ip", default="")
    ap.add_argument("--no-restore", action="store_true")
    args = ap.parse_args()

    ip = args.ip or board_ip()
    if not ip:
        Fail("need --ip (or BOARD_UID in config/machine.py)")
        return 2
    binpath = Path(args.bin).resolve()
    if not binpath.exists():
        Fail("no such image: %s" % args.bin)
        return 2

    cli = get_programmer_cli()
    iap = get_go_bin("IAPTool")
    if not iap.exists():
        iap = get_iap_tool()
    if not Path(iap).exists():
        Fail("no IAPTool to restore with")
        return 2

    image = binpath.read_bytes()
    print("restore image: %s (%d bytes) -> %s" % (binpath, len(image), APP_ADDR))

    assert_target_reachable(cli)

    # ----------------------------------------------- 1. prove the restore ----
    Section("1/4  prove the restore path BEFORE breaking anything")
    if not flash_and_boot("flashing the restore image", iap, cli, binpath, ip):
        Fail("the restore image does not produce a bootable board -- refusing to break anything")
        return 2
    Ok("restore path proven; safe to proceed")

    # -------------------------------------------------- 2. break the app -----
    Section("2/4  corrupting one byte of the installed application")

    offset = len(image) // 2
    corrupt = bytearray(image)
    corrupt[offset] ^= 0xFF
    corrupt_path = Path(get_scratch_file("s3_corrupt.bin"))
    corrupt_path.write_bytes(bytes(corrupt))
    print("  byte %d of %d: 0x%02X -> 0x%02X"
          % (offset, len(image), image[offset], corrupt[offset]))

    # -w erases the sectors it needs before programming, which is what keeps
    # this out of the double-programming ECC trap described at the top.
    wr, _ = run_capture([cli, "-c", "port=SWD", "mode=UR", "-w", str(corrupt_path), APP_ADDR])
    if any(s in wr for s in ("Error", "error occured", "cannot")):
        Fail("writing the corrupted image failed")
        for line in wr.splitlines():
            print("    " + line)
        return 2
    Ok("corrupted image written")

    # --------------------------------------------------- 3. the verdict ------
    Section("3/4  what does the board say now?")
    log = boot_log(cli)

    why = []
    if APP_MOD in log:
        why.append("the board booted the application anyway -- the signature is NOT "
                   "re-checked at boot (C3 is false)")
    if REJECTED not in log:
        why.append("no 'App signature invalid or absent' line")
    if "metadata present" not in log:
        why.append("'metadata present' missing -- the journal lost its metadata too, "
                   "so this is not the pure boot-check case")

    verdict = 0
    if why:
        Fail("S3 FAILED:")
        for w in why:
            Fail("    " + w)
        verdict = 1
    else:
        Ok("S3 PASSED -- metadata present, signature rejected, application not started")
        # Both strings come from the same branch of server_decide() (IAP_server.c),
        # so seeing "no valid application" here is expected, not a second failure.
        print("  ('no valid application' in the UPLOAD banner is the same branch, "
              "not a separate fault)")

    # ------------------------------------------------------ 4. restore -------
    if args.no_restore:
        Section("4/4  restore SKIPPED (--no-restore)")
        Warn("the board will not boot an application until you flash a valid image")
        return 1 if verdict else 0

    Section("4/4  restoring the board")
    if not flash_and_boot("flashing the restore image back", iap, cli, binpath, ip):
        Fail("THE BOARD IS LEFT WITHOUT A BOOTABLE APPLICATION -- flash a valid image "
             "over ethernet or ST-Link")
        return 1
    Ok("board restored and booting normally")

    Section("result")
    if verdict:
        Fail("S3 FAILED (board was restored)")
        return 1
    Ok("S3 passed and the board is back to normal")
    return 0


if __name__ == "__main__":
    sys.exit(main())
