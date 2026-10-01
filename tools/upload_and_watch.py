"""Run a real upload through IAPTool while capturing the board's serial log, then
judge it against what SDRAM staging is supposed to do.

Exit code is the verdict: 0 every check passed, 1 at least one failed. Until
2026-09-17 this always exited 0 because Fail() only colours text, so the
verdict was printed and thrown away.

    python3 tools/upload_and_watch.py --bin <file.bin>              over ethernet, IP from config
    python3 tools/upload_and_watch.py --bin <file.bin> --ip 1.2.3.4
    python3 tools/upload_and_watch.py --bin <file.bin> --cdc COM6   over USB CDC

The upload is driven by the shipping IAPTool, not by a reimplementation here:
what gets exercised has to be the code path customers use.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (cfg, Section, Ok, Warn, Fail, close_ports,  # noqa: E402
                    emit_file, get_iap_tool, get_scratch_file, open_log_ports,
                    run_while_draining)

TAIL_S = 6


def verdict(all_text, exit_code, expect_banner=None):
    """Judge the captured log. Returns (failures, unknowns).

    A failure is a check that ran and came out wrong. An unknown is a check
    whose evidence never appeared -- it does not fail the run on its own, but
    it is reported, because a silent unknown is how a check stops covering
    anything without anybody noticing.
    """
    Section("Verdict")
    fails = unknowns = 0

    if "SDRAM staging buffer OK" in all_text:
        Ok("  self-test: staging buffer usable")
    elif "SDRAM SELF-TEST FAILED" in all_text:
        Fail("  self-test: FAILED")
        fails += 1
    else:
        Warn("  self-test line not seen (did the board stay in the bootloader?)")
        unknowns += 1

    if "Staging in SDRAM" in all_text:
        Ok("  staged instead of erasing up front")
    else:
        Warn("  no 'Staging in SDRAM' - is this the new bootloader?")
        unknowns += 1

    # The whole point of staging: the erase must come after verification.
    i_erase = all_text.find("Erasing application region")
    i_verif = all_text.find("Transfer complete, verifying")
    if i_erase >= 0 and i_verif >= 0 and i_erase > i_verif:
        Ok("  erase happened AFTER verification - this is the change working")
    elif i_erase >= 0 and i_verif >= 0:
        # Erase before verification is staging not working, which is the one
        # thing this script exists to catch.
        Fail("  erase happened BEFORE verification - staging is not working")
        fails += 1
    elif i_erase >= 0 and i_verif < 0:
        Fail("  erased with no verification line before it")
        fails += 1
    else:
        Warn("  no erase line - upload did not reach the commit step")
        unknowns += 1

    if exit_code == 0:
        Ok("  IAPTool exit 0")
    else:
        Fail("  IAPTool exit %d" % exit_code)
        fails += 1

    # "It uploaded" and "what is installed changed" are different claims. The
    # checks above are all about the transfer; only this one is about the
    # upgrade, which is what every one of the five paths ends in.
    if expect_banner:
        if expect_banner in all_text:
            Ok("  the board came up saying %r" % expect_banner)
        else:
            Fail("  the board never said %r - what is installed did not change"
                 % expect_banner)
            fails += 1

    return fails, unknowns


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bin", required=True)
    ap.add_argument("--expect-banner",
                    help="a string the board must print AFTER this upload, "
                         "e.g. 'IAP_PROBE_APP up v2'. Every path in the "
                         "five-path run ends in 'and upgrade', and an "
                         "upgrade is only observed by the installed thing "
                         "having changed -- without this the case passes on "
                         "an upload that replaced nothing.")
    ap.add_argument("--key", default="",
                    help="the owner private key this board is claimed for")
    ap.add_argument("--ip", default="")
    ap.add_argument("--cdc", default="")
    ap.add_argument("--tail-seconds", type=int, default=TAIL_S)
    ap.add_argument("--ports", nargs="*", default=None)
    args = ap.parse_args()

    ports = list(args.ports if args.ports is not None else cfg.LOG_PORTS)
    ip = args.ip
    if not ip and not args.cdc:
        ip = getattr(cfg, "BOARD_IP", "")
    if not ip and not args.cdc:
        Fail("need --ip or --cdc (or set BOARD_IP / CDC_PORT in config)")
        return 1
    image = Path(args.bin)
    if not image.exists():
        Fail("no such image: %s" % image)
        return 1
    iaptool = get_iap_tool()

    # The CDC port is the board talking to us; it cannot also be a passive log port.
    if args.cdc:
        ports = [p for p in ports if p != args.cdc]

    print("image: %s  (%s B)" % (image, format(image.stat().st_size, ",d")))

    Section("Log ports")
    open_ports = open_log_ports(ports)

    Section("Upload")
    argv = ["cdc", str(image), args.cdc] if args.cdc else ["ether", str(image), ip]
    # A claimed board only starts firmware signed by its owner root, so an
    # upload to one needs that key named. Without it IAPTool signs with the
    # published key and the board refuses the image after it has been written.
    if args.key:
        argv.append("--key=%s" % args.key)
    print("IAPTool %s" % " ".join(argv))

    # The board reboots into the application after a good upload, so keep
    # listening past IAPTool's exit.
    rc, buf = run_while_draining([iaptool] + argv, open_ports,
                                 get_scratch_file("iaptool.out"),
                                 get_scratch_file("iaptool.err"),
                                 tail_seconds=args.tail_seconds)
    close_ports(open_ports)

    Section("IAPTool output (exit %d)" % rc)
    emit_file(get_scratch_file("iaptool.out"))
    emit_file(get_scratch_file("iaptool.err"))

    for k, text in buf.items():
        Section("%s  (%d bytes)" % (k, len(text)))
        if text:
            print(text)

    fails, unknowns = verdict("\n".join(buf.values()), rc, args.expect_banner)
    if unknowns:
        Warn("  %d check(s) had no evidence either way" % unknowns)
    if fails:
        Fail("%d check(s) failed" % fails)
        return 1
    Ok("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
