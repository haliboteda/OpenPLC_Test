"""Put a hand-made owner record into the board's root area, for testing the
bootloader's record handling (requirement R2-02, module M2).

    python3 tools/inject_owner_record.py                  one record, generation 1
    python3 tools/inject_owner_record.py --generation 7   pick the generation
    python3 tools/inject_owner_record.py --cleared        a factory-reset record
    python3 tools/inject_owner_record.py --corrupt        wrong format_ver, must be ignored
    python3 tools/inject_owner_record.py --v1             the previous format (v3), must be ignored
    python3 tools/inject_owner_record.py --wrong-uid      another board's uid, must be ignored
    python3 tools/inject_owner_record.py --restore        empty the root area again

⚠️ WHY THIS IS NOT JUST "PROGRAMMER, WRITE 160 BYTES AT 0x081E2000"

The root area lives in sector 15, next to the calibration values and the
firmware metadata. STM32_Programmer_CLI erases the whole 128 KiB sector before
writing into it, so writing only the record would take the calibration values
and the metadata with it.

So this reads the whole sector over SWD, replaces only the root area, and
writes the sector back as one image. Calibration, metadata and the layout
marker come back byte for byte.

It stays useful for the cases firmware is not supposed to be able to produce --
a record from a future format version, or one with a signature that does not
verify.

Exit 0 = flashed, 1 = the board did not report an owner slot, 2 = prerequisites missing.
"""

import argparse
import re
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (cfg, Section, Ok, Fail,  # noqa: E402
                    assert_target_reachable, get_programmer_cli, get_scratch_file,
                    open_log_ports, read_log_ports, run_capture, tcp_command)
from reset_board_to_factory_state import read_bytes  # noqa: E402

# Must match $BOOT/IAPServer/owner_slot.h and bootloader_state.c.
STATE_SECTOR_BASE = 0x081E0000
SECTOR_SIZE = 128 * 1024
OWNER_BASE = 0x081E2000
OWNER_OFFSET = OWNER_BASE - STATE_SECTOR_BASE      # root area inside sector 15
OWNER_SIZE = 8 * 1024
MARKER_OFFSET = SECTOR_SIZE - 32
MARKER_MAGIC = 0x4C353153                          # "S15L"
# Two fixed-length segments in the 8 KiB area: 32 'O' records of 160 B at
# offset 0, then 96 'R' records of 32 B at offset 5120 (5120 + 3072 = 8192).
# 'O' slot i lives at OWNER_OFFSET + i * RECORD_SIZE; this script only writes 'O'.
RECORD_SIZE = 160
OWNER_FORMAT_VER = 4
# The format this firmware no longer accepts, for --v1 -- named for the flag,
# not for what it writes: v3 is the format immediately before this one, and is
# exactly as rejected as an actual v1 record would be (record_is_structurally_valid()
# only ever compares against the CURRENT OWNER_FORMAT_VER, so any wrong number
# proves the same thing).
PREVIOUS_FORMAT_VER = 3
UID_LEN = 12

INTERESTING = re.compile(r"Owner slot|Bootloader state|Sector 15|APP Mod|UPLOAD Mod|"
                         r"NOT in effect|Reset cause|no root")


def record(generation, format_ver, flags, key_hex, filler, uid=b"", record_type=0x4F):
    """One 160-byte 'O' record. prev_sig and reserved stay zero.

    Byte 1 was `slots` through format_ver 2; v3 dropped it (nothing ever read
    it) in favour of a reserved byte that keeps every later field at the same
    offset, so this always writes 0 there regardless of which format_ver is
    being produced -- a --v1 record differs from a real one only in the
    format_ver field itself, which is the one thing record_is_structurally_valid()
    actually checks.
    """
    rec = bytearray(RECORD_SIZE)
    rec[0] = record_type                                # 'O' (0x4F); 'R' is a separate 32-byte layout
    rec[1] = 0                                          # reserved0
    rec[2:4] = struct.pack("<H", format_ver)
    rec[4:8] = struct.pack("<I", generation)
    rec[8:12] = struct.pack("<I", flags)
    if key_hex:
        rec[12:76] = bytes.fromhex(key_hex)
    elif filler is not None:
        rec[12:76] = bytes([filler]) * 64
    if uid:
        rec[76:76 + UID_LEN] = uid
    return bytes(rec)


