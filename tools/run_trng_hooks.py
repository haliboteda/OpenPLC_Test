"""Decision 67 on the real board: lwIP's seed and TCP ISNs come from the TRNG.

    python tools/run_trng_hooks.py --key <owner.pem> [--runs 2]

Bootloader side. Per run: the board is reset into its app, GDB attaches over
ST-Link with breakpoints in net_rand.c on the srand() call and the ISN return; enter_bootloader.py reboots the board into upload mode
(srand fires once, its TCP connection fires the ISN hook), then one more TCP
connection fires it again. Each value must equal hrng.RandomNumber at that
moment -- the HAL stores every TRNG word there, so equality means the value
came from the TRNG, not from the rand() fallback. All values must differ.

Not covered: the app side (checked once by hand, see
$PROD/docs/modules/M1/CHALLENGE-AUTH.md), and the bytes on the wire (no
capture on this machine).

Exit 0 = pass, 1 = fail, 2 = setup problem.
"""

import argparse
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from common import (board_ip, cfg, Section, Ok, Fail, EXE, _newest,  # noqa: E402
                    cube_layout, get_programmer_cli)

GDB_PORT = 61234
PLUGINS = cube_layout(cfg.CUBEIDE)[0]

GDB_SCRIPT = """set pagination off
set confirm off
file {elf}
target remote :{port}
break net_rand.c:16
break net_rand.c:27
define report
  if $_hit_bpnum == 1
    printf "SEED arg=%08x rng=%08x\\n", seed, hrng.RandomNumber
  else
    printf "ISN ret=%08x rng=%08x\\n", isn, hrng.RandomNumber
  end
end
continue
report
continue
report
continue
report
delete
detach
quit
"""


def tool(pattern):
    return _newest(PLUGINS / pattern)


def one_run(args, gdbserver, gdb, elf, scratch):
    script = scratch / "trng.gdb"
    script.write_text(GDB_SCRIPT.format(elf=Path(elf).as_posix(), port=GDB_PORT))
    # Start from the app: enter_bootloader only reboots a board that is in it.
    subprocess.run([str(get_programmer_cli()), "-c", "port=SWD", "mode=UR", "-rst"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(5)
    srv = subprocess.Popen([str(gdbserver), "-p", str(GDB_PORT), "-d", "-g",
                            "-cp", str(Path(get_programmer_cli()).parent)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(3)
        g = subprocess.Popen([str(gdb), "-batch", "-x", str(script)],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, errors="replace")
        time.sleep(4)
        argv = [sys.executable, str(HERE / "enter_bootloader.py"),
                "--ip", args.ip, "--seconds", "3"]
        if args.key:
            argv.append("--key=%s" % args.key)
        subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=180)
        time.sleep(2)
        try:
            socket.create_connection((args.ip, 56865), timeout=10).close()
        except OSError:
            pass
        out, _ = g.communicate(timeout=60)
    finally:
        srv.kill()
    seeds = re.findall(r"SEED arg=([0-9a-f]{8}) rng=([0-9a-f]{8})", out)
    isns = re.findall(r"ISN ret=([0-9a-f]{8}) rng=([0-9a-f]{8})", out)
    return seeds, isns, out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ip", default="", help="default: found by BOARD_UID")
    ap.add_argument("--key", default="")
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--elf", default=str(Path(cfg.BOOT_REPO) / "Debug"
                                         / "OpenPLC_BootLoader.elf"),
                    help="must be the bootloader that is on the board")
    args = ap.parse_args()

    gdbserver = tool("com.st.stm32cube.ide.mcu.externaltools.stlink-gdb-server.*"
                     "/tools/bin/ST-LINK_gdbserver" + EXE)
    gdb = tool("com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.*"
               "/tools/bin/arm-none-eabi-gdb" + EXE)
    args.ip = args.ip or board_ip()
    if not args.ip:
        return 2
    if not (gdbserver and gdb and Path(args.elf).exists()):
        Fail("need ST-LINK_gdbserver, arm-none-eabi-gdb and %s" % args.elf)
        return 2
    scratch = Path(cfg.TEST_REPO) / "Output" / "trng"
    scratch.mkdir(parents=True, exist_ok=True)

    values, bad = [], 0
    for i in range(args.runs):
        Section("run %d" % (i + 1))
        seeds, isns, out = one_run(args, gdbserver, gdb, args.elf, scratch)
        if len(seeds) != 1 or len(isns) != 2:
            print(out[-1500:])
            Fail("expected 1 seed and 2 ISNs, got %d and %d" % (len(seeds), len(isns)))
            return 1
        for kind, (val, rng) in [("seed", seeds[0])] + [("isn", x) for x in isns]:
            same = val == rng
            print("  %-4s %s  TRNG word %s  %s" % (kind, val, rng,
                                                  "from TRNG" if same else "NOT from TRNG"))
            bad += not same
            values.append(val)
    if bad:
        Fail("%d value(s) did not come from the TRNG" % bad)
        return 1
    if len(set(values)) != len(values):
        Fail("a value repeated: %s" % values)
        return 1
    Ok("%d seeds and %d ISNs, all from the TRNG and all different"
       % (args.runs, 2 * args.runs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
