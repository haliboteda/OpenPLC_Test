"""Runs the board-package examples in Renode and judges what they do. Case T3-11.

    python behaviour.py                  all of them
    python behaviour.py --only DI        only examples whose name matches
    python behaviour.py --boot BIN       use this bootloader image instead of building one

Builds the current $BOOT source once, compiles each example with the USB menu
set to "CDC (no generic 'Serial')" so Serial lands on UART4, lays out flash as
T3-05 does, gives the example its inputs in Renode and judges its outputs.
Criteria, what differs from the real board and what this cannot see:
$PROD/docs/modules/M3-app-runtime.md, T3-11 and footnote 6.

Exit 0 = every example passed, 1 = at least one failed, 2 = prerequisites missing.
"""

import argparse
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "tools"))

from build_image import build_copy  # noqa: E402
from common import EXE, Fail, Ok, Section, cfg, get_iap_tool, get_scratch_dir, read_text, run_capture  # noqa: E402
from run import FQBN, PLATFORM, RENODE_TIMEOUT, flash_banks, symbols  # noqa: E402
import netpeer  # noqa: E402
import ymodem  # noqa: E402

# Serial on UART4 instead of USB CDC: $PROD/maps/sim-coverage/SIM-01-findings.md.
FQBN_SIM = FQBN.replace("usb=CDCgen", "usb=CDC")

GPIO_BASE = {"A": 0x58020000, "B": 0x58020400, "C": 0x58020800, "D": 0x58020C00, "E": 0x58021000,
             "F": 0x58021400, "G": 0x58021800, "H": 0x58021C00, "I": 0x58022000}
ODR = 0x14
DAC_DHR12R1, DAC_DHR12R2 = 0x40007408, 0x40007414
SD_IMAGE_SIZE = 64 << 20
# The app's setup() starts about 6.6 s of virtual time after reset.
BOOT_S = 8
BRIDGE_CS = HERE / "udp_frame_bridge.cs"

# LAN8742A as in Renode's nucleo_h753zi.repl: $PROD/maps/sim-coverage/SIM-02-findings.md.
PHY = ("phy: Network.EthernetPhysicalLayer @ ethernet 0 { BasicControl: 0x3100; BasicStatus: 0x782D; "
       "Id1: 0x0007; Id2: 0xC130; AutoNegotiationAdvertisement: 0x01E1; "
       "AutoNegotiationLinkPartnerBasePageAbility: 0x0001; AutoNegotiationExpansion: 0x0064; "
       "AutoNegotiationNextPageTransmit: 0x2001; VendorSpecific15: 0x1058 }")


def variant_pins():
    """{'DOUT_1': ('B', 13), ...} from the variant header the examples are built with."""
    src = read_text(Path(cfg.CORE_LIVE) / "variants" / "STM32H7xx" / "H743" / "variant_PLC_H743.h")
    return {m.group(1): (m.group(2), int(m.group(3)))
            for m in re.finditer(r"#define\s+(DOUT_\d|REL_\d|DIN_\d)\s+P([A-I])(\d+)\b", src)}


