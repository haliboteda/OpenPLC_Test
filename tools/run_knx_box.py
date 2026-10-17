"""T3-13..T3-20 -- the KNX examples on the real board, with the KNX test box.

    python tools/run_knx_box.py                  every case, in order
    python tools/run_knx_box.py --only T3-15     one case (uploads the sketch it needs)
    python tools/run_knx_box.py --list

The PC sees the bus through a KNXnet/IP tunnel on the box's IP interface
(xknx) and the board through its USB port, where the sketches print with
Serial (decision 147); uploads go over the same port, so it is closed for each
upload. Steps only a person can do -- ETS, the BOOT0 button, 24 V on DI1,
hearing a relay, watching an LED, reading a meter -- are asked for the way
run_examples.py asks. Group address defaults: the test box's existing ones and
the board's from the KEX-06 table.

Cases and criteria: $PROD/docs/modules/M3-app-runtime.md, T3-13..T3-20.
Decided in $PROD/maps/knx-examples/issues/KEX-06-how-does-each-test-case-judge.md

Exit 0 = every case passed, 1 = one failed, 2 = setup problem.
"""

import argparse
import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import board_ip, cfg, Section, Ok, Fail, banner  # noqa: E402
import run_examples as rx  # noqa: E402

BUS_S = 5.0        # a telegram, and the reply it causes
TEMP_S = 15.0      # KNX_Temperature asks for the temperature right after start-up
SENSOR_S = 12.0    # the box's sensor writes the room temperature every 5 s
TUNNEL_S = 30.0    # the router sketch needs DHCP before it takes a tunnel
CONFIGURED = r"KNX: address (\d+\.\d+\.\d+), configured"

# Sketch, the line it prints once it runs, and the FQBN it is built with
# (the KNX role is in the sketch, OPENPLC_KNX_ROLE).
SKETCHES = {
    "KNX_BusMonitor": (r"KNX bus monitor, this board is (\d+\.\d+\.\d+)", rx.FQBN),
    "KNX_Switch": (r"KNX: (address|not configured)", rx.FQBN),
    "KNX_Inputs": (r"KNX: (address|not configured)", rx.FQBN),
    "KNX_Temperature": (r"KNX: (address|not configured)", rx.FQBN),
    "KNX_Analog": (r"KNX: (address|not configured)", rx.FQBN),
    "KNX_IPRouter": (r"KNX IP router, address", rx.FQBN),
}


class Bus:
    """One KNXnet/IP tunnel: writes and reads group values, records what arrives."""

    def __init__(self, gateway):
        from xknx import XKNX
        from xknx.io import ConnectionConfig, ConnectionType
        self.xknx = XKNX(connection_config=ConnectionConfig(
            connection_type=ConnectionType.TUNNELING, gateway_ip=gateway))
        self.seen = []

    async def start(self):
        await self.xknx.start()
        self.xknx.telegram_queue.register_telegram_received_cb(self.seen.append)

    async def stop(self):
        await self.xknx.stop()

    def mark(self):
        return len(self.seen)

    async def send(self, ga, payload):
        from xknx.telegram import GroupAddress, Telegram
        await self.xknx.telegrams.put(Telegram(destination_address=GroupAddress(ga),
                                               payload=payload))

    async def write_bit(self, ga, v):
        from xknx.dpt import DPTBinary
        from xknx.telegram.apci import GroupValueWrite
        await self.send(ga, GroupValueWrite(DPTBinary(v)))

    async def write_float(self, ga, dpt, v):
        from xknx.telegram.apci import GroupValueWrite
        await self.send(ga, GroupValueWrite(dpt.to_knx(v)))

    async def read(self, ga):
        from xknx.telegram.apci import GroupValueRead
        await self.send(ga, GroupValueRead())

    async def wait(self, since, ga, test, seconds=BUS_S, source=""):
        """First telegram after `since` to ga whose payload passes test(payload)."""
        from xknx.telegram.apci import GroupValueResponse, GroupValueWrite
        deadline = asyncio.get_running_loop().time() + seconds
        while True:
            for t in self.seen[since:]:
                if (str(t.destination_address) == ga
                        and isinstance(t.payload, (GroupValueWrite, GroupValueResponse))
                        and (not source or str(t.source_address) == source)
                        and test(t.payload.value)):
                    return t
            if asyncio.get_running_loop().time() > deadline:
                return None
            await asyncio.sleep(0.05)


