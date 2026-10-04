"""How the real bootloader picks upload mode, in Renode. Cases T1-27 and T1-25.

    python boot_entry.py

Builds the current $BOOT working tree in a temporary copy, compiles SystemLED as
the app, lays out flash as T3-05 does, and boots it four times:

  plain        no BOOT0, no request            -> jumps to the app
  boot0        BOOT0 (PG9) high from power-on,
               released before 10 s           -> upload mode, no factory reset  (T1-27)
  cdc-request  a CDC handoff record in SRAM4,
               soft reset                     -> CDC upload mode, lwIP never
                                                  initialised                   (T1-25)
  eth-request  the same with an ethernet record -> lwIP initialised (control)

Renode reports power-on in RCC_RSR after every reset, so the request runs turn
it into a soft reset while the record is in SRAM4 (SIM-02 found this).
Criteria and what this cannot see: $PROD/docs/modules/M1-firmware-upgrade.md,
T1-27 and T1-25.

Exit 0 = pass, 1 = fail, 2 = prerequisites missing.
"""

import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "tools"))

from build_image import build_copy  # noqa: E402
from common import EXE, Fail, Ok, Section, cfg, get_iap_tool, get_scratch_dir, run_capture  # noqa: E402
from run import FQBN, PLATFORM, flash_banks, symbols  # noqa: E402

APP_EXAMPLE = "SystemLED"
# $BOOT/IAPServer/IAP_boot_handoff.h
HANDOFF_ADDR = 0x38000000
HANDOFF_MAGIC = 0x504C4321
HANDOFF_VERSION = 1
REQ_CDC, REQ_ETH = 1, 2
RSR_PORRSTF, RSR_SFTRSTF = 1 << 23, 1 << 24
# Virtual seconds. The 2 s BOOT0 window ends before RELEASE_AT; the factory
# reset arms 10 s after the window opens, well after it.
RELEASE_AT = 7
RUN_SECONDS = 14
RENODE_TIMEOUT = 600
HOOKED = ("MX_LWIP_Init", "MX_USB_DEVICE_Init")


def handoff_words(mode):
    check = ~(HANDOFF_MAGIC ^ HANDOFF_VERSION ^ mode) & 0xFFFFFFFF
    return struct.unpack("<4I", struct.pack("<IHHII", HANDOFF_MAGIC, HANDOFF_VERSION, 16, mode, check)[:16])


def run_renode(work, boot_sym, run):
    w = work.as_posix()
    lines = [
        'mach create "plc"',
        "machine LoadPlatformDescription @%s" % PLATFORM.as_posix(),
        "sysbus WriteDoubleWord 0x1FF1E800 0",
        "sysbus WriteDoubleWord 0x1FF1E804 0",
        "sysbus WriteDoubleWord 0x1FF1E808 0",
        "sysbus LoadBinary @%s/bank1.bin 0x08000000" % w,
        "sysbus LoadBinary @%s/bank2.bin 0x08100000" % w,
        "cpu VectorTableOffset 0x08000000",
        "uart4 CreateFileBackend @%s/uart4.txt true" % w,
    ]
    for name in HOOKED:
        lines.append('cpu AddHook 0x%08X "self.InfoLog(\'HIT %s\')"' % (boot_sym[name], name))
    if run in ("cdc-request", "eth-request"):
        for i, word in enumerate(handoff_words(REQ_CDC if run == "cdc-request" else REQ_ETH)):
            lines.append("sysbus WriteDoubleWord 0x%08X 0x%08X" % (HANDOFF_ADDR + 4 * i, word))
        lines.append('sysbus SetHookAfterPeripheralRead sysbus.rcc "if offset == 0xD0 and '
                     'machine.SystemBus.ReadDoubleWord(0x%08X) == 0x%08X: value = (value & ~0x%X) | 0x%X"'
                     % (HANDOFF_ADDR, HANDOFF_MAGIC, RSR_PORRSTF, RSR_SFTRSTF))
    if run == "boot0":
        lines += ["sysbus.gpioPortG OnGPIO 9 true",
                  'emulation RunFor "00:00:%02d"' % RELEASE_AT,
                  "sysbus.gpioPortG OnGPIO 9 false",
                  'emulation RunFor "00:00:%02d"' % (RUN_SECONDS - RELEASE_AT)]
    else:
        lines.append('emulation RunFor "00:00:%02d"' % RUN_SECONDS)
    lines += ['echo "RUN DONE"', "q"]
    (work / "run.resc").write_text("\n".join(lines) + "\n")
    # stdin stays open until Renode quits (see run.py). --console opens no port.
    p = subprocess.Popen([cfg.RENODE, "--disable-gui", "--console", "-e", "include @%s/run.resc" % w],
                         cwd=str(Path(cfg.RENODE).parent), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, errors="replace")
    # A failing command in the script leaves Renode at its prompt for good.
    timer = threading.Timer(RENODE_TIMEOUT, p.kill)
    timer.start()
    out = p.stdout.read()
    p.wait()
    timer.cancel()
    out = re.sub(r"\x1b\[[0-9;]*m", "", out)
    (work / "console.txt").write_text(out, encoding="utf-8")
    uart4 = (work / "uart4.txt").read_text(errors="replace") if (work / "uart4.txt").exists() else ""
    return out, uart4