def mkfs_fat32(path, size):
    """A blank FAT32 volume without a partition table, one 512-byte sector per cluster."""
    total, rsvd, nfats = size // 512, 32, 2
    fatsz = 1
    while True:
        need = ((total - rsvd - nfats * fatsz + 2) * 4 + 511) // 512
        if need <= fatsz:
            break
        fatsz = need
    bs = bytearray(512)
    bs[0:11] = b"\xEB\x58\x90MSWIN4.1"
    struct.pack_into("<HBHBHHBHHHII", bs, 11, 512, 1, rsvd, nfats, 0, 0, 0xF8, 0, 32, 64, 0, total)
    struct.pack_into("<IHHIHH", bs, 36, fatsz, 0, 0, 2, 1, 6)
    struct.pack_into("<BBBI", bs, 64, 0x80, 0, 0x29, 0x0B1C0313)
    bs[71:90] = b"OPENPLC    FAT32   "
    bs[510:512] = b"\x55\xAA"
    fsinfo = bytearray(512)
    struct.pack_into("<I", fsinfo, 0, 0x41615252)
    struct.pack_into("<III", fsinfo, 484, 0x61417272, 0xFFFFFFFF, 0xFFFFFFFF)
    struct.pack_into("<I", fsinfo, 508, 0xAA550000)
    with open(path, "wb") as f:
        f.truncate(size)
        for at in (0, 6):
            f.seek(at * 512)
            f.write(bs + fsinfo)
        for n in range(nfats):
            f.seek((rsvd + n * fatsz) * 512)
            f.write(struct.pack("<III", 0x0FFFFFF8, 0x0FFFFFFF, 0x0FFFFFFF))


def free_port(kind=socket.SOCK_STREAM):
    """A port the OS hands out now; ask for the same kind the caller binds,
    since Windows reserves different TCP and UDP ranges."""
    s = socket.socket(socket.AF_INET, kind)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# --- the Renode script -------------------------------------------------------

