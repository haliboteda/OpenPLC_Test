"""Checks the cross-repo mirrored code listed in
$PROD/docs/repo/ARCHITECTURE.md ("跨仓镜像的代码").

Those copies cannot be enforced by any build system -- separate repos, no
shared build -- so a one-sided edit diverges silently and only shows up at
runtime as some unrelated-looking symptom. Most items compare a semantic anchor
rather than whole files, because the files legitimately differ (extern "C" in
the C++ core, different surrounding APIs) and only the anchors must agree.

Two items are stricter, because there the files are NOT allowed to differ at
all: sha256.c and iap_cert.c are compared byte for byte, and the three
functions iap_auth.c shares with its Arduino-side subset are compared as
normalised bodies. See $PROD/docs/tables/DECISIONS.md decision 65.

Exit code 0 = every anchor agrees, 1 = at least one diverged, 2 = a file the
check needs is missing.

Anchors that are NOT checked here are listed at the bottom of the output, so
"all green" never reads as "everything is covered".

"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import cfg, Section, Ok, Warn, Fail, read_text  # noqa: E402

failed = 0
skipped = 0


def get_anchor(path, pattern, all_matches=False):
    """Pull capture-group-1 out of a file. Missing file or zero matches both
    return None so the caller can tell them apart from ""."""
    if not Path(path).exists():
        return None
    matches = list(re.finditer(pattern, read_text(path)))
    if not matches:
        return None
    if all_matches:
        return "|".join(m.group(1).strip() for m in matches)
    return matches[0].group(1).strip()


def compare_anchor(name, sides):
    """One row of the report. `sides` maps label -> extracted value; they must
    all be equal and non-None."""
    global failed, skipped

    missing = [k for k, v in sides.items() if v is None]
    if missing:
        Warn("SKIP  %s" % name)
        for k in missing:
            Warn("        not found in: %s" % k)
        skipped += 1
        return

    # Select-Object -Unique is case-insensitive.
    values = list(dict.fromkeys(v.lower() for v in sides.values()))
    if len(values) == 1:
        Ok("OK    %s" % name)
        return

    Fail("DIFF  %s" % name)

    # Anchors built from many ";"-joined parts (the FMC pin map is 39 of them)
    # are longer than any sensible line, and truncating them printed two
    # identical-looking lines that differed somewhere past the cut. Report the
    # parts that actually differ instead.
    parts = [v.split(';') for v in sides.values()]
    is_multi_part = sum(1 for p in parts if len(p) > 1) == len(sides)
    common = None
    if is_multi_part:
        common = parts[0]
        for p in parts:
            common = [c for c in common if c in p]

    for k, v in sides.items():
        if is_multi_part:
            only = [x for x in v.split(';') if x not in common]
            Fail("        %-46s only here: %s" % (k, " ".join(only)))
        else:
            if len(v) > 120:
                v = v[:117] + "..."
            Fail("        %-46s %s" % (k, v))
    if is_multi_part:
        print("        (%d part(s) agree and are not shown)" % len(common))
    failed += 1


def get_function_body(path, name):
    """A C function body with comments and whitespace normalised away.

    Used where the two files may not be compared whole -- one side carries
    extra functions -- but the shared ones have to stay the same logic. Returns
    None if the function is absent, which compare_anchor reports as a SKIP
    rather than a pass.
    """
    if not Path(path).exists():
        return None
    text = read_text(path).replace("\r\n", "\n")
    head = re.search(r"^(?:static\s+)?[A-Za-z_][\w \t*]*\b%s\s*\([^;{]*?\)\s*\n?\{"
                     % re.escape(name), text, re.M | re.S)
    if not head:
        return None
    start = text.index("{", head.start())
    depth = 0
    body = None
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                body = text[start:i + 1]
                break
    if body is None:
        return None
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    body = re.sub(r"//[^\n]*", "", body)
    return re.sub(r"\s+", " ", body).strip()


def get_signed_bytes_recipe(path):
    """The exact bytes a challenge signature covers, and in what order.

    This is the wire contract between the two sides: they build the digest
    identically or no reboot request ever verifies. The rest of
    iap_auth_verify_and_consume legitimately differs -- the bootloader reaches
    the owner root through owner_slot.c and prints diagnostics, the application
    does neither -- so comparing whole bodies here reports drift that is meant
    to be there.
    """
    if not Path(path).exists():
        return None
    text = read_text(path).replace("\r\n", "\n")
    m = re.search(r"(memcpy\(buf, s_nonce.*?sha256\([^;]*?\);)", text, re.S)
    if not m:
        return None
    return re.sub(r"\s+", " ", m.group(1)).strip()


def compare_bytes(name, left, right):
    """Two files that must be identical to the byte. Reported like an anchor."""
    global failed, skipped

    missing = [str(q) for q in (left, right) if not Path(q).exists()]
    if missing:
        Warn("SKIP  %s" % name)
        for m in missing:
            Warn("        missing: %s" % m)
        skipped += 1
        return
    a = Path(left).read_bytes().replace(b"\r\n", b"\n")
    b = Path(right).read_bytes().replace(b"\r\n", b"\n")
    if a == b:
        Ok("OK    %s  (%d bytes, identical)" % (name, len(a)))
        return
    Fail("DIFF  %s" % name)
    Fail("        %s  (%d bytes)" % (left, len(a)))
    Fail("        %s  (%d bytes)" % (right, len(b)))
    Fail("        these two carry no repo-specific content -- sync them, do not")
    Fail("        adjust this check. Source is the bootloader (ARCHITECTURE.md rule 1).")
    failed += 1


BOOT = Path(cfg.BOOT_REPO)
LIVE = Path(cfg.CORE_LIVE)
TOOL = Path(cfg.IAPTOOL_REPO)
# The Python copies of the owner record (run_setowner, inject_owner_record,
# renode) live in this repo.
TESTTOOL_DIR = Path(cfg.TEST_REPO)
# Only for the three-way calibration contract; empty skips that one side.
PORTTOOL = Path(getattr(cfg, "PORTTOOL_REPO", "") or ".")

boot_udp = BOOT / "IAPServer/udp_server.c"
core_udp = LIVE / "libraries/OpenPLC_IAP/src/udp_server.c"
boot_eth = BOOT / "LWIP/Target/ethernetif.c"
core_eth = LIVE / "libraries/OpenPLC_Net/src/ethernetif.c"
boot_srv = BOOT / "IAPServer/IAP_server.c"
boot_hand = BOOT / "IAPServer/IAP_boot_handoff.h"
core_hand = LIVE / "cores/arduino/stm32/IAP_boot_handoff.h"
tool_lock = TOOL / "uploadlock.go"
core_disc = LIVE / "tools/discovery/network_discovery.go"
boot_fmc = BOOT / "Core/Src/fmc.c"
core_variant = LIVE / "variants/STM32H7xx/H743/variant_PLC_H743.h"
boot_cert = BOOT / "IAPServer/iap_cert.h"
core_cert = LIVE / "libraries/OpenPLC_IAP/src/iap_cert.h"
tool_cert = TOOL / "iapcert/iapcert.go"
boot_owner = BOOT / "IAPServer/owner_slot.h"
core_owner = LIVE / "libraries/OpenPLC_IAP/src/owner_root_ro.c"
tool_owner = TOOL / "owner.go"
core_iface_win = LIVE / "tools/discovery/iface_windows.go"
core_iface_lin = LIVE / "tools/discovery/iface_linux.go"
core_iface_mac = LIVE / "tools/discovery/iface_darwin.go"
tool_netiface = TOOL / "netiface/netiface.go"
tool_iface_win = TOOL / "netiface/iface_windows.go"
tool_iface_lin = TOOL / "netiface/iface_linux.go"
tool_iface_mac = TOOL / "netiface/iface_darwin.go"

# Whole function bodies, because these two are meant to be byte-identical
# apart from the exported name. (?s) so . spans the body.
BODY_IS_PHYSICAL = r"(?s)func %s\(iface net\.Interface\) bool \{(.*?)\n\}"
BODY_CLASSIFY = (
    r"(?s)func classifyHardware\(ifaces \[\]net\.Interface\) "
    r"map\[string\]bool \{(.*?)\n\}"
)

Section("cross-repo mirrors")

# --- discovery rate limiter -------------------------------------------------
# Divergence here means one image throttles and the other does not, which is
# exactly the bug that produced the 2026-08-15 "board disappears" reports.
compare_anchor("discovery reply cap (replies/sec)", {
    "bootloader IAPServer/udp_server.c":
        get_anchor(boot_udp, r'#define\s+DISCOVERY_MAX_REPLIES_PER_SEC\s+(\d+)'),
    "core OpenPLC_IAP/src/udp_server.c":
        get_anchor(core_udp, r'#define\s+DISCOVERY_MAX_REPLIES_PER_SEC\s+(\d+)'),
})

compare_anchor("discovery rate-limit window (ms)", {
    "bootloader IAPServer/udp_server.c": get_anchor(boot_udp, r'window_start\)\s*>=\s*(\d+)'),
    "core OpenPLC_IAP/src/udp_server.c": get_anchor(core_udp, r'window_start\)\s*>=\s*(\d+)'),
})


# --- MAC derivation ---------------------------------------------------------
# Two boards on one LAN collide if this diverges, and it only shows up in the
# field. The array is named MACAddr on one side and mac on the other, so the
# byte assignments are normalised before comparing.
#
# Both files also contain byte assignments that are NOT the derivation -- the
# CubeMX-generated constant MAC in the bootloader, the MAC_ADDR0..5 override
# branch in the core -- so only assignments whose right-hand side comes from the
# UID are collected. Anything else is a different code path.
def get_mac_derivation(path):
    if not Path(path).exists():
        return None
    text = read_text(path)
    text = re.sub(r'MACAddr', 'M', text, flags=re.I)    # -replace: case-insensitive
    text = re.sub(r'\bmac\b', 'M', text, flags=re.I)
    hash_m = re.search(r'h\s*=\s*(u0[^;]+);', text)     # [regex]::Match: case-sensitive
    if not hash_m:
        return None
    parts = [hash_m.group(1)]
    for b in re.finditer(r'M\[(\d)\]\s*=\s*([^;]+);', text):
        rhs = b.group(2)
        if not re.search(r'\bh\b|\bu0\b|0x02U', rhs, re.I):     # -notmatch: case-insensitive
            continue
        parts.append("%s=%s" % (b.group(1), rhs))
    if len(parts) != 7:
        return None
    return re.sub(r'\s+', '', ";".join(parts))


compare_anchor("MAC derived from UID", {
    "bootloader LWIP/Target/ethernetif.c": get_mac_derivation(boot_eth),
    "core OpenPLC_Net/src/ethernetif.c": get_mac_derivation(core_eth),
})

# --- identity string --------------------------------------------------------
# The PC tool splits the reply on "_"; a format change on one side alone makes
# that side's boards unparseable.
compare_anchor("identity string format", {
    "bootloader IAPServer/IAP_server.c": get_anchor(boot_srv, r'snprintf\([^;]*?"(%s_[^"]*)"'),
    "core OpenPLC_IAP/src/udp_server.c": get_anchor(core_udp, r'snprintf\([^;]*?"(%s_[^"]*)"'),
})


# --- SRAM4 handoff record ---------------------------------------------------
# A layout or magic mismatch means the app's "stay in the bootloader" request is
# read as garbage. The record fails towards staying in the bootloader, so the
# symptom is a board that will not boot its app rather than one that ignores the
# request -- still worth catching before it ships.
def get_handoff_layout(path):
    if not Path(path).exists():
        return None
    text = read_text(path)
    parts = []
    # RSR_* is the reset cause the bootloader publishes for the app (decision 80):
    # a mismatch makes every sketch read "unknown".
    for k in ('BOOT_HANDOFF_ADDR', 'BOOT_HANDOFF_SIZE',
              'BOOT_HANDOFF_MAGIC', 'BOOT_HANDOFF_VERSION',
              'BOOT_HANDOFF_RSR_OFFSET', 'BOOT_HANDOFF_RSR_MAGIC'):
        m = re.search(r'#define\s+%s\s+(\S+)' % k, text)
        if not m:
            return None
        parts.append("%s=%s" % (k, m.group(1)))
    for m in re.finditer(r'BOOT_REQ_(\w+)\s*=\s*(\d+)', text):
        parts.append("%s=%s" % (m.group(1), m.group(2)))
    st = re.search(r'typedef struct \{(.*?)\} boot_handoff_t;', text, re.S)
    if not st:
        return None
    for m in re.finditer(r'(uint\d+_t)\s+(\w+)\s*;', st.group(1)):
        parts.append("%s %s" % (m.group(1), m.group(2)))
    return ";".join(parts)


compare_anchor("SRAM4 boot_handoff_t layout", {
    "bootloader IAPServer/IAP_boot_handoff.h": get_handoff_layout(boot_hand),
    "core cores/arduino/stm32/IAP_boot_handoff.h": get_handoff_layout(core_hand),
})


# --- FMC pin map ------------------------------------------------------------
# The variant header names the 39 SDRAM pins (FMC_RESERVED_*) so a user can see
# what not to drive; fmc.c is where they are actually configured. That makes the
# header a second copy, and a one-sided change makes it lie -- it would still
# claim PE7 is a data line after PE7 stopped being one, which is worse than not
# listing the pins at all, because E6's whole value is that the list is true.
#
# The variant's own FMC_RESERVED_PIN_COUNT assertion cannot catch this: it only
# proves the header is self-consistent, and it stays self-consistent while fmc.c
# moves underneath it.
#
# Normalised to a sorted set of FUNC=PIN on both sides:
#   fmc.c    "  PE7   ------> FMC_D4"        -> D4=PE7
#   variant  "#define FMC_RESERVED_D4  PE7"  -> D4=PE7
def get_fmc_pin_map(path, pattern, pin_group, func_group):
    if not Path(path).exists():
        return None
    pairs = []
    for m in re.finditer(pattern, read_text(path)):
        pairs.append("%s=%s" % (m.group(func_group), m.group(pin_group)))
    if not pairs:
        return None
    # fmc.c carries the same block twice (MspInit and MspDeInit); dedupe rather
    # than compare a doubled list against a single one.
    return ";".join(sorted(set(pairs)))


compare_anchor("FMC pin map (39 SDRAM pins)", {
    "bootloader Core/Src/fmc.c":
        get_fmc_pin_map(boot_fmc, r'(P[A-I]\d+)\s*-+>\s*FMC_(\w+)', 1, 2),
    "core variants/.../variant_PLC_H743.h":
        get_fmc_pin_map(core_variant, r'#define\s+FMC_RESERVED_(\w+)\s+(P[A-I]\d+)', 2, 1),
})

# --- upload lock ------------------------------------------------------------
# IAPTool and network_discovery coordinate purely through this file. A name
# mismatch means neither sees the other and the IDE's poller talks over an
# upload in progress.
compare_anchor("upload lock filename", {
    "IAPTool uploadlock.go": get_anchor(tool_lock, r'uploadLockName\s*=\s*"([^"]+)"'),
    "core tools/discovery/network_discovery.go":
        get_anchor(core_disc, r'uploadLockName\s*=\s*"([^"]+)"'),
})

compare_anchor("upload lock max age", {
    "IAPTool uploadlock.go": get_anchor(tool_lock, r'UploadLockMaxAge\s*=\s*(\d+\s*\*\s*time\.\w+)'),
    "core tools/discovery/network_discovery.go":
        get_anchor(core_disc, r'uploadLockMaxAge\s*=\s*(\d+\s*\*\s*time\.\w+)'),
})


# --- certificate wire format ------------------------------------------------
# The board parses a certificate as three fields at fixed offsets and the tool
# writes it the same way. Nothing on the wire announces the layout, so a
# divergence here does not fail loudly -- it verifies a signature over the
# wrong bytes and rejects every legitimate upload.
compare_anchor("certificate size (bytes)", {
    "bootloader IAPServer/iap_cert.h": get_anchor(boot_cert, r'#define\s+IAP_CERT_SIZE\s+(\d+)U'),
    "core OpenPLC_IAP/src/iap_cert.h": get_anchor(core_cert, r'#define\s+IAP_CERT_SIZE\s+(\d+)U'),
    "IAPTool iapcert/iapcert.go": get_anchor(tool_cert, r'Size\s*=\s*(\d+)'),
})

compare_anchor("certificate signed prefix (bytes)", {
    "bootloader IAPServer/iap_cert.h": get_anchor(boot_cert, r'#define\s+IAP_CERT_SIGNED_LEN\s+(\d+)U'),
    "core OpenPLC_IAP/src/iap_cert.h": get_anchor(core_cert, r'#define\s+IAP_CERT_SIGNED_LEN\s+(\d+)U'),
    "IAPTool iapcert/iapcert.go": get_anchor(tool_cert, r'SignedLen\s*=\s*(\d+)'),
})

# --- owner record format ----------------------------------------------------
# Three readers of the same 8 KiB in the bootloader's flash sector: the
# bootloader writes and resolves records, the app resolves them read-only, and
# the tool signs the prefix. A prefix length that disagrees signs the wrong
# bytes, and the board rejects a handover that was perfectly legitimate.
# The two segment geometries are anchored too: a count or a base that drifts
# has the app reading revocations out of the middle of an 'O' record.
compare_anchor("owner record format version", {
    "bootloader IAPServer/owner_slot.h": get_anchor(boot_owner, r'#define\s+OWNER_FORMAT_VER\s+(\d+)U'),
    "core OpenPLC_IAP/src/owner_root_ro.c": get_anchor(core_owner, r'#define\s+OWNER_FORMAT_VER\s+(\d+)U'),
    "IAPTool owner.go": get_anchor(tool_owner, r'ownerRecordFormatVer\s*=\s*(\d+)'),
    # Python copies that build records byte by byte; a stale one tests nothing.
    "test tools/run_setowner.py": get_anchor(TESTTOOL_DIR / "tools/run_setowner.py", r'(?m)^OWNER_FORMAT_VER\s*=\s*(\d+)'),
    "test tools/inject_owner_record.py": get_anchor(TESTTOOL_DIR / "tools/inject_owner_record.py", r'(?m)^OWNER_FORMAT_VER\s*=\s*(\d+)'),
    "test host/renode/run.py": get_anchor(TESTTOOL_DIR / "host/renode/run.py", r'(?m)^OWNER_FORMAT_VER\s*=\s*(\d+)'),
})

compare_anchor("owner record signed prefix (bytes)", {
    "bootloader IAPServer/owner_slot.h": get_anchor(boot_owner, r'#define\s+OWNER_SIGNED_PREFIX_LEN\s+(\d+)U'),
    "core OpenPLC_IAP/src/owner_root_ro.c": get_anchor(core_owner, r'#define\s+OWNER_SIGNED_PREFIX_LEN\s+(\d+)U'),
    "IAPTool owner.go": get_anchor(tool_owner, r'ownerSignedPrefixLen\s*=\s*(\d+)'),
})

# The two segments. Only the bootloader and the app address the area, so the
# tool has no anchor to offer here.
compare_anchor("root area base address", {
    "bootloader IAPServer/owner_slot.h": get_anchor(boot_owner, r'#define\s+OWNER_SLOT_BASE\s+(0x[0-9A-Fa-f]+)UL'),
    "core OpenPLC_IAP/src/owner_root_ro.c": get_anchor(core_owner, r'#define\s+OWNER_SLOT_BASE\s+(0x[0-9A-Fa-f]+)UL'),
})

compare_anchor("'O' segment: record size (bytes)", {
    "bootloader IAPServer/owner_slot.h": get_anchor(boot_owner, r'#define\s+OWNER_RECORD_SIZE\s+(\d+)U'),
    "core OpenPLC_IAP/src/owner_root_ro.c": get_anchor(core_owner, r'#define\s+OWNER_RECORD_SIZE\s+(\d+)U'),
})

compare_anchor("'O' segment: record count", {
    "bootloader IAPServer/owner_slot.h": get_anchor(boot_owner, r'#define\s+OWNER_SLOT_MAX_RECORDS\s+(\d+)U'),
    "core OpenPLC_IAP/src/owner_root_ro.c": get_anchor(core_owner, r'#define\s+OWNER_SLOT_MAX_RECORDS\s+(\d+)U'),
})

compare_anchor("'R' segment: record size (bytes)", {
    "bootloader IAPServer/owner_slot.h": get_anchor(boot_owner, r'#define\s+OWNER_REVOKE_REC_SIZE\s+(\d+)U'),
    "core OpenPLC_IAP/src/owner_root_ro.c": get_anchor(core_owner, r'#define\s+OWNER_REVOKE_REC_SIZE\s+(\d+)U'),
})

compare_anchor("'R' segment: record count", {
    "bootloader IAPServer/owner_slot.h": get_anchor(boot_owner, r'#define\s+OWNER_REVOKE_MAX_RECORDS\s+(\d+)U'),
    "core OpenPLC_IAP/src/owner_root_ro.c": get_anchor(core_owner, r'#define\s+OWNER_REVOKE_MAX_RECORDS\s+(\d+)U'),
})

compare_anchor("revoked-leaf name length (bytes)", {
    "bootloader IAPServer/owner_slot.h": get_anchor(boot_owner, r'#define\s+OWNER_REVOKE_PREFIX_LEN\s+(\d+)U'),
    "core OpenPLC_IAP/src/owner_root_ro.c": get_anchor(core_owner, r'#define\s+OWNER_REVOKE_PREFIX_LEN\s+(\d+)U'),
    "IAPTool owner.go": get_anchor(tool_owner, r'ownerRevokePrefixLen\s*=\s*(\d+)'),
})

# --- RTC backup registers ---------------------------------------------------
# Not a mirror but the same failure mode: a shared resource with no allocator.
# Claiming one that the other image already uses cost a real bug (bootloader's
# VBAT witness vs the app's nonce counter, both on DR2, 2026-08-17).
Section("RTC backup register claims")

claims = {}
scan = [
    ("bootloader IAPServer/iap_auth.c", BOOT / "IAPServer" / "iap_auth.c"),
    ("core OpenPLC_IAP/src/iap_auth.c", LIVE / "libraries" / "OpenPLC_IAP" / "src" / "iap_auth.c"),
    # backup.h defines the index STM32RTC would write. Nothing writes it today,
    # but it is a claim, and leaving it out of the scan is what let it sit on top
    # of the bootloader's counter unnoticed until 2026-09-23.
    ("core cores/arduino/stm32/backup.h", LIVE / "cores" / "arduino" / "stm32" / "backup.h"),
]
for label, path in scan:
    if not path.exists():
        Warn("SKIP  %s not found" % label)
        skipped += 1
        continue
    text = read_text(path)
    for m in re.finditer(r'RTC_BKP_DR(\d+)', text):
        dr = "DR" + m.group(1)
        claims.setdefault(dr, [])
        if label not in claims[dr]:
            claims[dr].append(label)

for dr in sorted(claims):
    if len(claims[dr]) > 1:
        Fail("CLASH %s claimed by: %s" % (dr, " AND ".join(claims[dr])))
        failed += 1
    else:
        Ok("OK    %s <- %s" % (dr, claims[dr][0]))
print("      (the allocation table in $PROD/docs/repo/ARCHITECTURE.md is the record; this scans")
print("       the two iap_auth.c files and the core's backup.h)")

# --- physical interface selection -------------------------------------------
# A VPN tunnel or a Docker switch can hold a better default route than the real
# NIC, and then every probe leaves through it and times out -- which reads as
# "the board is not answering". That cost a full test round on 2026-09-18.
# Decision 51; item 10 of the mirror table in $PROD/docs/repo/ARCHITECTURE.md.
compare_anchor("physical interface filter", {
    "core tools/discovery/network_discovery.go": get_anchor(
        core_disc, BODY_IS_PHYSICAL % "isPhysicalInterface"),
    "tool netiface/netiface.go": get_anchor(
        tool_netiface, BODY_IS_PHYSICAL % "IsPhysical"),
})

# The three classifiers are what makes the rule work off Windows. Compared one
# platform at a time so a failure names the platform that drifted.
for _plat, _core, _tool in (
    ("windows", core_iface_win, tool_iface_win),
    ("linux", core_iface_lin, tool_iface_lin),
    ("darwin", core_iface_mac, tool_iface_mac),
):
    compare_anchor("virtual-adapter classifier (%s)" % _plat, {
        "core tools/discovery/iface_%s.go" % _plat: get_anchor(_core, BODY_CLASSIFY),
        "tool netiface/iface_%s.go" % _plat: get_anchor(_tool, BODY_CLASSIFY),
    })

# --- whole-file and whole-function mirrors (decision 65) ---------------------
#
# Stricter than an anchor, and they can be: these two files carry no
# repo-specific content at all, so anything that differs is drift.
Section("byte-identical files")
for _name in ("sha256.c", "iap_cert.c"):
    compare_bytes(_name, BOOT / "IAPServer" / _name,
                  LIVE / "libraries/OpenPLC_IAP/src" / _name)

# iap_auth.c is NOT byte-identical and must not be: the Arduino side carries
# only the verifying half. These two functions happen to be shared whole.
_boot_auth = BOOT / "IAPServer/iap_auth.c"
_core_auth = LIVE / "libraries/OpenPLC_IAP/src/iap_auth.c"

Section("iap_auth.c: the functions both copies carry whole")
# rng_words() is not here on purpose: the two reach different RNG handles (the
# bootloader uses CubeMX's, the core owns its own). Decision 66.
for _fn in ("iap_auth_issue_challenge",):
    compare_anchor("iap_auth.c %s()" % _fn, {
        "bootloader IAPServer/iap_auth.c": get_function_body(_boot_auth, _fn),
        "core OpenPLC_IAP/src/iap_auth.c": get_function_body(_core_auth, _fn),
    })

# iap_auth_verify_and_consume is deliberately NOT compared whole: only the
# digest it verifies against has to match, and that is the part a one-sided
# edit would break without any compiler noticing.
compare_anchor("iap_auth.c: bytes the challenge signature covers", {
    "bootloader IAPServer/iap_auth.c": get_signed_bytes_recipe(_boot_auth),
    "core OpenPLC_IAP/src/iap_auth.c": get_signed_bytes_recipe(_core_auth),
})

# The machine ID is what a board calls itself on the wire and what its MAC is
# derived from, so the two copies disagreeing means one board answering to two
# identities depending on which image is running. Only the includes differ
# (main.h here, Arduino.h there), so the bodies compare cleanly.
Section("iap_keyderive.c: the machine ID both copies derive")
for _fn in ("iap_keyderive_get_machine_id", "iap_keyderive_get_machine_id_hex"):
    compare_anchor("iap_keyderive.c %s()" % _fn, {
        "bootloader IAPServer/iap_keyderive.c": get_function_body(
            BOOT / "IAPServer/iap_keyderive.c", _fn),
        "core OpenPLC_IAP/src/iap_keyderive.c": get_function_body(
            LIVE / "libraries/OpenPLC_IAP/src/iap_keyderive.c", _fn),
    })

# --- calibration area format ------------------------------------------------
# The fixture writes it, the app reads it; a drift means every board reads its
# own calibration as blank or corrupt and silently falls back to nominal.
# Format: $PROD/docs/modules/M1/SECTOR-15.md, "校准值区的格式".
Section("calibration area format")


def get_calib_layout_c(path):
    if not Path(path).exists():
        return None
    text = read_text(path)
    parts = []
    for k in ("CALIB_AREA_ADDR", "CALIB_MAGIC", "CALIB_VERSION", "CALIB_CHANNELS"):
        m = re.search(r'#define\s+%s\s+(0x[0-9A-Fa-f]+|\d+)' % k, text)
        if not m:
            return None
        parts.append("%s=%d" % (k.split("_")[-1], int(m.group(1).rstrip("UL"), 0)))
    m = re.search(r'sizeof\(calib_area_t\)\s*==\s*(\d+)', text)
    st = re.search(r'typedef struct \{([^{}]*)\} calib_area_t;', text)
    ch = re.search(r'typedef enum \{(.*?)\} calib_channel_id_t;', text, re.S)
    if not (m and st and ch):
        return None
    parts.append("SIZE=%s" % m.group(1))
    parts.append("fields=" + ",".join(re.findall(r'\b(\w+)(?:\[\w+\])?\s*;', st.group(1))))
    parts.append("channels=" + ",".join(re.findall(r'CALIB_CH_(\w+)', ch.group(1))))
    return ";".join(parts)


def get_calib_layout_go(path):
    """The same anchor from PortTool's internal/calarea/calarea.go, which writes it."""
    if not Path(path).is_file():
        return None
    # Normalised to LF: the patterns below anchor on "\n".
    text = read_text(path).replace("\r\n", "\n")
    parts = []
    for c, k in (("Addr", "ADDR"), ("Magic", "MAGIC"), ("Version", "VERSION"), ("Channels", "CHANNELS")):
        m = re.search(r'\b%s\s*=\s*(0x[0-9A-Fa-f]+|\d+)' % c, text)
        if not m:
            return None
        parts.append("%s=%d" % (k, int(m.group(1), 0)))
    m = re.search(r'\bSize\s*=\s*(\d+)', text)
    st = re.search(r'type area struct \{(.*?)\n\}', text, re.S)
    ch = re.search(r'const \(\n\s*AI1 = iota(.*?)\n\)', text, re.S)
    if not (m and st and ch):
        return None
    parts.append("SIZE=%s" % m.group(1))
    parts.append("fields=" + ",".join(re.findall(r'^\s*(\w+)\s', st.group(1), re.M)))
    parts.append("channels=" + ",".join(["AI1"] + re.findall(r'^\s*(\w+)\s', ch.group(1), re.M)))
    return ";".join(parts)


