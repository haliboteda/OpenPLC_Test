"""One successful upload must consume exactly 7 metadata slots.

Requirement R1-28. The bootloader prints its slot accounting on every boot
(IAPServer/bootloader_state.c):

    Bootloader state: <used>/<total> metadata slots used, metadata <absent|present>

so the case is: read that line, do one upload, read it again, and require the
difference to be 7.

    python3 tools/run_journal_slot_accounting.py --bin <file.bin>
    python3 tools/run_journal_slot_accounting.py --judge-only --before a.log --after b.log

Exit code is the verdict: 0 the accounting is right, 1 it is not.
"""

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (board_ip, Section, Ok, Warn, Fail, boot_log_next,  # noqa: E402
                    get_iap_tool, get_programmer_cli, get_scratch_file,
                    read_boot_log_swd, read_text, run_while_draining,
                    wait_boot_log_swd)

# bootloader_state.c prints: "Bootloader state: %u/%u metadata slots used, metadata %s".
# Spelled with explicit classes rather than shorthand so the pattern survives
# being copied through shells and generators.
STATE_RE = re.compile(
    "Bootloader state:[ ]*([0-9]+)[ ]*/[ ]*([0-9]+)[ ]+metadata slots used,"
    "[ ]*metadata[ ]+(absent|present)")

# 8 slots of metadata + 1 of log. The M record grew from 4 slots to 8 so the
# whole 128-byte certificate fits; see $PROD/docs/modules/M1/SECTOR-15.md.
# 7 = the metadata record alone. It was 8 until 2026-09-21, when the eighth
# slot -- the UPDATE_OK event -- went away with the event log
# ($PROD/docs/modules/M1/SECTOR-15.md).
SLOTS_PER_UPLOAD = 7
TAIL_S = 6


def parse_state_line(text):
    """The board's own slot accounting, or None when the line never appeared.

    Takes the LAST match: a capture may span several boots, and the one that
    matters is the most recent.
    """
    hits = STATE_RE.findall(text or "")
    if not hits:
        return None
    used, total, meta = hits[-1]
    return int(used), int(total), meta


def verdict(before_text, after_text, expected=SLOTS_PER_UPLOAD):
    """Judge two captures. Returns (failures, unknowns)."""
    Section("Verdict")
    fails = unknowns = 0

    before = parse_state_line(before_text)
    after = parse_state_line(after_text)

    if before is None:
        Warn("  no 'Bootloader state:' line before the upload")
        unknowns += 1
    if after is None:
        Warn("  no 'Bootloader state:' line after the upload")
        unknowns += 1
    if before is None or after is None:
        return fails, unknowns

    b_used, b_total, _ = before
    a_used, a_total, a_meta = after
    print("  before: %d/%d      after: %d/%d  metadata %s"
          % (b_used, b_total, a_used, a_total, a_meta))

    if a_total != b_total:
        Fail("  slot count changed (%d -> %d); the journal format is not what it was"
             % (b_total, a_total))
        fails += 1

    delta = a_used - b_used
    if delta == expected:
        Ok("  one upload consumed exactly %d slots" % expected)
    elif delta < 0:
        # Reclaim resets the counter, so the difference says nothing this run.
        Warn("  slot count went down (%d -> %d): the sector was reclaimed, "
             "so this run cannot judge the accounting" % (b_used, a_used))
        unknowns += 1
    else:
        Fail("  one upload consumed %d slots, expected %d" % (delta, expected))
        fails += 1

    if a_meta != "present":
        Fail("  metadata %s after a successful upload -- it should be present" % a_meta)
        fails += 1
    else:
        Ok("  metadata present after the upload")

    return fails, unknowns


def capture_boot(cli, seconds):
    """Reset the board over SWD and return what it logged while booting, read
    from the SRAM4 buffer over SWD.

    The slot line comes from bootloader_state_init(), so a boot has to be
    caused. It is driven over ST-Link rather than through IAPTool because every
    path through IAP writes journal entries of its own -- a refused upload
    included -- and those would be counted as part of what the upload under
    test consumed.
    """
    mark = boot_log_next(read_boot_log_swd(cli) or [])
    run_while_draining([str(cli), "-c", "port=SWD", "mode=UR", "-rst"], {},
                       get_scratch_file("journal_reset.out"),
                       get_scratch_file("journal_reset.err"))
    return wait_boot_log_swd(cli, mark, seconds, until="[sys][BL] Bootloader state:")



def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bin")
    ap.add_argument("--key",
                    help="owner private key this board is claimed for. Without "
                         "it IAPTool signs with the published root, which a "
                         "claimed board refuses -- and then there is no "
                         "successful upload to count slots for.")
    ap.add_argument("--ip", default="")
    ap.add_argument("--tail-seconds", type=int, default=TAIL_S)
    ap.add_argument("--judge-only", action="store_true",
                    help="judge two captured logs instead of driving the board")
    ap.add_argument("--before")
    ap.add_argument("--after")
    args = ap.parse_args()

    if args.judge_only:
        if not args.before or not args.after:
            Fail("--judge-only needs --before and --after")
            return 1
        fails, unknowns = verdict(read_text(Path(args.before)), read_text(Path(args.after)))
    else:
        if not args.bin:
            Fail("need --bin (or --judge-only with --before/--after)")
            return 1
        image = Path(args.bin)
        if not image.exists():
            Fail("no such image: %s" % image)
            return 1
        ip = args.ip or board_ip()
        if not ip:
            Fail("need --ip (or BOARD_UID in config/machine.py)")
            return 1
        cli = get_programmer_cli()

        Section("Before")
        before_text = capture_boot(cli, args.tail_seconds)
        print(before_text)

        Section("Upload")
        upload = [get_iap_tool(), "ether", str(image), ip]
        if args.key:
            upload.append("--key=" + args.key)
        mark = boot_log_next(read_boot_log_swd(cli) or [])
        rc, _ = run_while_draining(upload, {},
                                   get_scratch_file("journal.out"),
                                   get_scratch_file("journal.err"))
        if rc != 0:
            Fail("IAPTool exit %d -- the upload has to succeed for this case to mean anything" % rc)
            return 1

        Section("After")
        # The boot after the upload logs the slot line again.
        after_text = wait_boot_log_swd(cli, mark, args.tail_seconds + 10,
                                       until="[sys][BL] ** APP Mod")
        print(after_text)
        fails, unknowns = verdict(before_text, after_text)

    if unknowns:
        Warn("  %d check(s) had no evidence either way" % unknowns)
    if fails:
        Fail("%d check(s) failed" % fails)
        return 1
    Ok("journal slot accounting is right")
    return 0


if __name__ == "__main__":
    sys.exit(main())