def run_for(seconds):
    ms = int(round(seconds * 1000))
    return 'emulation RunFor "%s.%03d"' % (time.strftime("%H:%M:%S", time.gmtime(ms // 1000)), ms % 1000)


def sample(tag, addrs):
    """Monitor lines that print 'S <tag>' followed by one value per address."""
    return ['echo "S %s"' % tag] + ["sysbus ReadDoubleWord 0x%08X" % a for a in addrs]


def samples(console, n):
    """{tag: [value, ...]} for every sample() block in the console output."""
    found = {}
    parts = re.split(r"(?m)^S (\S+)\s*$", console)
    for tag, body in zip(parts[1::2], parts[2::2]):
        vals = re.findall(r"(?m)^\s*(0x[0-9A-Fa-f]{1,8})\s*$", body)
        if len(vals) >= n:
            found[tag] = [int(v, 16) for v in vals[:n]]
    return found


def uart_write(uart, data):
    return ["sysbus.%s WriteChar 0x%02X" % (uart, b) for b in data]


def gpio_set(port, pin, high):
    return ["sysbus.gpioPort%s OnGPIO %d %s" % (port, pin, "true" if high else "false")]


class Example:
    """One example: what it needs, what to do after boot, and how to judge it."""

    def __init__(self, name, lib="OpenPLC_Ports", platform=(), real_time=False):
        self.name, self.lib, self.platform, self.real_time = name, lib, list(platform), real_time

    def script(self, w, sym):        # monitor lines after the header
        return []

    def peers(self, w):              # (start, stop) callables run around Renode
        return []

    def judge(self, w):              # [(check, ok)]
        return []


class Work:
    """One example's run directory and what Renode left in it."""

    def __init__(self, path):
        self.path = path
        self.console = ""
        self.ports = {}

    def text(self, name):
        p = self.path / name
        return p.read_text(errors="replace") if p.exists() else ""


def header(w, ex):
    p = w.path.as_posix()
    lines = ['mach create "plc"', "machine LoadPlatformDescription @%s" % PLATFORM.as_posix()]
    lines += ['machine LoadPlatformDescriptionFromString "%s"' % s for s in ex.platform]
    lines += [
        "sysbus WriteDoubleWord 0x1FF1E800 0",
        "sysbus WriteDoubleWord 0x1FF1E804 0",
        "sysbus WriteDoubleWord 0x1FF1E808 0",
        "sysbus LoadBinary @%s/bank1.bin 0x08000000" % p,
        "sysbus LoadBinary @%s/bank2.bin 0x08100000" % p,
        "cpu VectorTableOffset 0x08000000",
        "uart4 CreateFileBackend @%s/uart4.txt true" % p,
        "usart2 CreateFileBackend @%s/usart2.txt true" % p,
    ]
    if ex.real_time:
        # About as fast as the wall clock, so a peer on the host keeps pace
        # ($PROD/maps/sim-coverage/SIM-03-findings.md).
        lines.append("cpu PerformanceInMips 10")
    return lines


def run_renode(w, ex, sym):
    p = w.path.as_posix()
    lines = header(w, ex) + ex.script(w, sym) + ['echo "END"', "q"]
    (w.path / "run.resc").write_text("\n".join(lines) + "\n")
    peers = ex.peers(w)
    for start, _ in peers:
        start()
    # stdin stays open until Renode quits (see run.py); a failing command leaves
    # Renode at its prompt, so a timer kills it.
    proc = subprocess.Popen([cfg.RENODE, "--disable-gui", "--console", "-e", "include @%s/run.resc" % p],
                            cwd=str(Path(cfg.RENODE).parent), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, errors="replace")
    timer = threading.Timer(RENODE_TIMEOUT, proc.kill)
    timer.start()
    out = proc.stdout.read()
    proc.wait()
    timer.cancel()
    for _, stop in peers:
        stop()
    w.console = re.sub(r"\x1b\[[0-9;]*m", "", out)
    (w.path / "console.txt").write_text(w.console, encoding="utf-8")


# --- the examples ------------------------------------------------------------

PINS = {}


def ordered(text, regexes):
    """True if every regex matches, each after the previous one's match."""
    at = 0
    for rx in regexes:
        m = re.compile(rx).search(text, at)
        if not m:
            return False
        at = m.end()
    return True


class DIInputs(Example):
    def bitmap(self, n):
        return r"DI1\.\.DI8: %s\r?\n" % " ".join("1" if i == n else "0" for i in range(1, 9))

    def script(self, w, sym):
        lines = [run_for(BOOT_S)]
        for n in range(1, 9):
            port, pin = PINS["DIN_%d" % n]
            lines += gpio_set(port, pin, True) + [run_for(0.3)] + gpio_set(port, pin, False) + [run_for(0.3)]
        return lines

    def judge(self, w):
        u = w.text("uart4.txt")
        seq = [self.bitmap(0)]
        for n in range(1, 9):
            seq += [self.bitmap(n), self.bitmap(0)]
        return [("banner", "DI_Inputs: prints DI1..DI8 on every change." in u),
                ("each input alone, in order, then back to all 0", ordered(u, seq))]


class Sequence(Example):
    """Outputs that turn on one at a time, about 1 s each, in order."""

    def __init__(self, name, prefix, count, line, last):
        Example.__init__(self, name)
        self.prefix, self.count, self.line, self.last = prefix, count, line, last

    def pins(self):
        return [PINS["%s_%d" % (self.prefix, n)] for n in range(1, self.count + 1)]

    def ports(self):
        return sorted({port for port, _ in self.pins()})

    def script(self, w, sym):
        lines = [run_for(BOOT_S)]
        addrs = [GPIO_BASE[p] + ODR for p in self.ports()]
        for i in range(int(2 * self.count * 1.2 / 0.25)):
            lines += [run_for(0.25)] + sample("t%03d" % i, addrs)
        return lines

    def judge(self, w):
        ports = self.ports()
        got = samples(w.console, len(ports))
        on = []
        for tag in sorted(got):
            odr = dict(zip(ports, got[tag]))
            on.append([n + 1 for n, (port, pin) in enumerate(self.pins()) if (odr[port] >> pin) & 1])
        runs = []
        for highs in on:
            key = highs[0] if len(highs) == 1 else (0 if not highs else -1)
            if runs and runs[-1][0] == key:
                runs[-1][1] += 1
            else:
                runs.append([key, 1])
        lit = [k for k, _ in runs if k > 0]
        # Count from the first output 1 seen, past one full round.
        start = lit.index(1) if 1 in lit else len(lit)
        cycle = lit[start:start + self.count]
        long_enough = all(c >= 3 for k, c in runs[1:-1] if k > 0)
        u = w.text("uart4.txt")
        return [("%d samples read" % len(on), len(on) > 0),
                ("never more than one on at a time", all(len(h) <= 1 for h in on)),
                ("%s1..%s%d in order" % (self.line, self.line, self.count), cycle == list(range(1, self.count + 1))),
                ("each on for about a second (>= 3 samples of 250 ms)", long_enough),
                ("prints '%s'" % self.last, self.last in u)]


class SystemLED(Example):
    def script(self, w, sym):
        lines = [run_for(BOOT_S)]
        for i in range(16):
            lines += [run_for(0.25)] + sample("t%03d" % i, [GPIO_BASE["E"] + ODR])
        return lines

    def judge(self, w):
        got = samples(w.console, 1)
        led = [(got[t][0] >> 2) & 1 for t in sorted(got)]
        flips = sum(1 for a, b in zip(led, led[1:]) if a != b)
        u = w.text("uart4.txt")
        return [("PE2 changes every half second: 6 to 9 changes in 4 s (got %d)" % flips, 6 <= flips <= 9),
                ("prints 'on' and 'off'", ordered(u, [r"\bon\r?\n", r"\boff\r?\n"]))]


class AOOutputs(Example):
    def script(self, w, sym):
        hooks = ['sysbus AddWatchpointHook 0x%08X DoubleWord Write "self.InfoLog(\'DAC%d \' + str(value))"'
                 % (a, ch) for ch, a in ((1, DAC_DHR12R1), (2, DAC_DHR12R2))]
        return hooks + [run_for(BOOT_S + 16)]

    def judge(self, w):
        u = w.text("uart4.txt")
        # openplc_analog.c, nominal conversion (the simulated board has no calibration).
        want = [int(ma * 102.4 * 4095 / 2500.0 + 0.5) for ma in (0, 5, 10, 15, 20)]
        checks = []
        for ch in (1, 2):
            vals = [int(v) for v in re.findall(r"DAC%d (\d+)" % ch, w.console)]
            steps = [v for i, v in enumerate(vals) if i == 0 or v != vals[i - 1]]
            ok = len(steps) >= 5 and all(abs(a - b) <= 1 for a, b in zip(steps[:5], want))
            checks.append(("AO%d codes %s (want %s)" % (ch, steps[:5], want), ok))
        checks.append(("prints the 20 mA step", "AO1 = AO2 = 20.0 mA" in u))
        return checks


class BoardTemperature(Example):
    # LM50: 500 mV at 0 C, 10 mV per degree. ADC1 INP16 = protection, INP15 = output switches.
    VOLTS = {16: 0.750, 15: 0.800}

    def script(self, w, sym):
        return ["sysbus.adcM1S2 SetVoltage %d %d" % (int(v * 1e6), ch) for ch, v in self.VOLTS.items()] + [
            run_for(BOOT_S + 3)]

    def judge(self, w):
        u = w.text("uart4.txt")
        m = re.findall(r"protection = (-?[\d.]+) C   output switches = (-?[\d.]+) C", u)
        prot, sw = (float(m[-1][0]), float(m[-1][1])) if m else (None, None)
        return [("protection reads 25 C +-1 (got %s)" % prot, prot is not None and abs(prot - 25) <= 1),
                ("output switches read 30 C +-1 (got %s)" % sw, sw is not None and abs(sw - 30) <= 1)]


class Echo(Example):
    def __init__(self, name, uart, send, expect):
        Example.__init__(self, name)
        self.uart, self.send, self.expect = uart, send, expect

    def script(self, w, sym):
        lines = [run_for(BOOT_S)]
        if self.uart == "usart3":
            lines.insert(0, "usart3 CreateFileBackend @%s/usart3.txt true" % w.path.as_posix())
        return lines + uart_write(self.uart, self.send) + [run_for(1)]

    def judge(self, w):
        return [("%s shows /%s/" % (f, rx), re.search(rx, w.text(f)) is not None) for f, rx in self.expect]


class CANCounter(Example):
    # A frame from another node, delivered straight to FDCAN1 (0x4000A000); there
    # is no CAN bridge on Windows.
    INJECT = ("python \"from Antmicro.Renode.Core import EmulationManager; "
              "from Antmicro.Renode.Core.CAN import CANMessageFrame; from System import Array, Byte; "
              "m = EmulationManager.Instance.CurrentEmulation.Machines[0]; "
              "m.SystemBus.WhatPeripheralIsAt(0x4000A000).OnFrameReceived("
              "CANMessageFrame(0x456, Array[Byte]([0xBE, 0xEF]), False, False, False, False))\"")

    def script(self, w, sym):
        return [run_for(BOOT_S + 3), self.INJECT, run_for(1)]

    def judge(self, w):
        u = w.text("uart4.txt")
        counts = [int(c) for c in re.findall(r"TX id=0x123 counter=(\d+)\r?\n", u)]
        return [("banner", "CAN_Counter: sending ID 0x123 every second" in u),
                ("TX counter goes up, 3 or more frames (got %s)" % counts[:6],
                 len(counts) >= 3 and counts == list(range(counts[0], counts[0] + len(counts)))),
                ("no TX queue full", "(TX queue full)" not in u),
                ("the injected frame is printed", re.search(r"RX id=0x456 len=2 data=BE EF", u) is not None)]


class EthernetIP(Example):
    def __init__(self):
        Example.__init__(self, "Ethernet_IP", platform=[PHY], real_time=True)

    def script(self, w, sym):
        w.ports["bridge"], w.ports["peer"] = free_port(socket.SOCK_DGRAM), free_port(socket.SOCK_DGRAM)
        return ["include @%s" % BRIDGE_CS.as_posix(),
                'emulation CreateUdpFrameBridge "br" %d %d' % (w.ports["bridge"], w.ports["peer"]),
                'emulation CreateSwitch "sw"',
                "connector Connect br sw",
                "connector Connect sysbus.ethernet sw",
                run_for(BOOT_S + 25)]

    def peers(self, w):
        stop = threading.Event()
        w.peer_log = []
        t = threading.Thread(target=netpeer.serve, args=(w.ports["peer"], w.ports["bridge"], w.peer_log, stop),
                             daemon=True)
        return [(t.start, lambda: (stop.set(), t.join(5)))]

    def judge(self, w):
        u = w.text("uart4.txt")
        replies = [r for r in w.peer_log if r.startswith("reply ")]
        return [("prints 'link up'", "link up" in u),
                ("prints the address the DHCP peer gave (%s)" % netpeer.GUEST,
                 ("address %s" % netpeer.GUEST) in u),
                ("answers UDP discovery (%d replies)" % len(replies), len(replies) > 0)]


class SDReadWrite(Example):
    def script(self, w, sym):
        img = w.path / "sd.img"
        mkfs_fat32(img, SD_IMAGE_SIZE)
        return ["machine SdCardFromFile @%s sysbus.sdmmc 0x%X true" % (img.as_posix(), SD_IMAGE_SIZE),
                run_for(BOOT_S + 3)]

    def judge(self, w):
        u = w.text("uart4.txt")
        return [("reads back what it wrote", "Read back: hello from OpenPLC" in u),
                ("prints 'SD_ReadWrite: OK'", "SD_ReadWrite: OK" in u)]


class ThrottledSocket:
    """The serial-port shape ymodem.py wants, over Renode's socket terminal.

    Renode's UART does not pace bytes at the baud rate; a whole packet at once
    overruns the core's 64-byte receive buffer ($PROD/maps/sim-coverage/SIM-03-findings.md).
    """

    def __init__(self, port):
        self.s = socket.create_connection(("127.0.0.1", port), timeout=30)
        self.s.settimeout(0.2)

    def read(self, n):
        try:
            return self.s.recv(n)
        except socket.timeout:
            return b""

    def write(self, data):
        for i in range(0, len(data), 32):
            self.s.sendall(data[i:i + 32])
            time.sleep(0.05)

    def close(self):
        self.s.close()


class SDFileReceive(Example):
    NAME = "EXB.BIN"
    DATA = bytes((i * 7 + 3) & 0xFF for i in range(3000))

    def __init__(self):
        Example.__init__(self, "SD_FileReceive", real_time=True)

    def script(self, w, sym):
        img = w.path / "sd.img"
        mkfs_fat32(img, SD_IMAGE_SIZE)
        w.ports["rs232"] = free_port()
        port, pin = "E", 6   # SDMMC_CD_Pin, low = card in
        return ["machine SdCardFromFile @%s sysbus.sdmmc 0x%X true" % (img.as_posix(), SD_IMAGE_SIZE),
                'emulation CreateServerSocketTerminal %d "rs232" false' % w.ports["rs232"],
                "connector Connect sysbus.usart3 rs232",
                run_for(BOOT_S + 50)] + gpio_set(port, pin, True) + [run_for(2)] + gpio_set(port, pin, False) + [
                run_for(2)]

    def peers(self, w):
        w.sent = None

        def send():
            for _ in range(600):
                try:
                    sp = ThrottledSocket(w.ports["rs232"])
                    break
                except OSError:
                    time.sleep(0.1)
            else:
                return
            w.sent = ymodem.send(sp, self.NAME, self.DATA, start_timeout=120)
            sp.close()

        t = threading.Thread(target=send, daemon=True)
        return [(t.start, lambda: t.join(5))]

    def judge(self, w):
        u = w.text("uart4.txt")
        want = r"SD: wrote %s, %d bytes, crc32=%08X" % (re.escape(self.NAME), len(self.DATA), zlib.crc32(self.DATA))
        return [("prints 'SD: card inserted' at start", "SD: card inserted" in u),
                ("YMODEM send completed", w.sent is True),
                ("the file read back from the card has the sent length and CRC-32", re.search(want, u) is not None),
                ("card out, then in again, is reported",
                 ordered(u, [want, r"SD: card removed", r"SD: card inserted"]))]


class SDRAM(Example):
    def __init__(self, name, steps, expect):
        Example.__init__(self, name, lib="OpenPLC_SDRAM")
        self.steps, self.expect = steps, expect

    def script(self, w, sym):
        return ["usart3 CreateFileBackend @%s/usart3.txt true" % w.path.as_posix()] + self.steps

    def judge(self, w):
        t = w.text("usart3.txt")
        return [("RS232 shows /%s/" % rx, re.search(rx, t) is not None) for rx in self.expect]


EXAMPLES = [
    DIInputs("DI_Inputs"),
    Sequence("DO_Outputs", "DOUT", 8, "DO", "DO8 on"),
    Sequence("Relays", "REL", 6, "RY", "RY6 closed"),
    SystemLED("SystemLED"),
    AOOutputs("AO_Outputs"),
    BoardTemperature("BoardTemperature"),
    Echo("RS232_Echo", "usart3", b"EXB-PING-3\n",
         [("usart3.txt", r"EXB-PING-3\n"), ("uart4.txt", r"RS232 got 0x45")]),
    Echo("RS485_Echo", "usart2", b"EXB-PING-1\n",
         [("usart2.txt", r"EXB-PING-1\n"), ("uart4.txt", r"RS485 echoed: EXB-PING-1")]),
    Echo("USB_Serial", "uart4", b"EXB-PING-2\n", [("uart4.txt", r"1: EXB-PING-2")]),
    CANCounter("CAN_Counter"),
    EthernetIP(),
    SDReadWrite("SD_ReadWrite"),
    SDFileReceive(),
    SDRAM("SDRAM_Basic", [run_for(BOOT_S + 3)],
          [r"SDRAM ready, \d+ MB free", r"wrote and verified \d+ words, mismatches: 0\r?\n"]),
    SDRAM("SDRAM_DataLogger", [run_for(BOOT_S + 3)] + uart_write("usart3", b"d") + [run_for(3)],
          [r"=== SDRAM data logger ===", r"press 'd' for a dump", r"samples held: \d+"]),
]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", default="")
    ap.add_argument("--boot", default="", help="a bootloader image to use instead of building $BOOT")
    args = ap.parse_args()

    arduino_cli = getattr(cfg, "ARDUINO_CLI", "")
    cli_config = getattr(cfg, "ARDUINO_CLI_CONFIG", "")
    tools = Path(cfg.A15) / "packages" / "OpenPLC_Alpha" / "tools"
    nm = sorted(tools.glob("xpack-arm-none-eabi-gcc/*/bin/arm-none-eabi-nm" + EXE))
    missing = [
        (not getattr(cfg, "RENODE", "") or not Path(cfg.RENODE).exists(), "Renode not found. Set $RENODE in config/machine.py"),
        (not arduino_cli or not Path(arduino_cli).exists(), "arduino-cli not found. Set $ARDUINO_CLI in config/machine.py"),
        (not cli_config or not Path(cli_config).exists(), "arduino-cli config not found at %s" % cli_config),
        (not nm, "arm-none-eabi-nm not found under %s" % tools),
        (args.boot and not Path(args.boot).exists(), "--boot %s not found" % args.boot),
    ]
    if any(bad for bad, _ in missing):
        for bad, why in missing:
            if bad:
                Fail(why)
        return 2
    chosen = [e for e in EXAMPLES if args.only.lower() in e.name.lower()]
    if not chosen:
        Fail("no example matches '%s'" % args.only)
        return 2
    PINS.update(variant_pins())

    # Unique per run: other Renode runs may be going on at the same time.
    root = Path(tempfile.mkdtemp(prefix="renode_t3-11_", dir=str(get_scratch_dir())))
    boot_bin = Path(args.boot) if args.boot else build_copy(root / "boot")
    if boot_bin is None:
        Fail("FAIL - the bootloader does not build; files in %s" % root)
        return 1

    failed = []
    for ex in chosen:
        Section(ex.name)
        w = Work(root / ex.name)
        build = w.path / "build"
        sketch = Path(cfg.CORE_LIVE) / "libraries" / ex.lib / "examples" / ex.name
        out, rc = run_capture([arduino_cli, "compile", "--config-file", cli_config, "--fqbn", FQBN_SIM,
                               "--build-path", build, sketch])
        if rc != 0:
            Fail("FAIL - does not compile with usb=CDC:\n%s" % out[-1500:])
            failed.append(ex.name)
            continue
        flash_banks(boot_bin, build / (ex.name + ".ino.bin"), get_iap_tool(), w.path)
        run_renode(w, ex, symbols(nm[-1], build / (ex.name + ".ino.elf")))
        checks = [("bootloader jumped into the app", "APP Mod" in w.text("uart4.txt"))] + ex.judge(w)
        for name, ok in checks:
            (Ok if ok else Fail)("  %s %s" % ("ok  " if ok else "FAIL", name))
        if all(ok for _, ok in checks):
            Ok("PASS")
        else:
            Fail("FAIL - Renode output in %s" % w.path)
            failed.append(ex.name)

    Section("result")
    print("  %d example(s), %d failed" % (len(chosen), len(failed)))
    if failed:
        Fail("failed: " + ", ".join(failed))
        return 1
    shutil.rmtree(str(root), ignore_errors=True)
    Ok("every example behaves as its header says, as far as Renode can show")
    return 0


if __name__ == "__main__":
    sys.exit(main())
