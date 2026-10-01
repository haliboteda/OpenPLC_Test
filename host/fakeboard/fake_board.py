"""A stand-in for the bootloader's UDP discovery and TCP flash channel.

Just enough of the protocol to drive the real IAPTool.exe end to end without a
board. Its whole purpose is the `getpubkey` handshake: the tool has to decide,
before sending a single byte of firmware, whether the key it would sign with is
the one this board will accept. That decision has five outcomes and none of
them are reachable on a real board without physically swapping keys.

This is NOT a bootloader model. It answers commands with fixed strings and does
no verification whatsoever -- what is under test is IAPTool's behaviour, not the
device's. Device behaviour is covered by the T/N/S cases against real hardware.

Usage:  fake_board.py <pubkey-hex | "unknown" | "none"> [seconds] [--port N]
                      [--uid UID] [--app VERSION]

  pubkey-hex   64-byte P-256 public key as 128 hex chars, returned by getpubkey
  "unknown"    answer getpubkey with "Unknown command", i.e. an old bootloader
  "none"       a board with no root (factory state): getpubkey answers "none",
               takeown <pubkey> is accepted once and that key becomes the root,
               and flash is refused until then
  seconds      how long to stay up (default 25)
  --port       port to serve, default 56865 -- must match "server_port" in the
               local_config.json IAPTool reads, or the tool dials nothing
  --discovery-port
               also answer discovery on this UDP port (the board package's
               discovery tool always asks 56865); default: --port only
               placed below, at, or above what the "device" already runs.
  --uid        UID in the identity string (default: UID below)
  --app        start as a board running an app of this version (role CUSAPP,
               fifth identity field). It answers the UDP reboot challenge and,
               on an accepted reboot request, goes silent and comes back as
               BOOTLD; after the full image it goes silent again and comes back
               as CUSAPP, so IAPTool's "board came back" verdict means something.
               A reboot request is accepted only if its certificate's leaf key
               equals pubkey-hex (bytes compared, nothing verified -- so a
               delegated certificate is refused); with "unknown", any is.
"""
import socket
import sys
import threading
import time

UID = "003300343132511039333639"
NONCE = "00112233445566778899aabbccddeeff"

_argv = sys.argv[1:]


def _take_opt(name, default):
    global _argv
    if name in _argv:
        i = _argv.index(name)
        value = _argv[i + 1]
        del _argv[i:i + 2]
        return value
    return default


PORT = int(_take_opt("--port", 56865))
DISCOVERY_PORT = int(_take_opt("--discovery-port", PORT))
UID = _take_opt("--uid", UID)
APP_VERSION = _take_opt("--app", None)

PUBKEY = _argv[0] if len(_argv) > 0 else "unknown"
LIFETIME = float(_argv[1]) if len(_argv) > 1 else 25.0


# How long the board stays silent while "resetting": after an accepted reboot
# request (shorter than IAPTool's 4 s reboot wait) and after the full image.
REBOOT_SILENCE = 1.0
IMAGE_SILENCE = 3.0

_lock = threading.Lock()
_state = {"role": "CUSAPP" if APP_VERSION else "BOOTLD", "silent_until": 0.0,
          "root": PUBKEY}


def log(m):
    print("[board] " + m, flush=True)


def current_role():
    with _lock:
        return _state["role"]


def current_root():
    with _lock:
        return _state["root"]


def take_own(pub):
    """takeown: accepted only while there is no root."""
    with _lock:
        if _state["root"] != "none":
            return False
        _state["root"] = pub.lower()
        return True


def identity():
    role = current_role()
    if APP_VERSION is None:
        return "STM32H743_%s_%s_0.1.3" % (UID, role)
    # In the bootloader the app-version field is "-".
    return "STM32H743_%s_%s_0.1.3_%s" % (UID, role,
                                          APP_VERSION if role == "CUSAPP" else "-")


def go_silent(seconds, then_role):
    with _lock:
        _state["silent_until"] = time.time() + seconds
        _state["role"] = then_role
    log("SILENT for %.1f s, then %s" % (seconds, then_role))


def silent():
    with _lock:
        return time.time() < _state["silent_until"]


