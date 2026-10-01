"""R1-29 -- a full metadata area is reclaimed, and the board recovers.

    python3 tools/run_journal_reclaim.py --bin <app.bin>
    python3 tools/run_journal_reclaim.py --bin <app.bin> --leave 4
    python3 tools/run_journal_reclaim.py --inspect        read and report, change nothing

Filling the area by uploading is not an option: one upload costs 7 slots out of
3840, so it would take about 548 uploads and most of a day. This fills it
directly instead, then drives one real upload and watches for the reclaim.

HOW THE SECTOR IS FILLED, AND WHY NOT BY ERASING IT

The sector holds the current firmware metadata -- app size, SHA-256, signature
and certificate -- which cannot be forged here. So the existing content is read
back first and kept verbatim; synthetic log records are appended after it. The
board keeps its application, and only the free space changes.

The whole 128K is then written as one image, because STM32_Programmer_CLI erases
a sector before writing into it. Writing only the appended part would erase the
metadata that was being preserved. (inject_owner_record.py learned the same
lesson on the bootloader's own sector.)

FILLER FORMAT

One slot, 32 bytes, type 0x4C ('L'). The bootloader no longer knows this type,
and a record it cannot read is what makes it declare the area unusable -- which
is the state R1-29 is about.

Criteria:
    ** Metadata area full - the next successful update reclaims it. **  fill took
    Reclaiming metadata area (<n> slots discarded)                      reclaim ran
    the board boots its application afterwards                          it recovered

Exit 0 = R1-29 holds, 1 = it does not, 2 = the run could not be set up.
"""

import argparse
import hashlib
import struct
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (cfg, Section, Ok, Warn, Fail, close_ports,  # noqa: E402
                    get_programmer_cli, get_scratch_file, open_log_ports,
                    python_exe, read_log_ports)

# Sector 15 is split since 2026-09-21: the first 8 KiB is calibration data,
# the metadata area starts after it. Erasing still takes the whole sector, so
# SECTOR_ADDR is what gets erased while METADATA_ADDR is what gets counted.
# See $PROD/docs/modules/M1/SECTOR-15.md and DECISIONS.md #61.
SECTOR_ADDR = 0x081E0000
SECTOR_BYTES = 0x20000
CALIB_BYTES = 0x2000
STATE_ADDR = SECTOR_ADDR + CALIB_BYTES
SLOT = 32
METADATA_BYTES = SECTOR_BYTES - CALIB_BYTES
TOTAL_SLOTS = METADATA_BYTES // SLOT

REC_BLANK = 0xFF
REC_LOG = 0x4C

EVT_UPDATE_OK = 1
JOURNAL_FULL = "** Metadata area full - the next successful update reclaims it."
RECLAIMING = "Reclaiming metadata area"
APP_MOD = "** APP Mod"


def scan(data):
    """(used_slots, last_log_record_bytes). Mirrors the bootloader's own walk."""
    i = 0
    last_log = None
    while i < TOTAL_SLOTS:
        rec = data[i * SLOT:(i + 1) * SLOT]
        if rec[0] == REC_BLANK:
            break
        slots = rec[1]
        if slots == 0:
            break
        if rec[0] == REC_LOG:
            last_log = rec
        i += slots
    return i, last_log


def make_log_record(prev_digest, event, tick_ms, counter):
    prev = b"\x00" * 12 if prev_digest is None else prev_digest[:12]
    return struct.pack("<BBBBIIII", REC_LOG, 1, event, 0, 0, tick_ms, counter, 0) + prev


def fill(data, leave_free):
    used, last_log = scan(data)
    free = TOTAL_SLOTS - used
    if free <= leave_free:
        return data, used, 0
    prev = hashlib.sha256(last_log).digest() if last_log else None
    out = bytearray(data)
    added = 0
    slot = used
    while TOTAL_SLOTS - slot > leave_free:
        rec = make_log_record(prev, EVT_UPDATE_OK, 1000 + added, 1)
        out[slot * SLOT:(slot + 1) * SLOT] = rec
        prev = hashlib.sha256(rec).digest()
        slot += 1
        added += 1
    return bytes(out), used, added