# Three parties, one check (decision 78): the bootloader owns the format, the
# app reads it, PortTool writes it.
compare_anchor("calibration area layout", {
    "bootloader IAPServer/calib_area.h": get_calib_layout_c(BOOT / "IAPServer/calib_area.h"),
    "core OpenPLC_Ports/src/openplc_calib.h": get_calib_layout_c(LIVE / "libraries/OpenPLC_Ports/src/openplc_calib.h"),
    "porttool internal/calarea/calarea.go": get_calib_layout_go(PORTTOOL / "internal/calarea/calarea.go"),
})

# --- AI conversion constants (mirror 14) --------------------------------------
# The fixture computes the nominal value it fits against with the same divider,
# shunt and full scale the app applies; if they differ, every coefficient is off.
def get_ai_consts_c(path):
    if not Path(path).exists():
        return None
    t = read_text(path)
    div = re.search(r"pin_mv\(raw\)\s*\*\s*\(([\d.]+)f\s*/\s*([\d.]+)f\)", t)
    shunt = re.search(r"pin_mv\(raw\)\s*/\s*([\d.]+)f", t)
    fs = re.search(r"raw\s*\*\s*([\d.]+)f\s*/\s*\(float\)OPENPLC_ADC_FULL", t)
    if not (div and shunt and fs):
        return None
    return "div=%g/%g;shunt=%g;fs=%g" % (float(div.group(1)), float(div.group(2)),
                                          float(shunt.group(1)), float(fs.group(1)))


