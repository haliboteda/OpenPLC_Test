"""Boots every OpenPLC_Ports example through the real bootloader in Renode. Case T3-05.

    python run.py                   all of them
    python run.py --only CAN        only examples whose name matches

Per example: compile it, lay out flash as after a claim and one successful
upload (the bootloader, the signed app, a claimed root area, one metadata
record, the sector-15 marker), run Renode, and judge that the
bootloader jumped into the app, setup() ran once, loop() is still being entered
at the end, and nothing faulted. Criteria and what this cannot see:
$PROD/docs/modules/M3-app-runtime.md, "测试怎么跑".

Exit 0 = every example passed, 1 = at least one failed, 2 = prerequisites
missing.
"""

import argparse
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "tools"))

from common import EXE, Fail, Ok, Section, Warn, cfg, get_iap_tool, get_scratch_dir, run_capture  # noqa: E402
from flash_bootloader import looks_like_bootloader  # noqa: E402

FQBN = ("OpenPLC_Alpha:stm32:OPEN-PLC:pnum=PLC_H743,usb=CDCgen,xusb=FS,"
        "upload_method=ethMethod,knxrole=dual_device")

FLASH, FLASH_SIZE = 0x08000000, 2 << 20
APP = 0x08020000
# Sector-15 layout: $BOOT/IAPServer/bootloader_state.c and owner_slot.h. The
# bootloader re-verifies all of it at every boot, so a drift shows up here as
# "did not jump into the app".
ROOT_AREA = 0x081E2000
META = 0x081E4000
MARKER = 0x081FFFE0
REC_METADATA, REC_SLOTS = 0x4D, 7
OWNER_FORMAT_VER = 4
# The UID the Renode run pins (run_renode()); the owner record names that board.
RENODE_UID = bytes(12)

# The bootloader spends about 6.5 s of virtual time in its boot-relay window.
RUN_SECONDS = 15
# Second run with a fresh hook on loop(): proves loop() is still being entered.
TAIL_SECONDS = 10

SD_IMAGE_SIZE = 64 << 20

PLATFORM = HERE / "plc_h743.repl"


def symbols(nm, elf):
    out, _ = run_capture([nm, elf])
    return {m.group(2): int(m.group(1), 16)
            for m in re.finditer(r"^([0-9a-f]{8}) [TtWBbDd] (\S+)\r?$", out, re.M)}


def owner_record(root_pub):
    # The first claim: unsigned, generation 1, bound to this board's uid.
    rec = struct.pack("<BBHII", ord("O"), 0, OWNER_FORMAT_VER, 1, 0) + root_pub + RENODE_UID
    return rec + bytes(64) + bytes(8)


