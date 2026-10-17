"""R3-02 on the real board: the app starts on a cold board (decision 4).

    python tools/run_r3_02.py

GDB over ST-Link takes one register snapshot at the bootloader's Reset_Handler
(the cold reference) and one at the app's Reset_Handler (the handover), then
compares them. Allowed differences, and nothing else:
  - VTOR points at the app, RCC_RSR is cleared (the jump itself)
  - safe_outputs.c's pins are push-pull low and their ports clocked (decision 81)
Status-only registers (RCC_CR, PWR_CSR1) and GPIO IDR are not compared.

Not covered: peripherals outside the snapshot list below.
See $PROD/docs/modules/M3-app-runtime.md R3-02.

Exit 0 = pass, 1 = fail, 2 = setup problem.
"""

import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from common import cfg, Section, Ok, Fail, EXE, _newest, cube_layout, get_programmer_cli  # noqa: E402

GDB_PORT = 61235
PLUGINS = cube_layout(cfg.CUBEIDE)[0]
APP_BASE = 0x08020000

GPIO_BASE = {c: 0x58020000 + 0x400 * i for i, c in enumerate("ABCDEFGHIJK")}
# (label, address, words)
SNAP = ([("RCC_RSR", 0x580244D0, 1), ("RCC_ENR", 0x580244D4, 9)]
        + [("GPIO" + c, a, 6) for c, a in GPIO_BASE.items()]
        + [("NVIC_ISER", 0xE000E100, 8), ("NVIC_ISPR", 0xE000E200, 8),
           ("SysTick", 0xE000E010, 3), ("VTOR", 0xE000ED08, 1), ("CCR", 0xE000ED14, 1),
           ("MPU_CTRL", 0xE000ED94, 1), ("UART4_CR1", 0x40004C00, 1),
           ("ETH_MACCR", 0x40028000, 1), ("FMC_BCR1", 0x52004000, 1),
           ("FMC_SDCR1", 0x52004140, 1), ("OTG_HS", 0x40040008, 1),
           ("OTG_FS", 0x40080008, 1), ("PWR_CR1", 0x58024800, 1),
           ("FLASH_ACR", 0x52002000, 1)])
AHB4ENR = 0x580244E0
GPIO_IDR = 4


def safe_pins():
    src = (Path(cfg.BOOT_REPO) / "IAPServer" / "safe_outputs.c").read_text()
    return [(p, int(n)) for p, n in re.findall(r"\{\s*GPIO([A-K]),\s*(\d+)U\s*\}", src)]


def gdb_script(app_reset):
    dump = "".join("  x/%dwx 0x%08x\n" % (n, a) for _, a, n in SNAP)
    return ("set pagination off\nset confirm off\ntarget remote :%d\n"
            "monitor reset\nprintf \"PC %%08x\\n\", $pc\n"
            "define snap\n%send\n"
            "printf \"=== cold\\n\"\nsnap\n"
            "hbreak *0x%08x\ncontinue\n"
            "printf \"PC %%08x\\n\", $pc\nprintf \"=== hand\\n\"\nsnap\n"
            "delete\ndetach\nquit\n") % (GDB_PORT, dump, app_reset)


def parse(text):
    words = {}
    for line in text.splitlines():
        m = re.match(r"\s*0x([0-9a-f]+):\s+((?:0x[0-9a-f]+\s*)+)$", line)
        if m:
            base = int(m.group(1), 16)
            for i, w in enumerate(m.group(2).split()):
                words[base + 4 * i] = int(w, 16)
    return words


