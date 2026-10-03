"""The bootloader drives the outputs low and they stay low into the app, in Renode. Case T1-37.

    python boot_outputs.py

Builds the current $BOOT working tree in a temporary copy, compiles SystemLED
(touches PE2 only), lays out flash as T3-05 does, and runs Renode twice: with
the reset-value option bytes (BOR_LEV 0) and with BOR_LEV programmed to 3.
Each run reads the pins of $BOOT/IAPServer/safe_outputs.c at five points.
Criteria and what this cannot see: $PROD/docs/modules/M1-firmware-upgrade.md, T1-37.

Exit 0 = pass, 1 = fail, 2 = prerequisites missing.
"""

import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "tools"))

from build_image import build_copy  # noqa: E402
from common import EXE, Fail, Ok, Section, cfg, get_iap_tool, get_scratch_dir, read_text, run_capture  # noqa: E402
from run import FQBN, PLATFORM, flash_banks, symbols  # noqa: E402

APP_EXAMPLE = "SystemLED"
GPIO_BASE = {"A": 0x58020000, "B": 0x58020400, "C": 0x58020800, "D": 0x58020C00, "E": 0x58021000,
             "F": 0x58021400, "G": 0x58021800, "H": 0x58021C00, "I": 0x58022000}
MODER, OTYPER, ODR = 0x00, 0x04, 0x14
FLASH_REG = 0x52002000
OPTKEYR, OPTCR, OPTSR_CUR, OPTSR_PRG = 0x08, 0x18, 0x1C, 0x20
BOR_LEV_SHIFT = 2
BOR_WARNING = "Brown-out reset is at level"
# Virtual time: the app's setup() starts at about 6.6 s.
RUN_SECONDS = 20
# Wall clock; a run takes about a minute.
RENODE_TIMEOUT = 600


def safe_pins():
    """(name, port letter, pin) from k_pins[] in safe_outputs.c."""
    src = read_text(Path(cfg.BOOT_REPO) / "IAPServer" / "safe_outputs.c")
    return [(m.group(3).split(",")[0].strip(), m.group(1), int(m.group(2)))
            for m in re.finditer(r"\{\s*GPIO([A-I]),\s*(\d+)U\s*\},\s*/\*\s*(.+?)\s*\*/", src)]


def sample_hook(tag, ports):
    regs = ", ".join("0x%08X" % (GPIO_BASE[p] + off) for p in ports for off in (MODER, OTYPER, ODR))
    return ("self.InfoLog('SAMPLE %s ' + ' '.join('%%08X' %% self.Bus.ReadDoubleWord(a) for a in [%s]))"
            % (tag, regs))


def run_renode(work, boot_sym, app_sym, ports, bor_level):
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
    if bor_level is not None:
        # The option-byte programming sequence ST-Link uses: unlock, stage, start.
        sr = 0x0406AAF0 & ~(3 << BOR_LEV_SHIFT) | (bor_level << BOR_LEV_SHIFT)
        lines += ["sysbus WriteDoubleWord 0x%08X 0x08192A3B" % (FLASH_REG + OPTKEYR),
                  "sysbus WriteDoubleWord 0x%08X 0x4C5D6E7F" % (FLASH_REG + OPTKEYR),
                  "sysbus WriteDoubleWord 0x%08X 0x%08X" % (FLASH_REG + OPTSR_PRG, sr),
                  "sysbus WriteDoubleWord 0x%08X 0x2" % (FLASH_REG + OPTCR)]
    lines += ['echo "OPTSR_CUR"', "sysbus ReadDoubleWord 0x%08X" % (FLASH_REG + OPTSR_CUR)]
    points = [("boot-after-safe-outputs", boot_sym["HAL_Init"]),
              ("boot-before-jump", boot_sym["server_jump_to_app"]),
              ("app-reset-handler", app_sym["Reset_Handler"]),
              ("app-setup", app_sym["setup"])]
    for tag, addr in points:
        lines.append('cpu AddHook 0x%08X "%s; self.RemoveHooksAt(0x%08X)"' % (addr, sample_hook(tag, ports), addr))
    lines += ['emulation RunFor "00:00:%02d"' % RUN_SECONDS, 'echo "SAMPLE app-running"']
    lines += ["sysbus ReadDoubleWord 0x%08X" % (GPIO_BASE[p] + off) for p in ports for off in (MODER, OTYPER, ODR)]
    lines += ['echo "SAMPLES DONE"', "q"]
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
    return out, uart4, [tag for tag, _ in points] + ["app-running"]