def read_sector(cli, path):
    """The WHOLE sector, calibration bytes included.

    Not just the metadata half, even though that is all this case fills:
    STM32_Programmer_CLI erases the whole 128 KiB sector before writing any
    part of it, so writing back only the metadata half destroys the
    calibration area -- which is exactly what the carry-over this case is
    supposed to be able to observe. Read it all, put it all back.
    """
    r = subprocess.run([cli, "-c", "port=SWD", "mode=UR", "-r",
                        hex(SECTOR_ADDR), hex(SECTOR_BYTES), str(path)],
                       capture_output=True, text=True, timeout=180)
    return r.returncode == 0 and Path(path).exists()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin", help="signed application image for the upload that triggers the reclaim")
    ap.add_argument("--key",
                    help="owner private key this board is claimed for. Without "
                         "it IAPTool signs with the published root, a claimed "
                         "board refuses the upload, and the reclaim this case "
                         "is about never happens.")
    ap.add_argument("--ip", default=None)
    ap.add_argument("--ports", action="append")
    ap.add_argument("--leave", type=int, default=4,
                    help="free slots to leave; must be under the 9 a metadata "
                         "record needs, or nothing reclaims (default 4)")
    ap.add_argument("--inspect", action="store_true", help="report and change nothing")
    args = ap.parse_args()

    ports = args.ports or list(cfg.LOG_PORTS)
    ip = args.ip or cfg.BOARD_IP
    cli = str(get_programmer_cli())

    Section("R1-29 -- reading the state sector")
    raw = Path(get_scratch_file("journal_before.bin"))
    if not read_sector(cli, raw):
        Fail("could not read the state sector over SWD")
        return 2
    sector = raw.read_bytes()
    calib, data = sector[:CALIB_BYTES], sector[CALIB_BYTES:]
    if len(data) != METADATA_BYTES:
        Fail("read %d bytes, expected %d" % (len(data), METADATA_BYTES))
        return 2

    used, last_log = scan(data)
    Ok("%d/%d slots used, %d free" % (used, TOTAL_SLOTS, TOTAL_SLOTS - used))

    if args.inspect:
        return 0
    if not args.bin:
        Fail("--bin is required: the reclaim only happens inside a metadata write")
        return 2

    Section("filling the journal")
    filled, used, added = fill(data, args.leave)
    if added == 0:
        Warn("already within %d slots of full; nothing appended" % args.leave)
    else:
        Ok("appended %d synthetic log records, leaving %d free" % (added, args.leave))

    out = Path(get_scratch_file("journal_filled.bin"))
    out.write_bytes(calib + filled)

    Section("writing it back")
    # One image for the whole sector, calibration bytes and all: the programmer
    # erases the whole sector before writing any part of it, so anything left
    # out of this image is gone.
    w = subprocess.run([cli, "-c", "port=SWD", "mode=UR", "-w", str(out),
                        hex(SECTOR_ADDR), "-rst"],
                       capture_output=True, text=True, timeout=300)
    if w.returncode != 0:
        Fail("write failed (rc=%d)" % w.returncode)
        for line in w.stdout.splitlines()[-6:]:
            print("  " + line)
        return 2
    Ok("sector written")

    Section("what the board says with a full journal")
    handles = open_log_ports(ports)
    time.sleep(1)
    subprocess.run([cli, "-c", "port=SWD", "mode=UR", "-rst"],
                   capture_output=True, text=True, timeout=120)
    before = read_log_ports(handles, 12)
    close_ports(handles)
    before_text = "\n".join(before.values()) if isinstance(before, dict) else str(before)
    for line in before_text.splitlines():
        print("  " + line)

    if JOURNAL_FULL not in before_text:
        Warn("the board did not report a full journal")

    Section("one real upload, which is where the reclaim happens")
    handles = open_log_ports(ports)
    upload = [python_exe(), str(Path(__file__).with_name("upload_and_watch.py")),
              "--bin", args.bin, "--ip", ip]
    if args.key:
        upload += ["--key", args.key]
    up = subprocess.run(upload, capture_output=True, text=True, timeout=900)
    after = read_log_ports(handles, 10)
    close_ports(handles)
    after_text = (up.stdout or "") + "\n" + (
        "\n".join(after.values()) if isinstance(after, dict) else str(after))

    Section("verdict")
    if RECLAIMING not in after_text:
        Fail("no %r in the log -- the sector was not reclaimed" % RECLAIMING)
        return 1
    for line in after_text.splitlines():
        if RECLAIMING in line:
            Ok(line.strip())

    post = Path(get_scratch_file("journal_after.bin"))
    if read_sector(cli, post):
        used_after, _ = scan(post.read_bytes()[CALIB_BYTES:])
        Ok("journal after the reclaim: %d/%d slots used" % (used_after, TOTAL_SLOTS))

    if APP_MOD in after_text:
        Ok("R1-29 holds: the journal was reclaimed and the board booted its application")
        return 0
    Fail("reclaimed, but the board did not reach %r afterwards" % APP_MOD)
    return 1


if __name__ == "__main__":
    sys.exit(main())