def expected(cold, pins):
    """Cold values with the two deliberate differences applied."""
    want = dict(cold)
    want[0xE000ED08] = APP_BASE
    want[0x580244D0] = 0
    # An unclocked GPIO port does not read back its registers, so the ports
    # the handover clocks are compared against their reset values (RM0433 GPIO
    # register map): MODER, OTYPER, OSPEEDR, PUPDR, (IDR), ODR.
    for port in {p for p, _ in pins}:
        b = GPIO_BASE[port]
        reset = {"A": (0xABFFFFFF, 0, 0x0C000000, 0x64000000),
                 "B": (0xFFFFFEBF, 0, 0x000000C0, 0x00000100)}.get(
                     port, (0xFFFFFFFF, 0, 0, 0))
        for i, v in enumerate(reset):
            want[b + 4 * i] = v
        want[b + 20] = 0
    for port, n in pins:
        b = GPIO_BASE[port]
        want[AHB4ENR] |= 1 << "ABCDEFGHIJK".index(port)
        want[b] = (want[b] & ~(3 << 2 * n)) | (1 << 2 * n)       # MODER: output
        want[b + 4] &= ~(1 << n)                                  # OTYPER: push-pull
        want[b + 12] &= ~(3 << 2 * n)                             # PUPDR: none
        want[b + 20] &= ~(1 << n)                                 # ODR: low
    return want


def label(addr):
    for name, a, n in SNAP:
        if a <= addr < a + 4 * n:
            return "%s+%d" % (name, (addr - a) // 4) if n > 1 else name
    return "?"


def main():
    gdbserver = _newest(PLUGINS / ("com.st.stm32cube.ide.mcu.externaltools.stlink-gdb-server.*"
                                   "/tools/bin/ST-LINK_gdbserver" + EXE))
    gdb = _newest(PLUGINS / ("com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.*"
                             "/tools/bin/arm-none-eabi-gdb" + EXE))
    if not (gdbserver and gdb):
        Fail("need ST-LINK_gdbserver and arm-none-eabi-gdb from STM32CubeIDE")
        return 2
    pins = safe_pins()
    if not pins:
        Fail("no pins parsed from safe_outputs.c")
        return 2

    out_dir = Path(cfg.TEST_REPO) / "Output" / "r3_02"
    out_dir.mkdir(parents=True, exist_ok=True)
    srv = subprocess.Popen([str(gdbserver), "-p", str(GDB_PORT), "-d", "-e",
                            "-cp", str(Path(get_programmer_cli()).parent)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(3)
        # The app's reset vector, read from its vector table before anything runs.
        probe = out_dir / "vec.gdb"
        probe.write_text("target remote :%d\nx/1wx 0x%08x\ndetach\nquit\n"
                         % (GDB_PORT, APP_BASE + 4))
        vec = subprocess.run([str(gdb), "-batch", "-x", str(probe)], capture_output=True,
                             text=True, errors="replace", timeout=60).stdout
        app_reset = parse(vec).get(APP_BASE + 4)
        if not app_reset:
            print(vec)
            Fail("could not read the app's reset vector")
            return 2
        script = out_dir / "r3_02.gdb"
        script.write_text(gdb_script(app_reset & ~1))
        out = subprocess.run([str(gdb), "-batch", "-x", str(script)], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT,
                             text=True, errors="replace", timeout=120).stdout
    finally:
        srv.kill()
    (out_dir / "r3_02.log").write_text(out)

    if "=== hand" not in out:
        print(out[-1500:])
        Fail("never reached the app's Reset_Handler")
        return 2
    cold_txt, hand_txt = out.split("=== hand", 1)
    cold, hand = parse(cold_txt.split("=== cold", 1)[1]), parse(hand_txt)
    want = expected(cold, pins)

    Section("handover vs cold board (%d words)" % len(cold))
    bad = 0
    for addr in sorted(cold):
        if any(a == addr - 4 * GPIO_IDR for a in GPIO_BASE.values()):
            continue
        if hand.get(addr) != want[addr]:
            bad += 1
            print("  %-12s 0x%08x  cold %08x  expected %08x  handover %08x"
                  % (label(addr), addr, cold[addr], want[addr], hand.get(addr, 0)))
    if bad:
        Fail("%d word(s) differ from a cold board beyond decisions 4 / 81" % bad)
        return 1
    Ok("handover matches a cold board except VTOR, RCC_RSR and the %d safe outputs"
       % len(pins))
    return 0


if __name__ == "__main__":
    sys.exit(main())