def flash_banks(boot_bin, app_bin, iap, work):
    # APPDATA points IAPTool away from the user's own key directory.
    env = dict(os.environ, APPDATA=str(work / "appdata"))
    key = work / "root.pem"
    subprocess.run([str(iap), "genkey", str(work / "root")], env=env, capture_output=True, check=True)
    root_pub = bytes.fromhex(subprocess.run([str(iap), "pubkey", str(key)], env=env,
                                            capture_output=True, text=True, check=True)
                             .stdout.strip().splitlines()[-1].strip())
    subprocess.run([str(iap), "sign", str(app_bin), str(key), "--out=" + str(work / "app")],
                   env=env, capture_output=True, check=True)
    cert = subprocess.run([str(iap), "cert", "--key=" + str(key)], env=env,
                          capture_output=True, text=True, check=True).stdout.strip()
    app, sig = app_bin.read_bytes(), (work / "app.sig").read_bytes()
    rec = struct.pack("<BBHII", REC_METADATA, REC_SLOTS, 0, 0, len(app)) + sig + bytes.fromhex(cert) + bytes(20)
    assert len(rec) == REC_SLOTS * 32
    # Erased flash reads 0xFF; Renode's blank memory is 0x00, which the
    # bootloader takes for corrupt records.
    img = bytearray(b"\xff" * FLASH_SIZE)
    boot = boot_bin.read_bytes()
    img[:len(boot)] = boot
    img[APP - FLASH:APP - FLASH + len(app)] = app
    img[META - FLASH:META - FLASH + len(rec)] = rec
    owner = owner_record(root_pub)
    assert len(owner) == 160
    img[ROOT_AREA - FLASH:ROOT_AREA - FLASH + len(owner)] = owner
    marker = struct.pack("<II", 0x4C353153, 1) + bytes(24)
    img[MARKER - FLASH:MARKER - FLASH + len(marker)] = marker
    (work / "bank1.bin").write_bytes(img[:FLASH_SIZE // 2])
    (work / "bank2.bin").write_bytes(img[FLASH_SIZE // 2:])


def run_renode(renode, work, sym, sd_image):
    w = work.as_posix()
    loop = sym["loop"]
    lines = [
        'mach create "plc"',
        "machine LoadPlatformDescription @%s" % PLATFORM.as_posix(),
        # The platform fills the UID with random words; pin it to RENODE_UID.
        "sysbus WriteDoubleWord 0x1FF1E800 0",
        "sysbus WriteDoubleWord 0x1FF1E804 0",
        "sysbus WriteDoubleWord 0x1FF1E808 0",
        # LoadBinary only: LoadELF zero-fills .data/.bss at their flash load address.
        "sysbus LoadBinary @%s/bank1.bin 0x%08X" % (w, FLASH),
        "sysbus LoadBinary @%s/bank2.bin 0x%08X" % (w, FLASH + FLASH_SIZE // 2),
        "cpu VectorTableOffset 0x%08X" % FLASH,
        "uart4 CreateFileBackend @%s/uart4.txt true" % w,
        "usart3 CreateFileBackend @%s/usart3.txt true" % w,
        # The card-detect pin reads "present" in Renode; without a card the
        # SDMMC model never answers CMD0 and setup() hangs.
        "machine SdCardFromFile @%s sysbus.sdmmc 0x%X false" % (sd_image.as_posix(), SD_IMAGE_SIZE),
        'cpu AddHook 0x%08X "self.InfoLog(\'HIT setup\')"' % sym["setup"],
        # One-shot: a hook left on loop() slows examples that loop without delay() to a crawl.
        'cpu AddHook 0x%08X "self.InfoLog(\'HIT loop\'); self.RemoveHooksAt(0x%08X)"' % (loop, loop),
        'cpu AddHook 0x%08X "self.InfoLog(\'HIT fault\')"' % sym["Default_Handler"],
        'emulation RunFor "00:00:%02d"' % RUN_SECONDS,
        'echo "END PC"',
        "sysbus.cpu PC",
        'cpu AddHook 0x%08X "self.InfoLog(\'HIT endloop\'); self.RemoveHooksAt(0x%08X)"' % (loop, loop),
        'emulation RunFor "00:00:%02d"' % TAIL_SECONDS,
        "q",
    ]
    (work / "run.resc").write_text("\n".join(lines) + "\n")
    # stdin must stay open until Renode quits: a closed or inherited stdin kills
    # its console reader and every command after RunFor is silently dropped.
    p = subprocess.Popen([renode, "--disable-gui", "--console", "-e", "include @%s/run.resc" % w],
                         cwd=str(Path(renode).parent), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, errors="replace")
    out = p.stdout.read()
    p.wait()
    (work / "console.txt").write_text(out, encoding="utf-8")
    return out


def judge(out, work, app_bin):
    uart4 = (work / "uart4.txt").read_text(errors="replace") if (work / "uart4.txt").exists() else ""
    m = re.search(r"END PC\s+(0x[0-9a-fA-F]+)", out)
    pc = int(m.group(1), 16) if m else None
    app = app_bin.read_bytes()
    off = pc - APP if pc is not None else -1
    stuck = 0 <= off < len(app) - 1 and struct.unpack_from("<H", app, off)[0] == 0xE7FE  # b .
    # Renode folds repeated log lines into "(N)", so count occurrences, not lines.
    checks = [
        ("bootloader jumped into the app", "APP Mod" in uart4),
        ("setup() entered once", out.count("HIT setup") == 1),
        ("loop() entered", out.count("HIT loop") == 1),
        ("loop() still entered at the end", out.count("HIT endloop") == 1),
        ("no fault", "HIT fault" not in out),
        ("PC readable and not parked on 'b .'", pc is not None and not stuck),
    ]
    return [(name, ok) for name, ok in checks], pc


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--only", default="")
    args = ap.parse_args()

    renode = getattr(cfg, "RENODE", "")
    arduino_cli = getattr(cfg, "ARDUINO_CLI", "")
    cli_config = getattr(cfg, "ARDUINO_CLI_CONFIG", "")
    boot_bin = Path(cfg.BOOT_REPO) / "Debug" / "open_plc_cube_ide.bin"
    tools = Path(cfg.A15) / "packages" / "OpenPLC_Alpha" / "tools"
    nm = sorted(tools.glob("xpack-arm-none-eabi-gcc/*/bin/arm-none-eabi-nm" + EXE))
    iap = get_iap_tool()

    missing = [
        (not renode or not Path(renode).exists(), "Renode not found. Set $RENODE in config/machine.py"),
        (not arduino_cli or not Path(arduino_cli).exists(), "arduino-cli not found. Set $ARDUINO_CLI in config/machine.py"),
        (not cli_config or not Path(cli_config).exists(), "arduino-cli config not found at %s" % cli_config),
        (not boot_bin.exists(), "bootloader image not found at %s -- build the bootloader first" % boot_bin),
        (not nm, "arm-none-eabi-nm not found under %s" % tools),
    ]
    if any(bad for bad, _ in missing):
        for bad, why in missing:
            if bad:
                Fail(why)
        return 2
    # Debug/ holds whichever image was built last. Booting the fixture image as
    # the bootloader prints nothing and never returns, which reads as a hang.
    not_boot = looks_like_bootloader(boot_bin)
    if not_boot:
        Fail("%s: %s -- rebuild the bootloader (tools/build_image.py) first" % (boot_bin, not_boot))
        return 2

    examples_dir = Path(cfg.CORE_LIVE) / "libraries" / "OpenPLC_Ports" / "examples"
    examples = [d for d in sorted(examples_dir.iterdir())
                if (d / (d.name + ".ino")).exists() and args.only.lower() in d.name.lower()]
    if not examples:
        Fail("no examples under %s match '%s'" % (examples_dir, args.only))
        return 2

    # Unique per run: other Renode runs may be going on at the same time.
    root = Path(tempfile.mkdtemp(prefix="renode_t3-05_", dir=str(get_scratch_dir())))
    sd_image = root / "sd.img"
    with open(sd_image, "wb") as f:
        f.truncate(SD_IMAGE_SIZE)

    failed = []
    for ex in examples:
        Section(ex.name)
        work = root / ex.name
        build = work / "build"
        out, rc = run_capture([arduino_cli, "compile", "--config-file", cli_config, "--fqbn", FQBN,
                               "--build-path", build, ex])
        if rc != 0:
            Fail("FAIL - does not compile (P5 has the details)")
            failed.append(ex.name)
            continue
        elf, app_bin = build / (ex.name + ".ino.elf"), build / (ex.name + ".ino.bin")
        flash_banks(boot_bin, app_bin, iap, work)
        out = run_renode(renode, work, symbols(nm[-1], elf), sd_image)
        checks, pc = judge(out, work, app_bin)
        for name, ok in checks:
            (Ok if ok else Fail)("  %s %s" % ("ok  " if ok else "FAIL", name))
        if all(ok for _, ok in checks):
            Ok("PASS")
        else:
            Fail("FAIL - end PC %s; Renode output in %s" % ("0x%08X" % pc if pc is not None else "?", work))
            failed.append(ex.name)

    Section("result")
    print("  %d example(s), %d failed" % (len(examples), len(failed)))
    if failed:
        Fail("failed: " + ", ".join(failed))
        return 1
    shutil.rmtree(str(root), ignore_errors=True)
    Ok("every example boots and keeps looping")
    return 0


if __name__ == "__main__":
    sys.exit(main())
