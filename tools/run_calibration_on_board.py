"""T3-21 on the real board (requirement R3-10) -- the calibration in sector 15 reaches the AI readings.

    python tools/run_calibration_on_board.py [--key <owner.pem>] [--upload cdc|ether] [--skip-upload]

Uploads onboard/calibration/T3_21_Calibration, backs up the whole of sector 15
over SWD to Output/calibration-on-board/ (named by the chip UID), then:

  A  known coefficients in the calibration area -> status OK, readings follow them
  B  calibration area all 0xFF                  -> status BLANK, nominal readings,
                                                   the library's log line exactly once

Each write replaces only the first 8 KiB of the backed-up sector and writes the
whole sector back (decision 97, CAL-04), so the root area, the M records and
the layout marker are the ones read. The backup is ALWAYS written back at the
end and read back to check it.

The expected value is recomputed from the raw readings the sketch prints, so
whatever is wired to AI1 / AI2 works. Criterion and what this cannot see:
$PROD/docs/modules/M3-app-runtime.md, T3-21. AO is not checked: that needs a
person with a meter.

Exit 0 = pass, 1 = fail (or the restore did not verify), 2 = setup problem.
"""

import argparse
import re
import statistics
import struct
import sys
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (cfg, Section, Ok, Warn, Fail,  # noqa: E402
                    assert_target_reachable, board_ip, get_iap_tool,
                    get_output_dir, get_programmer_cli, get_scratch_dir,
                    open_board_log, open_rs232_ports, read_log_ports, run_capture)
from reset_board_to_factory_state import read_bytes, read_words  # noqa: E402

FQBN = ("OpenPLC_Alpha:stm32:OPEN-PLC:pnum=PLC_H743,usb=CDCgen,"
        "upload_method=cdcMethod")
NAME = "T3_21_Calibration"

# Sector 15 and the calibration format: $PROD/docs/modules/M1/SECTOR-15.md,
# owned by $BOOT/IAPServer/calib_area.h.
SECTOR_BASE = 0x081E0000
SECTOR_SIZE = 128 * 1024
CALIB_SIZE = 8 * 1024
MARKER_OFFSET = SECTOR_SIZE - 32
MARKER_MAGIC = 0x4C353153          # "S15L"
CALIB_MAGIC = 0x4C41434F           # "OCAL"
UID_BASE = 0x1FF1E800              # STM32H743 96-bit unique ID

# (gain, offset) for AI1 (mV), AI2 (mA), AO1, AO2 (mA). AO stays nominal: not checked here.
KNOWN = [(2.0, 1000.0), (1.5, 2.0), (1.0, 0.0), (1.0, 0.0)]
NOMINAL = [(1.0, 0.0)] * 4

# Margin: 0.5 % of each channel's nominal full scale, scaled by the gain.
AI1_FS_MV = 2500.0 * 90.6 / 22.6
AI2_FS_MA = 2500.0 / 124.0
MARGIN = 0.005

# The library's printf: a system line on USB port 1. The sketch's CAL lines are on RS232.
LOG_LINE = re.compile(r"\[sys\]\[NET\] OpenPLC_Ports: .*using nominal AI/AO conversion")
LOG_BLANK = "[sys] OpenPLC_Ports: no calibration on this board"
VALUES = re.compile(r"CAL ai1 raw=(\d+),(\d+) mv=(-?[\d.]+) ai2 raw=(\d+),(\d+) ma=(-?[\d.]+)")
MIN_LINES = 6


def nominal_ai1_mv(raw):
    """Same conversion as $PROD/docs/modules/M3/CALIBRATED-ANALOG.md, step 2."""
    return raw * 2500.0 / 65535.0 * 90.6 / 22.6


def nominal_ai2_ma(raw):
    return raw * 2500.0 / 65535.0 / 124.0


def calib_area(uid_words, coeffs):
    """The 8 KiB calibration area holding coeffs, valid for this chip."""
    head = struct.pack("<IHH3I", CALIB_MAGIC, 1, 4, *uid_words)
    head += b"".join(struct.pack("<ff", g, o) for g, o in coeffs)
    head += struct.pack("<I", zlib.crc32(head) & 0xFFFFFFFF)
    return head + b"\xFF" * (CALIB_SIZE - len(head))