def judge(run, out, uart4):
    hit = {name: ("HIT " + name) in out for name in HOOKED}
    ran = [("Renode ran the whole script", "RUN DONE" in out)]
    if run == "plain":
        return ran + [("BOOT0 read as not pressed", "BOOT0 button is not pressed." in uart4),
                      ("jumped to the app", "** APP Mod" in uart4)]
    if run == "boot0":
        return ran + [("BOOT0 read as held at the end of the window", "** BOOT0 held" in uart4),
                      ("upload mode because of BOOT0", "** UPLOAD Mod ... (BOOT0 held)" in uart4),
                      ("did not jump to the app", "** APP Mod" not in uart4),
                      ("released before 10 s: no factory reset", "Factory reset ARMED" not in uart4
                       and "FACTORY RESET DONE" not in uart4)]
    if run == "cdc-request":
        return ran + [("reset read as soft", "** Reset cause: SOFT" in uart4),
                      ("upload mode because of the CDC request", "** UPLOAD Mod ... (CDC upload requested)" in uart4),
                      ("USB device initialised", hit["MX_USB_DEVICE_Init"]),
                      ("lwIP never initialised", not hit["MX_LWIP_Init"])]
    return ran + [("reset read as soft", "** Reset cause: SOFT" in uart4),
                  ("upload mode because of the ethernet request",
                   "** UPLOAD Mod ... (ethernet upload requested)" in uart4),
                  ("lwIP initialised (control for the CDC run)", hit["MX_LWIP_Init"]),
                  ("USB device not initialised", not hit["MX_USB_DEVICE_Init"])]


def main():
    renode = getattr(cfg, "RENODE", "")
    arduino_cli = getattr(cfg, "ARDUINO_CLI", "")
    cli_config = getattr(cfg, "ARDUINO_CLI_CONFIG", "")
    tools = Path(cfg.A15) / "packages" / "OpenPLC_Alpha" / "tools"
    nm = sorted(tools.glob("xpack-arm-none-eabi-gcc/*/bin/arm-none-eabi-nm" + EXE))
    example = Path(cfg.CORE_LIVE) / "libraries" / "OpenPLC_Ports" / "examples" / APP_EXAMPLE
    missing = [
        (not renode or not Path(renode).exists(), "Renode not found. Set $RENODE in config/machine.py"),
        (not arduino_cli or not Path(arduino_cli).exists(), "arduino-cli not found. Set $ARDUINO_CLI in config/machine.py"),
        (not cli_config or not Path(cli_config).exists(), "arduino-cli config not found at %s" % cli_config),
        (not nm, "arm-none-eabi-nm not found under %s" % tools),
        (not example.exists(), "example not found at %s" % example),
    ]
    if any(bad for bad, _ in missing):
        for bad, why in missing:
            if bad:
                Fail(why)
        return 2

    # Unique per run: other Renode runs may be going on at the same time.
    root = Path(tempfile.mkdtemp(prefix="renode_t1-27_", dir=str(get_scratch_dir())))
    boot_bin = build_copy(root / "boot")
    if boot_bin is None:
        Fail("FAIL - the bootloader does not build; files in %s" % root)
        return 1
    boot_sym = symbols(nm[-1], boot_bin.with_suffix(".elf"))
    absent = [n for n in HOOKED if n not in boot_sym]
    if absent:
        Fail("FAIL - not in the bootloader's symbols: %s" % ", ".join(absent))
        return 1

    Section("Build: %s" % APP_EXAMPLE)
    build = root / "app"
    out, rc = run_capture([arduino_cli, "compile", "--config-file", cli_config, "--fqbn", FQBN,
                           "--build-path", build, example])
    if rc != 0:
        Fail("FAIL - %s does not compile:\n%s" % (APP_EXAMPLE, out[-2000:]))
        return 1
    app_bin = build / (APP_EXAMPLE + ".ino.bin")

    failed = False
    for run in ("plain", "boot0", "cdc-request", "eth-request"):
        Section("Renode: %s" % run)
        work = root / run
        work.mkdir()
        flash_banks(boot_bin, app_bin, get_iap_tool(), work)
        out, uart4 = run_renode(work, boot_sym, run)
        for name, ok in judge(run, out, uart4):
            (Ok if ok else Fail)("  %s %s" % ("ok  " if ok else "FAIL", name))
            failed |= not ok

    Section("result")
    if failed:
        Fail("FAIL - Renode output in %s" % root)
        return 1
    shutil.rmtree(str(root), ignore_errors=True)
    Ok("BOOT0 held -> upload mode; a CDC request -> CDC upload mode without lwIP")
    return 0


if __name__ == "__main__":
    sys.exit(main())