def board_uid(ip, port):
    """The board's own UID, which an 'O' record has to carry to be accepted.

    Asked of the board rather than passed in: the whole point of the field is
    that it names one specific board, so a value typed by hand is a value that
    can be wrong without anything noticing.
    """
    text = tcp_command(ip, port, "getuid").strip().lower()
    if len(text) != UID_LEN * 2:
        return None
    try:
        return bytes.fromhex(text)
    except ValueError:
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--generation", type=int, default=1)
    ap.add_argument("--key", default="")       # 128 hex chars; default is a recognisable pattern
    ap.add_argument("--cleared", action="store_true")
    ap.add_argument("--corrupt", action="store_true")
    ap.add_argument("--v1", action="store_true",
                    help="write the previous format (v3), which this firmware must reject")
    ap.add_argument("--wrong-uid", action="store_true",
                    help="carry another board's uid, as a copied record would")
    ap.add_argument("--restore", action="store_true",
                    help="empty the root area; everything else in sector 15 is kept")
    ap.add_argument("--ip", default=getattr(cfg, "BOARD_IP", ""))
    ap.add_argument("--port", default="56865")
    ap.add_argument("--also-unsigned", type=int, default=0,
                    help="add a second, unsigned record at this generation")
    ap.add_argument("--also-cleared", action="store_true",
                    help="make that second record a factory reset")
    ap.add_argument("--seconds", type=int, default=10)
    args = ap.parse_args()

    cli = get_programmer_cli()
    area = bytearray(b"\xFF" * OWNER_SIZE)

    if args.restore:
        Section("emptying the root area")
    else:
        Section("building the owner record")

        # format_ver: 4 normally, 99 for --corrupt and 3 (PREVIOUS_FORMAT_VER)
        # for --v1, both of which the scanner must reject rather than try to
        # interpret.
        ver = 99 if args.corrupt else (PREVIOUS_FORMAT_VER if args.v1 else OWNER_FORMAT_VER)
        flags = 1 if args.cleared else 0

        # A current-format record only counts on the board whose uid it carries. Cleared
        # records are exempt (they assert nothing about which board), and a v1
        # record has no field to put it in.
        uid = b""
        if ver == OWNER_FORMAT_VER and not args.cleared:
            if args.wrong_uid:
                uid = bytes(range(1, UID_LEN + 1))
                print("  uid: another board's (%s)" % uid.hex())
            else:
                uid = board_uid(args.ip, args.port)
                if uid is None:
                    Fail("could not read this board's uid over TCP at %s:%s -- "
                         "it has to be in the bootloader and reachable" % (args.ip, args.port))
                    return 2
                print("  uid: this board's (%s)" % uid.hex())

        # root_pubkey: all zero in a cleared record. Otherwise a real key when
        # one is given -- needed to set up a board for the setowner cases, where
        # the next record has to be signed by whoever this record names -- and a
        # recognisable pattern when it is not, for the cases where the key's
        # value never matters.
        key_hex, filler = "", None
        if not args.cleared:
            if args.key:
                if len(args.key) != 128:
                    Fail("--key needs 128 hex chars, got %d" % len(args.key))
                    return 2
                key_hex = args.key
                print("  root_pubkey: %s..." % args.key[:32])
            else:
                filler = 0xAA
        area[0:RECORD_SIZE] = record(args.generation, ver, flags, key_hex, filler, uid)
        print("  type 'O', format_ver %d, generation %d, flags %d"
              % (ver, args.generation, flags))

        if args.also_unsigned > 0:
            # A second record with a HIGHER generation and no signature.
            #
            # Without --also-cleared this is what an attacker able to append
            # would write to take a claimed board over, and the bootloader must
            # reject it: authority comes from the chain, not from being the
            # highest generation present.
            #
            # With --also-cleared it is a factory reset, which is legitimately
            # unsigned -- gated by a physical action instead. Same shape,
            # opposite verdict, which is exactly why both are worth having.
            if args.also_cleared:
                att = record(args.also_unsigned, OWNER_FORMAT_VER, 1, "", None)
                print("  plus a CLEARED record at generation %d (should apply)" % args.also_unsigned)
            else:
                att = record(args.also_unsigned, OWNER_FORMAT_VER, 0, "", 0xBB, uid)
                print("  plus an UNSIGNED record at generation %d (should be rejected)"
                      % args.also_unsigned)
            area[RECORD_SIZE:2 * RECORD_SIZE] = att

    Section("reading sector 15")
    assert_target_reachable(cli)
    sector = read_bytes(cli, STATE_SECTOR_BASE, SECTOR_SIZE)
    if sector is None:
        Fail("could not read sector 15 over SWD - nothing written")
        return 2
    magic = struct.unpack_from("<I", sector, MARKER_OFFSET)[0]
    if magic != MARKER_MAGIC:
        # Without the layout marker the bootloader rebuilds the sector on the next
        # boot and the record would be thrown away with everything else.
        Fail("sector 15 has no layout marker - boot the decision-72 bootloader once first")
        return 2
    image = bytearray(sector)
    image[OWNER_OFFSET:OWNER_OFFSET + OWNER_SIZE] = area
    tmp = Path(get_scratch_file("sector15_with_owner.bin"))
    tmp.write_bytes(bytes(image))
    print("  calibration, metadata and marker kept; root area replaced")

    Section("flashing")
    open_ports = open_log_ports(cfg.LOG_PORTS)
    text, _ = run_capture([cli, "-c", "port=SWD", "mode=UR", "-w", str(tmp),
                           hex(STATE_SECTOR_BASE), "-rst"])
    for line in text.splitlines():
        if re.search(r"Download|verified|Error|Reset", line):
            print("  " + line)

    all_text = "\n".join(read_log_ports(open_ports, args.seconds).values())

    Section("boot log")
    for line in all_text.splitlines():
        if INTERESTING.search(line):
            print("    | " + line)

    Section("result")
    if "Owner slot:" not in all_text:
        Fail("no 'Owner slot:' line - is this bootloader new enough?")
        return 1
    Ok("flashed; read the line above against what this record was meant to be")
    return 0


if __name__ == "__main__":
    sys.exit(main())