def write_sector(cli, image, rs232_port, seconds):
    """Write a whole sector-15 image, reset, capture the sketch's RS232 output
    and the board's system lines, read the sector back.
    Returns (RS232 text, system lines, read-back matches)."""
    tmp = get_scratch_dir() / "t3_21_sector15.bin"
    tmp.write_bytes(image)
    ports = open_rs232_ports([rs232_port])
    ports.update(open_board_log(sys_only=True))
    out, rc = run_capture([cli, "-c", "port=SWD", "mode=UR", "-w", str(tmp),
                           hex(SECTOR_BASE), "-rst"])
    if rc or re.search(r"Error", out):
        print("\n".join(out.splitlines()[-6:]))
        read_log_ports(ports, 0)
        return "", "", False
    buf = read_log_ports(ports, seconds, until=VALUES, until_count=MIN_LINES + 2)
    back = read_bytes(cli, SECTOR_BASE, SECTOR_SIZE)
    return buf.get(rs232_port, ""), buf.get("usb1", ""), back == image


def judge(text, sys_text, coeffs, want_status, want_logs):
    """Checks one boot's capture: text from RS232, sys_text from USB port 1.
    Returns a list of failures (empty = pass)."""
    boots = text.count("CAL boot")
    if boots != 1:
        return ["saw %d sketch boots in the capture, want exactly 1" % boots]
    run = text.split("CAL boot", 1)[1]
    bad = []

    m = re.search(r"CAL status=(\w+)", run)
    status = m.group(1) if m else "(none)"
    print("    status: %s" % status)
    if status != want_status:
        bad.append("status %s, want %s" % (status, want_status))

    logs = LOG_LINE.findall(sys_text)
    for line in sys_text.splitlines():
        if LOG_LINE.search(line):
            print("    | " + line.strip())
    if len(logs) != want_logs:
        bad.append("library log line seen %d times, want %d" % (len(logs), want_logs))
    if want_logs and logs and LOG_BLANK not in sys_text:
        bad.append("the log line is not the 'no calibration' one")

    rows = VALUES.findall(run)
    if len(rows) < MIN_LINES:
        bad.append("only %d value lines, want %d" % (len(rows), MIN_LINES))
    (g1, o1), (g2, o2) = coeffs[0], coeffs[1]
    dev1, dev2 = [], []
    raw_sum = [0, 0]
    for a0, a1, mv, b0, b1, ma in rows:
        a0, a1, b0, b1 = int(a0), int(a1), int(b0), int(b1)
        raw_sum[0] += a0 + a1
        raw_sum[1] += b0 + b1
        n0, n1 = nominal_ai1_mv(a0), nominal_ai1_mv(a1)
        exp1 = g1 * (n0 + n1) / 2 + o1
        tol1 = g1 * (abs(n0 - n1) / 2 + MARGIN * AI1_FS_MV)
        n0, n1 = nominal_ai2_ma(b0), nominal_ai2_ma(b1)
        exp2 = g2 * (n0 + n1) / 2 + o2
        tol2 = g2 * (abs(n0 - n1) / 2 + MARGIN * AI2_FS_MA)
        # Deviation in units of this line's tolerance.
        dev1.append((float(mv) - exp1) / tol1)
        dev2.append((float(ma) - exp2) / tol2)
    if rows:
        # Median, not worst: a floating AI input spikes on single lines.
        med1, med2 = statistics.median(dev1), statistics.median(dev2)
        if abs(med1) > 1:
            bad.append("AI1 median deviation %.2f x tolerance" % med1)
        if abs(med2) > 1:
            bad.append("AI2 median deviation %.2f x tolerance" % med2)
        n = 2 * len(rows)
        print("    %d lines; mean raw AI1 %d, AI2 %d; deviation / tolerance: AI1 median %.2f worst %.2f, "
              "AI2 median %.2f worst %.2f"
              % (len(rows), raw_sum[0] // n, raw_sum[1] // n, med1, max(dev1, key=abs),
                 med2, max(dev2, key=abs)))
        if nominal_ai1_mv(raw_sum[0] / n) < 0.05 * AI1_FS_MV or \
           nominal_ai2_ma(raw_sum[1] / n) < 0.05 * AI2_FS_MA:
            Warn("    an input reads under 5 % of full scale: the offset is exercised, "
                 "the gain barely is")
    return bad


def run_case(cli, backup, area, label, coeffs, want_status, want_logs, args):
    Section(label)
    image = area + backup[CALIB_SIZE:]
    text, sys_text, same = write_sector(cli, image, args.port, args.seconds)
    if not same:
        return ["sector 15 did not read back as written"]
    return judge(text, sys_text, coeffs, want_status, want_logs)


