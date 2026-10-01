"""R1-04 -- holding BOOT0 across a reset forces upload mode.

    python3 tools/run_boot0_upload_mode.py              wait up to 180s for the gesture
    python3 tools/run_boot0_upload_mode.py --wait 300
    python3 tools/run_boot0_upload_mode.py --ports COM5

The person does the whole gesture; this script only captures the board's log and
judges it. That split is forced by the hardware, not chosen for convenience --
see below.

    1. Press and release the reset button.
    2. The moment the system LED starts blinking, press and HOLD BOOT0.
    3. Keep holding until the blinking stops, then about two seconds more.

The blinking is the cue on purpose: boot_window() in Core/Src/main.c blinks
the system LED for 2 s and reads BOOT0 at the end, so the blinking IS the
window. Nothing on the PC can see that window open.

⛔ NEVER HOLD BOOT0 WHILE SOMETHING ELSE DRIVES THE RESET.

BOOT0 is a boot-mode pin. Held high at the reset edge, the STM32H7 starts ST's
own system DFU loader instead of this flash, so the bootloader never runs, the
log stays completely silent, and the board enumerates as "DFU in FS Mode" until
the next reset with BOOT0 low. An earlier version of this file drove NRST from
the ST-Link while the operator held the button, and did exactly that 11 times in
a row on 2026-09-18 -- every attempt reported "not taken" while the board was in
fact in DFU. The board's own boot banner already says the right order: hold
BOOT0 "for 3-5 seconds WHILE CLICKING", that is, reset first.

⛔ DESTRUCTIVE ON A CLAIMED BOARD.

Holding BOOT0 for 10 s after the reset arms a factory reset, and releasing it
then runs one. Measured on an unclaimed board the log reads "Factory reset:
board was already unclaimed, nothing to do"; on a claimed board the same gesture
drops the owner key. Run this on an unclaimed board, or be ready to claim it
again.

Criteria, both required:
    ** UPLOAD Mod ... (BOOT0 held)        the board took the gesture
    ** Reset cause: PIN                   the reset really happened

A capture with no log at all is reported as "the board never rebooted", not as a
failure of R1-04: silence proves nothing about the gesture.

Exit 0 = R1-04 held, 1 = it did not, 2 = the run could not be set up.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (cfg, Section, Ok, Warn, Fail, banner,  # noqa: E402
                    close_ports, open_log_ports, read_log_ports)

UPLOAD_HELD = "(BOOT0 held)"
UPLOAD_MOD = "** UPLOAD Mod"
RESET_PIN = "** Reset cause: PIN"
FACTORY_RAN = "Factory reset:"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ports", action="append", help="log port; repeatable")
    ap.add_argument("--wait", type=int, default=180,
                    help="seconds to capture while you do the gesture (default 180)")
    args = ap.parse_args()

    ports = args.ports or list(cfg.LOG_PORTS)

    Section("R1-04 -- BOOT0 held across a reset forces upload mode")
    open_ports = open_log_ports(ports)
    if not open_ports:
        Fail("no log port opened; without the board's own log there is no verdict")
        return 2

    try:
        banner(["DO THIS ON THE BOARD, IN THIS ORDER:",
                "",
                "  1. Press and release the RESET button.",
                "  2. When the system LED starts blinking, press and HOLD BOOT0.",
                "  3. Let go about two seconds after the blinking stops.",
                "",
                "Do NOT hold BOOT0 while pressing reset -- that starts ST's DFU",
                "loader instead, and the board goes completely silent.",
                "",
                "%d seconds; nothing to confirm afterwards." % args.wait])
        captured = read_log_ports(open_ports, args.wait)
    finally:
        close_ports(open_ports)

    text = "\n".join(captured.values()) if isinstance(captured, dict) else str(captured)

    Section("what the board said")
    if not text.strip():
        Fail("nothing came out of the log port -- the board never rebooted. "
             "This proves nothing about R1-04. If the board also enumerates as "
             "'DFU in FS Mode', BOOT0 was down at the reset edge; reset it once "
             "with the button released and try again.")
        return 2
    for line in text.splitlines():
        print("  " + line)

    Section("verdict")
    if RESET_PIN not in text:
        Warn("the log has no %r" % RESET_PIN)
        Fail("no external reset in this capture; nothing was proven")
        return 2

    if FACTORY_RAN in text:
        Warn("a factory reset ran -- BOOT0 was held past 10 s. Harmless on an "
             "unclaimed board; on a claimed one the owner key is now gone.")

    if UPLOAD_MOD in text and UPLOAD_HELD in text:
        Ok("R1-04 holds: the board reports %r" % UPLOAD_HELD)
        return 0

    Fail("the board reset but did not report %r -- the gesture was not taken. "
         "Most often the button went down before the relay window, or was "
         "released before it ended." % UPLOAD_HELD)
    return 1


if __name__ == "__main__":
    sys.exit(main())
