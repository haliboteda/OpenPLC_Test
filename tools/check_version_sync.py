"""Checks that the firmware version agrees everywhere it is written down.

The bootloader and the Arduino core each carry their own copy of the version
string and nothing links them. When they drifted (bootloader stuck on 0.1.2
while the core said 0.1.3) the same board reported two versions and looked
like a failed upgrade. This is release-checklist item B1, automated.

Exit 0 = all agree, 1 = drift, 2 = a file is missing.

been shown to reach the same verdict through both.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import cfg, Section, Ok, Warn, Fail, read_text          # noqa: E402


def get_one(path, pattern, what, flags=0):
    if not Path(path).exists():
        Fail("missing: %s" % path)
        sys.exit(2)
    m = re.search(pattern, read_text(path), flags)
    if not m:
        Fail("no %s in %s" % (what, path))
        sys.exit(2)
    return m.group(1).strip()


boot_cfg = Path(cfg.BOOT_REPO) / "Core" / "Inc" / "IAP_config.h"
boards = Path(cfg.CORE_LIVE) / "boards.txt"
rel_notes = Path(cfg.BOOT_REPO) / "RELEASE-NOTES.md"

v_boot = get_one(boot_cfg, r'#define\s+OPENPLC_FW_VERSION\s+"([^"]+)"', "OPENPLC_FW_VERSION")
v_core = get_one(boards, r'OPEN-PLC\.build\.fw_version\s*=\s*(\S+)', "build.fw_version")
v_notes = get_one(rel_notes, r'^##\s+.*?(\d+\.\d+\.\d+)', "version heading", re.M)

Section("firmware version")
print("  bootloader  Core/Inc/IAP_config.h      %s" % v_boot)
print("  core        boards.txt                 %s" % v_core)
print("  notes       RELEASE-NOTES.md heading   %s" % v_notes)

bad = 0
if v_boot != v_core:
    Fail("bootloader and core disagree")
    bad += 1
if v_boot != v_notes:
    Fail("bootloader and RELEASE-NOTES disagree")
    bad += 1

# The known-issues section outlives the fix more often than not: an entry that
# names an older version is either stale or a genuinely unfixed regression, and
# either way someone has to look.
#
# The phrase test here is deliberately case-insensitive, so it is
# too. Getting that wrong would silently narrow the check.
stale = []
for line_no, line in enumerate(read_text(rel_notes).splitlines(), 1):
    for m in re.finditer(r'(\d+\.\d+\.\d+)', line):
        if m.group(1) != v_boot and re.search(r'reports version|still\s+`?\d', line, re.I):
            stale.append("    RELEASE-NOTES.md:%d  %s" % (line_no, line.strip()))
stale = list(dict.fromkeys(stale))      # -Unique, keeping first-seen order
if stale:
    Warn("RELEASE-NOTES.md mentions an older version in what reads like a live issue:")
    for s in stale:
        Warn(s)
    Warn("  -> if it is fixed, delete the entry; a stale known-issue is worse than none")

Section("result")
if bad > 0:
    Fail("version drift")
    sys.exit(1)
Ok("all three agree on %s" % v_boot)
sys.exit(0)