def build_and_upload(args):
    sketch = Path(cfg.TEST_REPO) / "onboard" / "calibration" / NAME
    build = get_scratch_dir() / ("t3_21_" + NAME)
    Section("build")
    out, rc = run_capture([cfg.ARDUINO_CLI, "compile", "--config-file",
                           cfg.ARDUINO_CLI_CONFIG, "--fqbn", FQBN,
                           "--build-path", build, sketch])
    if rc:
        print("\n".join(out.splitlines()[-5:]))
        Fail("compile failed")
        return 2
    if args.skip_upload:
        return 0
    Section("upload (%s)" % args.upload)
    target = args.cdc if args.upload == "cdc" else (args.ip or board_ip())
    if not target:
        Fail("no upload target: give --cdc / --ip, or set CDC_PORT / BOARD_UID in config/machine.py")
        return 2
    argv = [get_iap_tool(), args.upload, str(build / (NAME + ".ino.bin")), target]
    if args.key:
        argv.append("--key=%s" % args.key)
    out, rc = run_capture(argv)
    if rc:
        print("\n".join(out.splitlines()[-8:]))
        Fail("upload failed")
        return 2
    Ok("uploaded")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--upload", choices=["cdc", "ether"], default="cdc")
    ap.add_argument("--cdc", default=getattr(cfg, "CDC_PORT", ""))
    ap.add_argument("--ip", default="", help="for --upload ether; default: found by BOARD_UID")
    ap.add_argument("--port", default=getattr(cfg, "RS232_PORT", ""),
                    help="RS232 adapter port (C05/C06), where the sketch prints")
    ap.add_argument("--key", default="")
    ap.add_argument("--skip-upload", action="store_true",
                    help="the sketch is already on the board")
    ap.add_argument("--seconds", type=int, default=30, help="log capture per case")
    args = ap.parse_args()

    rc = build_and_upload(args)
    if rc:
        return rc

    # Backed up after the upload: the upload appends an M record, and the
    # restore must keep the one that matches the app now in flash.
    Section("backing up sector 15")
    cli = str(get_programmer_cli())
    assert_target_reachable(cli)
    uid = read_words(cli, UID_BASE, 12)
    backup = read_bytes(cli, SECTOR_BASE, SECTOR_SIZE)
    if not uid or len(uid) < 3 or backup is None:
        Fail("could not read the UID or sector 15 over SWD - nothing written")
        return 2
    uid = uid[:3]
    if struct.unpack_from("<I", backup, MARKER_OFFSET)[0] != MARKER_MAGIC:
        Fail("sector 15 has no layout marker - nothing written")
        return 2
    uid_hex = "%08X%08X%08X" % tuple(reversed(uid))  # the order discovery and BOARD_UID use
    out_dir = get_output_dir() / "calibration-on-board"
    out_dir.mkdir(parents=True, exist_ok=True)
    saved = out_dir / ("sector15_%s_%s.bin" % (uid_hex, time.strftime("%Y%m%d-%H%M%S")))
    saved.write_bytes(backup)
    print("  uid %s; calibration area %s" % (
        uid_hex, "blank" if backup[:CALIB_SIZE] == b"\xFF" * CALIB_SIZE else "written"))
    print("  saved to %s" % saved)
    print("  manual restore: %s -c port=SWD mode=UR -w %s %s -rst" % (cli, saved, hex(SECTOR_BASE)))

    failures = {}
    try:
        failures["A known coefficients"] = run_case(
            cli, backup, calib_area(uid, KNOWN), "case A: known coefficients",
            KNOWN, "OK", 0, args)
        failures["B area erased"] = run_case(
            cli, backup, b"\xFF" * CALIB_SIZE, "case B: calibration area erased",
            NOMINAL, "BLANK", 1, args)
    finally:
        Section("restoring sector 15")
        text, _, same = write_sector(cli, backup, args.port, 10)
        if same:
            Ok("sector 15 read back identical to %s" % saved.name)
        else:
            Fail("sector 15 does NOT match the backup - restore it with the manual command above")
        m = re.search(r"CAL status=(\w+)", text)
        print("  sketch now reports status %s" % (m.group(1) if m else "(not seen)"))

    Section("result")
    for case, bad in failures.items():
        if bad:
            Fail("%s:" % case)
            for b in bad[:8]:
                print("    " + b)
        else:
            Ok(case)
    return 0 if same and len(failures) == 2 and not any(failures.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