def bit(v):
    return lambda p: getattr(p, "value", None) == v


def temperature(p):
    from xknx.dpt import DPTTemperature
    return DPTTemperature.from_knx(p)


def mv(p):
    from xknx.dpt import DPTVoltage
    return DPTVoltage.from_knx(p)


def ma(p):
    from xknx.dpt import DPTCurrent
    return DPTCurrent.from_knx(p)


class Board:
    """The board's USB port, read with run_examples.py's helpers."""

    def __init__(self, port):
        self.port = port
        self.sp = None
        self.buf = ""

    def open(self):
        import serial
        self.sp = rx.open_retry(serial, self.port, rx.REOPEN_S)
        self.buf = ""

    def close(self):
        if self.sp:
            self.sp.close()
            self.sp = None

    def clear(self):
        self.buf = ""

    def send(self, line):
        self.clear()
        self.sp.write(line.encode() + b"\n")

    def find(self, regex):
        return re.search(regex, self.buf)

    async def expect(self, regex, seconds):
        ports, bufs = {"log": self.sp}, {"log": self.buf}
        missing = await asyncio.to_thread(rx.wait_for, ports, bufs,
                                          [("log", regex)], seconds)
        self.sp, self.buf = ports["log"], bufs["log"]
        return not missing


def check(ok, what):
    (Ok if ok else Fail)(what)
    return bool(ok)


async def ask(question):
    ok, note = await asyncio.to_thread(rx.ask, [question])
    return check(ok, "operator: %s" % (question if ok else (note or "no")))


async def do(board, action, regex):
    """Hands-on step the board's output shows; waits up to rx.HUMAN_S."""
    board.clear()
    banner([action, "The script carries on by itself once the board shows it "
                    "(%d s at most)." % rx.HUMAN_S])
    print("\a", end="", flush=True)
    return check(await board.expect(regex, rx.HUMAN_S), action)


class Ctx:
    def __init__(self, args, bus, board):
        self.args, self.bus, self.board = args, bus, board
        self.sketch = ""
        self.address = ""     # the board's individual address, as printed


async def run_sketch(ctx, name):
    """Uploads `name` unless it is already running. False = stop the run."""
    if ctx.sketch == name:
        return True
    ready, fqbn = SKETCHES[name]
    ctx.board.close()   # IAPTool needs the port
    rc = await asyncio.to_thread(rx.build_and_upload, {"name": name, "lib": "OpenPLC_KNX"},
                                 ctx.args.cdc, ctx.args.key, fqbn)
    if rc:
        return False
    ctx.board.open()
    if not await ctx.board.expect(ready, rx.READY_S):
        Fail("no banner /%s/; got %r" % (ready, ctx.board.buf[-300:]))
        return False
    ctx.sketch = name
    m = ctx.board.find(r"this board is (\d+\.\d+\.\d+)")
    if m:
        ctx.address = m.group(1)
    return True


async def configured(ctx):
    """The ETS configuration is in place; asks for a new download if not."""
    m = ctx.board.find(CONFIGURED)
    if not m:
        if not await do(ctx.board, "In ETS, download the board again (it reports "
                                   "'not configured')", CONFIGURED):
            return False
        m = ctx.board.find(CONFIGURED)
    ctx.address = m.group(1)
    return True


async def t3_13(ctx):
    """KNX_BusMonitor prints a telegram from the bus and sends one typed on its USB port."""
    a, b, bus = ctx.args, ctx.board, ctx.bus
    if not await run_sketch(ctx, "KNX_BusMonitor"):
        return None
    b.clear()
    await bus.write_bit(a.act_switch, 1)
    ok = check(await b.expect(r"KNX RX \S+ -> %s Write 1" % re.escape(a.act_switch), BUS_S),
               "board printed the write to %s" % a.act_switch)
    ok &= await ask("Is the actuator's A1 LED on now?")
    m = bus.mark()
    b.send("w %s 0" % a.act_switch)
    ok &= check(await bus.wait(m, a.act_switch, bit(0), source=ctx.address),
                "the bus carried 'w %s 0' from %s" % (a.act_switch, ctx.address))
    ok &= check(await b.expect(r"KNX TX acknowledged", BUS_S), "board: KNX TX acknowledged")
    ok &= await ask("Is the actuator's A1 LED off now?")
    return ok