def get_ai_consts_js(path):
    if not Path(path).exists():
        return None
    t = read_text(path)
    div = re.search(r"aicalPinMv\(raw\)\s*\*\s*([\d.]+)\s*/\s*([\d.]+)", t)
    shunt = re.search(r"aicalPinMv\(raw\)\s*/\s*([\d.]+)", t)
    fs = re.search(r"function aicalPinMv\(raw\)\s*\{\s*return raw\s*\*\s*([\d.]+)\s*/", t)
    if not (div and shunt and fs):
        return None
    return "div=%g/%g;shunt=%g;fs=%g" % (float(div.group(1)), float(div.group(2)),
                                          float(shunt.group(1)), float(fs.group(1)))


compare_anchor("AI conversion constants", {
    "core OpenPLC_Ports/src/openplc_analog.c": get_ai_consts_c(LIVE / "libraries/OpenPLC_Ports/src/openplc_analog.c"),
    "porttool ptpanel/web/index.html": get_ai_consts_js(PORTTOOL / "internal/ptpanel/web/index.html"),
})

# --- what this script does not check ----------------------------------------
Section("not covered by this script -- still manual")
print("  - $CORE_LIVE vs $CORE_REPO: use tools/check_core_sync.py")

Section("result")
if failed > 0:
    Fail("%d anchor(s) diverged, %d skipped" % (failed, skipped))
    sys.exit(1)
if skipped > 0:
    Warn("all compared anchors agree, but %d could not be checked" % skipped)
    sys.exit(0)
Ok("all mirrored anchors agree")
sys.exit(0)
