"""Checks $BOOT's golden_vectors.h against what the shipping IAPTool produces today.

Contract test (decision 78): the header is consumed by the bootloader's T1-16
host harness in $BOOT and produced by IAPTool from $TOOL. This script never
writes $BOOT (decision 76): it regenerates into a temporary directory and
compares. Keys and ECDSA signatures are fresh random values on every run, so
the comparison is the header's SHAPE -- every array's name and length and the
fixed inputs -- which is what a wire-format change (certificate layout,
signature size) alters. A change that keeps every length is not caught.

    python tools/check_golden_vectors.py              compare, exit 1 on drift
    python tools/check_golden_vectors.py --out FILE   write a fresh header to FILE
                                                      (a person copies it into $BOOT)

Original description, still true of the vectors themselves:

Every certificate and signature the C tests check is produced here by the
real shipping tool (IAPTool cert / signraw / genkey) rather than by a second
implementation written for the test. A passing H2 therefore proves the
bootloader's C code and the PC tool agree on the wire format -- not just that
each is internally consistent.

    python gen_vectors.py [--iaptool <path>]

The output is committed. Run this again only when the wire format changes
(certificate layout, what the root signature covers, the nonce construction);
never hand-edit golden_vectors.h.

The keys are thrown away with the temporary directory: nothing here is meant
to be reused, and a key that outlives the run is a key someone can mistake for
a real one.
"""

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from common import cfg, get_go_bin  # noqa: E402

# Fixed inputs the C side reproduces exactly. The UID/tick/counter triple is
# what makes the nonce predictable: iap_auth_issue_challenge() builds it as
# counter(4,LE) || UIDW0(4,LE) || tick(4,LE) || 0(4).
UID0, UID1, UID2 = 0x01234567, 0x89ABCDEF, 0xDEADBEEF
TICK = 5000
COUNTER = 1
AUTH_MSG = b"flash 1024 deadbeef abcd1234"
IMAGE_BLOB = b"golden image bytes for the H2 harness"

HEX_LINE = re.compile(r"^[0-9a-f]+$")