async def t3_14(ctx):
    """ETS programs the board; the box's devices keep their existing configuration (KEX-06)."""
    if not await run_sketch(ctx, "KNX_Switch"):
        return None
    ok = await do(ctx.board,
                  "In ETS: address this board, link its objects to the group addresses "
                  "as in KEX-06 and download it; leave the box's devices as they are. "
                  "Press BOOT0 once (system LED on) when ETS asks.",
                  CONFIGURED)
    if ok:
        ctx.address = ctx.board.find(CONFIGURED).group(1)
        Ok("board is %s" % ctx.address)
    return ok


async def t3_15(ctx):
    """ETS-side write to the board's relay 1; the board answers with its status."""
    a, b, bus = ctx.args, ctx.board, ctx.bus
    if not await run_sketch(ctx, "KNX_Switch") or not await configured(ctx):
        return None
    ok = True
    for v in (1, 0):
        m = bus.mark()
        b.clear()
        await bus.write_bit(a.plc_switch, v)
        ok &= check(await b.expect(r"KNX RX relay 1 switch = %d" % v, BUS_S),
                    "board: KNX RX relay 1 switch = %d" % v)
        ok &= check(await bus.wait(m, a.plc_status, bit(v), source=ctx.address),
                    "board reported relay 1 status %d on %s" % (v, a.plc_status))
        if v:
            ok &= await ask("Did RY1 click on just now?")
    return ok


async def t3_16(ctx):
    """The board's DI1 switches the box's actuator."""
    a, bus = ctx.args, ctx.bus
    if not await run_sketch(ctx, "KNX_Inputs") or not await configured(ctx):
        return None
    ok = True
    for v, action in ((1, "Apply 24 V to DI1"), (0, "Remove 24 V from DI1")):
        m = bus.mark()
        ok &= await do(ctx.board, action, r"KNX TX DI 1 = %d" % v)
        ok &= check(await bus.wait(m, a.act_switch, bit(v), source=ctx.address),
                    "board sent %d to %s" % (v, a.act_switch))
        ok &= await ask("Is the actuator's A1 LED %s now?" % ("on" if v else "off"))
    return ok


async def t3_17(ctx):
    """The sensor's temperature reaches the board and matches what the sensor writes on the bus."""
    a, b, bus = ctx.args, ctx.board, ctx.bus
    if not await run_sketch(ctx, "KNX_Temperature") or not await configured(ctx):
        return None
    rxp = r"KNX RX temperature = (-?[\d.]+)"
    m = bus.mark()
    b.clear()
    if not check(await b.expect(rxp, TEMP_S), "board printed a temperature"):
        return False
    on_board = float(re.findall(rxp, b.buf)[-1])
    # No read request: the sensor writes on its own (KEX-11).
    t = await bus.wait(m, a.temp, lambda p: True, SENSOR_S)
    if not check(t, "sensor wrote %s" % a.temp):
        return False
    on_bus = temperature(t.payload.value)
    return check(abs(on_board - on_bus) <= 0.5,
                 "board %.1f C, bus %.1f C, within 0.5" % (on_board, on_bus))


