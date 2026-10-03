"""YMODEM sender, for tests that put a file onto the board over a serial port.

    from ymodem import send
    ok = send(serial_port, "NAME.BIN", data)

One file per batch, 1024-byte packets, CRC-16. Bytes other than the
receiver's answers are skipped: the board prints its own lines on RS232.
Decided in $PROD/maps/core-examples-on-board/issues/EXB-03-how-does-a-file-get-from-rs232-onto-the-sd-card.md
"""

import binascii
import time

SOH, STX, EOT, ACK, NAK, CAN = 0x01, 0x02, 0x04, 0x06, 0x15, 0x18
C = ord("C")
TRIES = 10


def _answer(sp, want, timeout):
    """The first byte in want, or None on timeout or CAN."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        b = sp.read(1)
        if not b:
            continue
        if b[0] == CAN:
            return None
        if b[0] in want:
            return b[0]
    return None


def _packet(seq, data):
    size = 1024 if len(data) > 128 else 128
    body = data.ljust(size, b"\x00" if seq == 0 else b"\x1a")
    crc = binascii.crc_hqx(body, 0)
    return (bytes([STX if size == 1024 else SOH, seq & 0xFF, ~seq & 0xFF]) + body
            + bytes([crc >> 8, crc & 0xFF]))


def _send_acked(sp, pkt):
    for _ in range(TRIES):
        sp.write(pkt)
        if _answer(sp, (ACK, NAK), 5.0) == ACK:
            return True
    return False


def send(sp, name, data, start_timeout=10.0):
    """Sends one file. True when the receiver acknowledged the whole batch."""
    if _answer(sp, (C,), start_timeout) is None:
        return False
    header = name.encode() + b"\x00" + ("%d" % len(data)).encode() + b"\x00"
    if not _send_acked(sp, _packet(0, header)):
        return False
    if _answer(sp, (C,), 5.0) is None:
        return False
    for seq, off in enumerate(range(0, len(data), 1024), start=1):
        if not _send_acked(sp, _packet(seq, data[off:off + 1024])):
            return False
    for _ in range(TRIES):
        sp.write(bytes([EOT]))
        if _answer(sp, (ACK, NAK), 5.0) == ACK:
            break
    else:
        return False
    if _answer(sp, (C,), 5.0) is None:
        return False
    return _send_acked(sp, _packet(0, b""))