def run_tool(iaptool, *args):
    """Runs IAPTool and returns the one hex line it printed.

    IAPTool writes progress through its logger, so the payload is picked out
    by shape rather than by position -- a new log line must not silently
    become the returned value.
    """
    proc = subprocess.run([str(iaptool), *args], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise SystemExit("IAPTool %s failed:\n%s%s" % (" ".join(args), proc.stdout, proc.stderr))
    hexes = [ln.strip() for ln in proc.stdout.splitlines()
             if HEX_LINE.match(ln.strip()) and len(ln.strip()) >= 64]
    if len(hexes) != 1:
        raise SystemExit("IAPTool %s printed %d hex lines, expected 1:\n%s"
                         % (" ".join(args), len(hexes), proc.stdout))
    return hexes[0]


def genkey(iaptool, path):
    """Creates a key and returns its public half as 128 hex characters.

    genkey prints "Public key: <128 hex>" (since decision 72; before that it
    printed C initialiser lines, still accepted). It takes a name and appends
    ".pem" itself, so the suffix is stripped off here.
    """
    proc = subprocess.run([str(iaptool), "genkey", str(path.with_suffix(""))], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise SystemExit("IAPTool genkey failed:\n%s%s" % (proc.stdout, proc.stderr))
    m = re.search(r"\b([0-9a-fA-F]{128})\b", proc.stdout + proc.stderr)
    if m:
        return m.group(1).lower()
    byte_vals = re.findall(r"0x([0-9a-fA-F]{2})", proc.stdout)
    if len(byte_vals) != 64:
        raise SystemExit("genkey printed no 64-byte public key:\n%s%s" % (proc.stdout, proc.stderr))
    return "".join(b.lower() for b in byte_vals)


def nonce_bytes():
    out = bytearray()
    for v in (COUNTER, UID0, TICK, 0):
        out += v.to_bytes(4, "little")
    return bytes(out)


def c_array(name, data):
    lines = ["static const uint8_t %s[%d] = {" % (name, len(data))]
    for i in range(0, len(data), 12):
        chunk = ", ".join("0x%02x" % b for b in data[i:i + 12])
        lines.append("\t%s," % chunk)
    lines.append("};")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--iaptool", default=str(get_go_bin("IAPTool")))
    ap.add_argument("--out", help="write the fresh header here instead of comparing")
    args = ap.parse_args()

    iaptool = Path(args.iaptool)
    if not iaptool.exists():
        raise SystemExit("IAPTool not found at %s -- build it first (go build -o Output/ .)" % iaptool)

    with tempfile.TemporaryDirectory(prefix="h2-vectors-") as tmp:
        tmp = Path(tmp)
        root_key, leaf_key, foreign_key = tmp / "root.pem", tmp / "leaf.pem", tmp / "foreign.pem"

        root_pub = genkey(iaptool, root_key)
        leaf_pub = genkey(iaptool, leaf_key)
        foreign_pub = genkey(iaptool, foreign_key)

        cert_self = run_tool(iaptool, "cert", "--key=%s" % root_key)
        cert_delegated = run_tool(iaptool, "cert", leaf_pub, "--key=%s" % root_key)
        cert_foreign = run_tool(iaptool, "cert", "--key=%s" % foreign_key)

        blob_hex = IMAGE_BLOB.hex()
        image_sig_leaf = run_tool(iaptool, "signraw", blob_hex, str(leaf_key))
        image_sig_root = run_tool(iaptool, "signraw", blob_hex, str(root_key))
        image_sig_foreign = run_tool(iaptool, "signraw", blob_hex, str(foreign_key))

        auth_hex = (nonce_bytes() + AUTH_MSG).hex()
        auth_sig_leaf = run_tool(iaptool, "signraw", auth_hex, str(leaf_key))
        auth_sig_foreign = run_tool(iaptool, "signraw", auth_hex, str(foreign_key))

    body = [
        "/*",
        " * Golden vectors for the H2 host harness -- GENERATED, do not hand-edit.",
        " * Regenerate with: python gen_vectors.py",
        " *",
        " * Produced by the shipping IAPTool (cert / signraw / genkey), so a passing",
        " * test means the bootloader's C code and the PC tool agree on the wire",
        " * format. The private keys were temporary and no longer exist.",
        " */",
        "",
        "#ifndef H2_GOLDEN_VECTORS_H_",
        "#define H2_GOLDEN_VECTORS_H_",
        "",
        "#include <stdint.h>",
        "",
        "/* The device this harness pretends to be. */",
        "#define GOLDEN_UID0 0x%08XU" % UID0,
        "#define GOLDEN_UID1 0x%08XU" % UID1,
        "#define GOLDEN_UID2 0x%08XU" % UID2,
        "#define GOLDEN_TICK %uU" % TICK,
        "",
        '#define GOLDEN_AUTH_MSG "%s"' % AUTH_MSG.decode(),
        "",
        c_array("golden_root_pub", bytes.fromhex(root_pub)),
        "",
        c_array("golden_leaf_pub", bytes.fromhex(leaf_pub)),
        "",
        c_array("golden_foreign_pub", bytes.fromhex(foreign_pub)),
        "",
        "/* Simple mode: the root certifies its own key. */",
        c_array("golden_cert_self", bytes.fromhex(cert_self)),
        "",
        "/* The root certifies a separate leaf key. */",
        c_array("golden_cert_delegated", bytes.fromhex(cert_delegated)),
        "",
        "/* Structurally perfect, signed by a root this board does not trust. */",
        c_array("golden_cert_foreign", bytes.fromhex(cert_foreign)),
        "",
        c_array("golden_image_blob", IMAGE_BLOB),
        "",
        c_array("golden_image_sig_leaf", bytes.fromhex(image_sig_leaf)),
        "",
        c_array("golden_image_sig_root", bytes.fromhex(image_sig_root)),
        "",
        c_array("golden_image_sig_foreign", bytes.fromhex(image_sig_foreign)),
        "",
        "/* ECDSA over sha256(nonce || GOLDEN_AUTH_MSG) for the nonce the stubbed",
        " * HAL makes iap_auth_issue_challenge() produce. */",
        c_array("golden_auth_sig_leaf", bytes.fromhex(auth_sig_leaf)),
        "",
        c_array("golden_auth_sig_foreign", bytes.fromhex(auth_sig_foreign)),
        "",
        "#endif /* H2_GOLDEN_VECTORS_H_ */",
        "",
    ]

    text = "\n".join(body)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print("wrote %s -- copy it over $BOOT's golden_vectors.h and commit there" % args.out)
        return 0

    committed = committed_header()
    if committed is None:
        print("FAIL  golden_vectors.h not found under %s/tests" % cfg.BOOT_REPO)
        return 1
    want, have = shape(text), shape(committed.read_text(encoding="utf-8", errors="replace"))
    if want == have:
        print("OK    %s has the shape IAPTool produces today (%d arrays, %d fixed inputs)" % (
            committed, sum(1 for v in want.values() if "bytes" in str(v)),
            sum(1 for v in want.values() if "bytes" not in str(v))))
        return 0
    print("FAIL  %s no longer matches what IAPTool produces" % committed)
    for name in sorted(set(want) | set(have)):
        if want.get(name) != have.get(name):
            print("        %-28s committed %-6s  IAPTool now %s" % (name, have.get(name), want.get(name)))
    print("  update it: python tools/check_golden_vectors.py --out golden_vectors.h,")
    print("  then copy that file over %s and commit it in $BOOT" % committed)
    return 1


def committed_header():
    """$BOOT's copy; it moved into tests/ with the host harness (decision 78)."""
    hits = sorted((Path(cfg.BOOT_REPO) / "tests").rglob("golden_vectors.h"))
    return hits[0] if hits else None


def shape(text):
    """name -> byte count for every `static const uint8_t name[] = {...};`, plus
    the fixed inputs, which must not move."""
    out = {}
    for m in re.finditer(r"(\w+)\s*\[\s*(\d*)\s*\]\s*=\s*\{([^}]*)\}", text):
        out[m.group(1)] = "%s bytes (declared %s)" % (
            len(re.findall(r"0x[0-9A-Fa-f]{2}", m.group(3))), m.group(2) or "-")
    for m in re.finditer(r"#define\s+(\w+)\s+(\S+)", text):
        if not m.group(1).endswith("_H_"):
            out[m.group(1)] = m.group(2)
    return out


if __name__ == "__main__":
    sys.exit(main())