def handle_reboot(msg):
    """openplc_server_reboot <cert hex> <nonce sig hex>; no reply either way."""
    parts = msg.split()
    leaf = parts[1][:128].lower() if len(parts) >= 2 else ""
    if PUBKEY != "unknown" and leaf != PUBKEY.lower():
        log("REBOOT REFUSED: certificate leaf %s... is not the trusted key" % leaf[:16])
        return
    log("REBOOT ACCEPTED")
    go_silent(REBOOT_SILENCE, "BOOTLD")


def udp_server(stop, port=PORT):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("0.0.0.0", port))
    s.settimeout(0.5)
    while not stop.is_set():
        try:
            data, addr = s.recvfrom(1024)
        except socket.timeout:
            continue
        msg = data.decode(errors="replace").strip()
        if silent():
            log("UDP %r from %s ignored (resetting)" % (msg[:40], addr))
            continue
        log("UDP %r from %s" % (msg[:60], addr))
        if APP_VERSION is not None and current_role() == "CUSAPP":
            if msg == "openplc_server_reboot_challenge":
                s.sendto(NONCE.encode(), addr)
                continue
            if msg.startswith("openplc_server_reboot "):
                handle_reboot(msg)
                continue
        # The four keywords a real board answers (case T1-01). Missing any of them
        # here just makes the board look absent, which is a confusing way for a
        # key-match case to fail.
        if msg in ("openplc_server_where_r_y", "DISCOVER", "openplc_discover", "ping"):
            # name_uid_role_version[_appversion] -- the PC tool splits this on "_"
            s.sendto(identity().encode(), addr)
    s.close()


def handle_tcp(conn):
    state = "IDLE"
    expected = 0
    received = 0
    buf = b""
    while True:
        try:
            data = conn.recv(65536)
        except OSError:
            break
        if not data:
            break

        if state == "FLASH":
            received += len(data)
            conn.sendall(b"OK")
            log("data chunk %d bytes (%d/%d)" % (len(data), received, expected))
            if received >= expected:
                log("IMAGE FULLY RECEIVED %d bytes" % received)
                if APP_VERSION is not None:
                    # The connection is left open: closing it right after the
                    # last OK makes IAPTool read EOF as a failed ack.
                    go_silent(IMAGE_SILENCE, "CUSAPP")
            continue

        buf += data
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            cmd = line.decode(errors="replace").strip()
            if not cmd:
                continue
            log("CMD %r" % cmd)

            if cmd == "ping":
                conn.sendall(b"OK")
            elif cmd == "getuid":
                conn.sendall(UID.encode())
            elif cmd == "getpubkey":
                if current_root() == "unknown":
                    conn.sendall(b"Unknown command")
                else:
                    conn.sendall(current_root().encode())
            elif cmd.startswith("takeown "):
                pub = cmd.split()[1]
                if take_own(pub):
                    log("TAKEOWN ACCEPTED %s" % pub.lower())
                    conn.sendall(b"OK")
                else:
                    log("TAKEOWN REFUSED")
                    conn.sendall(b"Refused")
            elif cmd == "getowner":
                conn.sendall(b"0" if current_root() == "none" else b"1")
            elif cmd == "authchallenge":
                conn.sendall(NONCE.encode())
            elif cmd.startswith("flash") and current_root() == "none":
                log("FLASH REFUSED: no root")
                conn.sendall(b"Refused")
            elif cmd.startswith("flash"):
                expected = int(cmd.split()[1])
                state = "FLASH"
                log("accepting %d bytes" % expected)
                conn.sendall(b"OK")
            else:
                conn.sendall(b"Unknown command")
    conn.close()


def tcp_server(stop):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", PORT))
    s.listen(4)
    s.settimeout(0.5)
    while not stop.is_set():
        try:
            conn, addr = s.accept()
        except socket.timeout:
            continue
        log("TCP connect from %s" % (addr,))
        handle_tcp(conn)
    s.close()


def main():
    stop = threading.Event()
    servers = [(udp_server, (stop,)), (tcp_server, (stop,))]
    if DISCOVERY_PORT != PORT:
        servers.append((udp_server, (stop, DISCOVERY_PORT)))
    for target, args in servers:
        t = threading.Thread(target=target, args=args)
        t.daemon = True
        t.start()

    shown = PUBKEY[:16] + "..." if PUBKEY != "unknown" else "unknown"
    log("ready on %d, pubkey=%s" % (PORT, shown))
    try:
        time.sleep(LIFETIME)
    except KeyboardInterrupt:
        pass
    stop.set()


if __name__ == "__main__":
    main()