async def t3_18(ctx):
    """With the threshold set 1 C under / over the room temperature, the actuator and DO2 go on / off."""
    a, b, bus = ctx.args, ctx.board, ctx.bus
    if not await run_sketch(ctx, "KNX_Temperature") or not await configured(ctx):
        return None
    # The room temperature the sensor writes is the input; nothing is written on the bus (KEX-11).
    rxp = r"KNX RX temperature = (-?[\d.]+)"
    b.clear()
    if not check(await b.expect(rxp, TEMP_S), "board printed a temperature"):
        return False
    room = float(re.findall(rxp, b.buf)[-1])
    # Start from off whatever the room is: this may or may not send 0.
    b.send("t %.1f" % (room + 1.0))
    await asyncio.sleep(1.0)
    ok = True
    for th, v in ((room - 1.0, 1), (room + 1.0, 0)):
        m = bus.mark()
        b.send("t %.1f" % th)
        ok &= check(await b.expect(r"threshold = %s" % re.escape("%.1f" % th), BUS_S),
                    "board: threshold = %.1f" % th)
        ok &= check(await b.expect(r"KNX TX switch = %d" % v, BUS_S),
                    "room %.1f C, threshold %.1f C -> board: KNX TX switch = %d" % (room, th, v))
        ok &= check(await b.expect(r"DO2 = %d" % v, BUS_S), "board: DO2 = %d" % v)
        ok &= check(await bus.wait(m, a.act_switch, bit(v), source=ctx.address),
                    "board sent %d to %s" % (v, a.act_switch))
        ok &= await ask("Is the actuator's A1 LED %s now?" % ("on" if v else "off"))
        ok &= await ask("Is DO2 at about %s V on the meter?" % ("24" if v else "0"))
    b.send("t 27")
    return ok


async def t3_20(ctx):
    """AO1 follows a setpoint from the bus; AI1 / AI2 answer reads with what the board sent."""
    from xknx.dpt import DPTCurrent
    a, b, bus = ctx.args, ctx.board, ctx.bus
    if not await run_sketch(ctx, "KNX_Analog") or not await configured(ctx):
        return None
    ok = True
    for ga, rxp, decode, tol, unit in ((a.ai1, r"KNX TX AI1 = (-?[\d.]+) mV", mv, 10.0, "mV"),
                                       (a.ai2, r"KNX TX AI2 = (-?[\d.]+) mA", ma, 0.02, "mA")):
        if not check(await b.expect(rxp, BUS_S), "board sent %s" % ga):
            ok = False
            continue
        await asyncio.sleep(0.5)  # a value sent just now would be printed by then
        on_board = float(re.findall(rxp, b.buf)[-1])
        m = bus.mark()
        await bus.read(ga)
        t = await bus.wait(m, ga, lambda p: True, source=ctx.address)
        if not check(t, "board answered a read of %s" % ga):
            ok = False
            continue
        on_bus = decode(t.payload.value)
        ok &= check(abs(on_board - on_bus) <= tol, "board printed %.2f %s, bus %.2f %s, within %g"
                    % (on_board, unit, on_bus, unit, tol))
    b.clear()
    await bus.write_float(a.ao1, DPTCurrent, 10.0)
    ok &= check(await b.expect(r"KNX RX AO1 = 10\.00 mA", BUS_S), "board: KNX RX AO1 = 10.00 mA")
    ok &= await ask("Is the voltage across AO1's 470 ohm load about 4.7 V (10 mA)?")
    await bus.write_float(a.ao1, DPTCurrent, 0.0)
    ok &= check(await b.expect(r"KNX RX AO1 = 0\.00 mA", BUS_S), "board: KNX RX AO1 = 0.00 mA")
    return ok


async def t3_19(ctx):
    """A tunnel to the board, as IP router, reaches the TP bus."""
    a, bus = ctx.args, ctx.bus
    a.board_ip = a.board_ip or board_ip()
    if not a.board_ip:
        Fail("need --board-ip (or BOARD_UID in config/machine.py)")
        return None
    if not await run_sketch(ctx, "KNX_IPRouter"):
        return None
    router = Bus(a.board_ip)
    try:
        await asyncio.wait_for(router.start(), TUNNEL_S)
    except Exception as e:
        Fail("no tunnel to %s: %r" % (a.board_ip, e))
        return False
    ok = True
    try:
        for v in (1, 0):
            m = bus.mark()
            await router.write_bit(a.act_switch, v)
            ok &= check(await bus.wait(m, a.act_switch, bit(v)),
                        "write %d through %s reached the TP bus" % (v, a.board_ip))
            ok &= await ask("Is the actuator's A1 LED %s now?" % ("on" if v else "off"))
    finally:
        await router.stop()
    return ok


