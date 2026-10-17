"""TODO #70 -- reproduce USB CDC output stopping until the port is reopened.

    python tools/run_usb_cdc_stall.py --key Output/five-paths-keys/wipe/wipe_root.pem
    python tools/run_usb_cdc_stall.py --skip-upload
    python tools/run_usb_cdc_stall.py --watch 900     a person drives their own terminal;
                                                      print every change the board sees

Uploads onboard/usb/CDC_Stall over USB CDC, then plays the host side in a few
ways and reports, per phase, how many "seq" lines arrived on USB and what the
board says on RS232 about DTR, CDC_connected() and dropped writes. Separates
the two candidate causes in $PROD/work/TODO.md #70: the terminal not raising
DTR, and the host not reading for longer than USB_CDC_TRANSMIT_TIMEOUT.

Exit 0 = ran, 2 = setup problem. It reports; it does not judge.
"""

import argparse
import re
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import cfg, Section, Ok, Fail, get_iap_tool, get_scratch_dir, run_capture  # noqa: E402

import serial  # noqa: E402

FQBN = ("OpenPLC_Alpha:stm32:OPEN-PLC:pnum=PLC_H743,usb=CDCgen,"
        "upload_method=cdcMethod")
SKETCH = Path(__file__).resolve().parent.parent / "onboard" / "usb" / "CDC_Stall"
STAT = re.compile(r"stat seq=(\d+) dtr=(\d) conn=(\d) dropped=(\d+)")
SEQ = re.compile(rb"seq (\d+)")


class LogTail(threading.Thread):
    """Keeps the latest RS232 stat line."""

    def __init__(self, port):
        super().__init__(daemon=True)
        self.ser = serial.Serial(port, 115200, timeout=0.2)
        self.last = None
        self.stop = False

    def run(self):
        buf = b""
        while not self.stop:
            buf += self.ser.read(256)
            *lines, buf = buf.split(b"\n")
            for ln in lines:
                m = STAT.search(ln.decode(errors="replace"))
                if m:
                    self.last = tuple(int(x) for x in m.groups())


def build_and_upload(key, cdc):
    cli = getattr(cfg, "ARDUINO_CLI", "")
    build = get_scratch_dir() / "cdc_stall"
    out, rc = run_capture([cli, "compile", "--config-file", cfg.ARDUINO_CLI_CONFIG,
                           "--fqbn", FQBN, "--build-path", build, SKETCH])
    if rc:
        print(out)
        return False
    argv = [get_iap_tool(), "cdc", str(build / "CDC_Stall.ino.bin"), cdc]
    if key:
        argv.append("--key=%s" % key)
    out, rc = run_capture(argv)
    if rc:
        print(out)
    return rc == 0


def open_cdc(port, dtr, seconds=20):
    deadline = time.time() + seconds
    while True:
        try:
            s = serial.Serial()
            s.port, s.baudrate, s.timeout = port, 115200, 0.1
            # On Linux/macOS the kernel raises DTR on open before pyserial lowers it,
            # so dtr=False there still gives the board a brief DTR=1.
            s.dtr = dtr
            s.rts = False
            s.open()
            return s
        except Exception:
            if time.time() > deadline:
                raise
            time.sleep(0.3)


def read_for(s, seconds):
    """Returns (lines, first seq, last seq) read during `seconds`."""
    end = time.time() + seconds
    data = b""
    while time.time() < end:
        try:
            data += s.read(4096)
        except serial.SerialException:
            return "handle dead", None, None
    seqs = [int(x) for x in SEQ.findall(data)]
    return len(seqs), (seqs[0] if seqs else None), (seqs[-1] if seqs else None)


def phase(name, log, fn):
    before = log.last
    got = fn()
    after = log.last
    print("%-44s usb_lines=%-6s first=%-8s last=%-8s  board before=%s after=%s"
          % (name, got[0], got[1], got[2], before, after))
    return got


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cdc", default=getattr(cfg, "CDC_PORT", ""))
    ap.add_argument("--rs232", default=getattr(cfg, "RS232_PORT", ""))
    ap.add_argument("--key", default="")
    ap.add_argument("--skip-upload", action="store_true")
    ap.add_argument("--ip", default="",
                    help="also re-upload over Ethernet with the port held open")
    ap.add_argument("--watch", type=float, default=0,
                    help="only watch RS232 for this many seconds and print each "
                         "change of DTR / connected / dropping; no upload, no USB access")
    ap.add_argument("--pause", type=float, default=20.0,
                    help="seconds the host keeps the port open without reading")
    a = ap.parse_args()
    if not a.cdc or not a.rs232:
        Fail("need --cdc and --rs232")
        return 2

    if a.watch:
        return watch(a.rs232, a.watch)

    if not a.skip_upload:
        Section("Upload")
        if not build_and_upload(a.key, a.cdc):
            Fail("build or upload failed")
            return 2
        Ok("uploaded")

    log = LogTail(a.rs232)
    log.start()
    time.sleep(3)

    Section("Phases")
    s = open_cdc(a.cdc, dtr=False)
    phase("1 open without DTR, read 3 s", log, lambda: read_for(s, 3))
    s.dtr = True
    phase("2 raise DTR, read 3 s", log, lambda: read_for(s, 3))
    phase("3 stop reading %.0f s (port open, DTR up)" % a.pause, log,
          lambda: (time.sleep(a.pause), (0, None, None))[1])
    phase("4 resume reading 5 s", log, lambda: read_for(s, 5))
    phase("5 keep reading 5 s more", log, lambda: read_for(s, 5))
    s.dtr = False
    time.sleep(0.2)
    s.dtr = True
    phase("6 toggle DTR, read 5 s", log, lambda: read_for(s, 5))
    s.close()
    s = open_cdc(a.cdc, dtr=True)
    phase("7 close and reopen with DTR, read 5 s", log, lambda: read_for(s, 5))
    if a.ip:
        # The board resets after an upload; the terminal keeps its old handle.
        phase("8 port open, re-upload over Ethernet", log,
              lambda: (upload_ether(a.ip, a.key), read_for(s, 3))[1])
        phase("9 same handle, read 15 s after reset", log, lambda: read_for(s, 15))
        s.close()
        s = open_cdc(a.cdc, dtr=True)
        phase("10 close and reopen, read 5 s", log, lambda: read_for(s, 5))
    s.close()
    log.stop = True
    return 0


def watch(port, seconds):
    log = LogTail(port)
    log.start()
    end = time.time() + seconds
    prev = None
    while time.time() < end:
        time.sleep(0.5)
        cur = log.last
        if not cur or cur == prev:
            continue
        dropping = prev is not None and cur[3] > prev[3]
        state = (cur[1], cur[2], dropping)
        if prev is None or state != prev_state:
            print("%s  seq=%d dtr=%d conn=%d dropped=%d%s" % (
                time.strftime("%H:%M:%S"), cur[0], cur[1], cur[2], cur[3],
                "  <- dropping" if dropping else ""), flush=True)
            prev_state = state
        prev = cur
    log.stop = True
    return 0


def upload_ether(ip, key):
    argv = [get_iap_tool(), "ether", str(get_scratch_dir() / "cdc_stall" / "CDC_Stall.ino.bin"), ip]
    if key:
        argv.append("--key=%s" % key)
    out, rc = run_capture(argv)
    print("   IAPTool ether exit %d" % rc)


if __name__ == "__main__":
    sys.exit(main())
