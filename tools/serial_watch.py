"""Watch the board's log ports. Touches nothing on the board unless --reset.

    python tools/serial_watch.py                 watch until Ctrl-C
    python tools/serial_watch.py --seconds 30    watch for a while, then print
    python tools/serial_watch.py --reset         reset over ST-Link first, to catch a boot log
    python tools/serial_watch.py --ports COM7    override the ports from config

    python tools/serial_watch.py --seconds 600 --until "Checking Starting Mod" --until-count 6
                                                 stop as soon as six boots have
                                                 been seen, --seconds is only
                                                 the give-up time

⚠️ Without --until, --seconds is both the window AND the wait: the capture
prints nothing until it expires. Guess it too short and the event is missed;
too long and somebody is sitting there waiting for a timer. When you know
what you are waiting for, say so and let it stop on its own.
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from common import (Fail, Section, cfg, decode_serial,  # noqa: E402
                    get_programmer_cli, open_log_ports, read_log_ports)


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--seconds", type=int, default=0,
                    help="0 = until Ctrl-C")
    ap.add_argument("--reset", action="store_true",
                    help="reset over ST-Link first, to catch a boot log")
    ap.add_argument("--ports", nargs="*", default=None)
    ap.add_argument("--until", default=None,
                    help="regex; stop once it has matched --until-count times")
    ap.add_argument("--until-count", type=int, default=1)
    args = ap.parse_args()

    ports = args.ports if args.ports else cfg.LOG_PORTS

    Section("Ports")
    open_ports = open_log_ports(ports)
    if not open_ports:
        Fail("no log port could be opened")
        return 1

    if args.reset:
        Section("Reset")
        out = subprocess.run([str(get_programmer_cli()), "-c", "port=SWD", "mode=UR", "-rst"],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, errors="replace").stdout or ""
        for line in out.splitlines():
            if "Error" in line or "Reset" in line:
                print(line)

    Section("Capturing %ds" % args.seconds if args.seconds > 0
            else "Streaming - Ctrl-C to stop")

    # Streaming mode prints as it arrives so a long soak is watchable; timed mode
    # reuses the shared drain so its output matches what flash_bootloader.py shows.
    if args.seconds > 0:
        buf = read_log_ports(open_ports, args.seconds,
                             until=args.until, until_count=args.until_count)
        for k, v in buf.items():
            Section("%s  (%d bytes)" % (k, len(v)))
            if v:
                print(v)
    else:
        try:
            while True:
                for k, h in open_ports.items():
                    try:
                        n = h.in_waiting
                        if n:
                            chunk = decode_serial(h.read(n))
                            if len(open_ports) > 1:
                                sys.stdout.write("[%s] " % k)
                            sys.stdout.write(chunk)
                            sys.stdout.flush()
                    except Exception:
                        pass
                time.sleep(0.06)
        except KeyboardInterrupt:
            pass
        finally:
            for h in open_ports.values():
                try:
                    h.close()
                except Exception:
                    pass
            print("")
    return 0


if __name__ == "__main__":
    sys.exit(main())