def samples(out, ports):
    """{tag: {port: (moder, otyper, odr)}}; a tag sampled twice keeps the first."""
    found = {}
    n = 3 * len(ports)
    for m in re.finditer(r"SAMPLE ([\w-]+) ((?:[0-9A-F]{8} ?){%d})" % n, out):
        found.setdefault(m.group(1), [int(v, 16) for v in m.group(2).split()])
    tail = out.rsplit("SAMPLE app-running", 1)[-1].split("SAMPLES DONE")[0]
    vals = re.findall(r"(?m)^\s*(0x[0-9A-Fa-f]{8})\s*$", tail)
    if "SAMPLE app-running" in out and len(vals) >= n:
        found["app-running"] = [int(v, 16) for v in vals[:n]]
    return {tag: {p: tuple(v[3 * i:3 * i + 3]) for i, p in enumerate(ports)} for tag, v in found.items()}


def judge(out, uart4, tags, pins, ports, bor_level):
    got = samples(out, ports)
    checks = [("bootloader jumped into the app", "APP Mod" in uart4)]
    for tag in tags:
        if tag not in got:
            checks.append(("%s: sampled" % tag, False))
            continue
        bad = []
        for name, port, pin in pins:
            moder, otyper, odr = got[tag][port]
            if (moder >> 2 * pin) & 3 != 1 or (otyper >> pin) & 1 or (odr >> pin) & 1:
                bad.append("%s P%s%d MODER=%d OTYPER=%d ODR=%d" % (name, port, pin, (moder >> 2 * pin) & 3,
                                                                   (otyper >> pin) & 1, (odr >> pin) & 1))
        checks.append(("%s: %d pins push-pull low%s" % (tag, len(pins), (" -- " + "; ".join(bad)) if bad else ""),
                       not bad))
    m = re.search(r"OPTSR_CUR\s+(0x[0-9A-Fa-f]+)", out)
    level = (int(m.group(1), 16) >> BOR_LEV_SHIFT) & 3 if m else None
    want = 0 if bor_level is None else bor_level
    checks.append(("OPTSR_CUR reads BOR_LEV %d (got %s)" % (want, level), level == want))
    if want == 3:
        checks.append(("no BOR warning on UART4", BOR_WARNING not in uart4))
    else:
        checks.append(("BOR warning on UART4 names level %d" % want, "%s %d " % (BOR_WARNING, want) in uart4))
    return checks


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
    pins = safe_pins()
    if len(pins) != 11:
        Fail("expected 11 pins in safe_outputs.c k_pins[], parsed %d" % len(pins))
        return 2
    ports = sorted({port for _, port, _ in pins})

    # Unique per run: other Renode runs may be going on at the same time.
    root = Path(tempfile.mkdtemp(prefix="renode_t1-37_", dir=str(get_scratch_dir())))
    boot_bin = build_copy(root / "boot")
    if boot_bin is None:
        Fail("FAIL - the bootloader does not build; files in %s" % root)
        return 1
    boot_elf = boot_bin.with_suffix(".elf")

    Section("Build: %s" % APP_EXAMPLE)
    build = root / "app"
    out, rc = run_capture([arduino_cli, "compile", "--config-file", cli_config, "--fqbn", FQBN,
                           "--build-path", build, example])
    if rc != 0:
        Fail("FAIL - %s does not compile:\n%s" % (APP_EXAMPLE, out[-2000:]))
        return 1
    app_bin = build / (APP_EXAMPLE + ".ino.bin")
    boot_sym = symbols(nm[-1], boot_elf)
    app_sym = symbols(nm[-1], build / (APP_EXAMPLE + ".ino.elf"))

    failed = False
    for label, bor_level in (("option bytes at reset value", None), ("BOR_LEV programmed to 3", 3)):
        Section("Renode: %s" % label)
        work = root / ("bor-%s" % ("reset" if bor_level is None else bor_level))
        work.mkdir()
        flash_banks(boot_bin, app_bin, get_iap_tool(), work)
        out, uart4, tags = run_renode(work, boot_sym, app_sym, ports, bor_level)
        for name, ok in judge(out, uart4, tags, pins, ports, bor_level):
            (Ok if ok else Fail)("  %s %s" % ("ok  " if ok else "FAIL", name))
            failed |= not ok

    Section("result")
    if failed:
        Fail("FAIL - Renode output in %s" % root)
        return 1
    shutil.rmtree(str(root), ignore_errors=True)
    Ok("outputs low from reset into the app; BOR check reads the option bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