CASES = [
    ("T3-13", t3_13, "KNX_BusMonitor: prints a telegram from the bus, sends one typed on its USB port"),
    ("T3-14", t3_14, "ETS programs the board; the box's devices keep their configuration"),
    ("T3-15", t3_15, "KNX_Switch: a write to the board's relay 1 switches it, status comes back"),
    ("T3-16", t3_16, "KNX_Inputs: the board's DI1 switches the box's actuator"),
    ("T3-17", t3_17, "KNX_Temperature: the sensor's temperature reaches the board"),
    ("T3-18", t3_18, "KNX_Temperature: above the threshold the board switches the actuator and DO2"),
    ("T3-20", t3_20, "KNX_Analog: AO1 follows a setpoint from the bus, AI1 / AI2 answer reads"),
    ("T3-19", t3_19, "KNX_IPRouter: a tunnel to the board reaches the TP bus"),
]

SETUP = {
    "T3-13": "the board's KNX terminals on the box's bus, the bus powered",
    "T3-14": "ETS open on this network, product OpenPLC_TP.knxprod imported",
    "T3-16": "a 24 V lead to DI1",
    "T3-20": "a voltmeter across the 470 ohm load on AO1",
    "T3-19": "the board's Ethernet on the box's network (DHCP)",
}


async def run(args, chosen):
    from knx_gateway_probe import search
    gateway = args.gateway or search()
    if not gateway:
        Fail("no KNXnet/IP interface answered the search; give --gateway")
        return 2
    bus = Bus(gateway)
    try:
        await bus.start()
    except Exception as e:
        Fail("no tunnel to %s: %r" % (gateway, e))
        return 2
    Ok("tunnel to %s as %s" % (gateway, bus.xknx.current_address))
    board = Board(args.cdc)
    board.open()
    ctx = Ctx(args, bus, board)
    failed = []
    try:
        for cid, fn, what in chosen:
            Section("%s  %s" % (cid, what))
            ok = await fn(ctx)
            if ok is None:
                return 2
            (Ok if ok else Fail)("%s %s" % (cid, "PASS" if ok else "FAIL"))
            if not ok:
                failed.append(cid)
    finally:
        board.close()
        await bus.stop()
    Section("result")
    for cid in failed:
        Fail(cid)
    if failed:
        return 1
    Ok("all %d passed" % len(chosen))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--list", action="store_true", help="print the cases and stop")
    ap.add_argument("--only", default="", help="one case id, e.g. T3-15")
    ap.add_argument("--cdc", default=getattr(cfg, "CDC_PORT", ""),
                    help="the board's USB serial port: uploads, and what the sketches print")
    ap.add_argument("--board-ip", default="",
                    help="the board's address, for T3-19; default: found by BOARD_UID")
    ap.add_argument("--gateway", default="",
                    help="the box's KNXnet/IP interface; found by search if not given")
    ap.add_argument("--key", default="", help="owner private key, for a claimed board")
    ap.add_argument("--act-switch", default="1/0/1", help="actuator switch group address")
    ap.add_argument("--temp", default="7/5/4",
                    help="group address the box's sensor writes the temperature to, watched in T3-17")
    ap.add_argument("--plc-switch", default="3/0/1", help="board relay 1 switch group address")
    ap.add_argument("--plc-status", default="3/0/2", help="board relay 1 status group address")
    ap.add_argument("--ai1", default="4/0/1", help="board AI1 (mV) group address")
    ap.add_argument("--ai2", default="4/0/2", help="board AI2 (mA) group address")
    ap.add_argument("--ao1", default="4/0/3", help="board AO1 setpoint (mA) group address")
    args = ap.parse_args()

    if args.list:
        for cid, _, what in CASES:
            print("%-6s %s" % (cid, what))
        return 0
    chosen = [c for c in CASES if not args.only or c[0] == args.only]
    if not chosen:
        Fail("no case %s; see --list" % args.only)
        return 2
    try:
        import serial  # noqa: F401
        import xknx  # noqa: F401
    except ImportError as e:
        Fail("%s: python -m pip install pyserial xknx" % e)
        return 2
    if not args.cdc:
        Fail("need --cdc (or CDC_PORT in config/machine.py)")
        return 2

    setup = [SETUP[c[0]] for c in chosen if c[0] in SETUP]
    if setup:
        ok, _ = rx.ask(["Before starting, have ready:"] + ["- " + s for s in setup])
        if not ok:
            Fail("not ready; nothing run")
            return 2
    return asyncio.run(run(args, chosen))


if __name__ == "__main__":
    sys.exit(main())
