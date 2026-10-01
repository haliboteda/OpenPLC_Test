"""Warns when a repo's personal permission allowlist has stopped being rules
and started being a diary.

Case P10. Added 2026-08-23. `.claude/settings.local.json` grows one line every
time a person approves "don't ask again" for an exact command Claude just
typed. Nothing ever removes a line. Left alone this accumulates into hundreds
of dead entries -- one project's `allow` array reached 481, of which 426 were
one-off literal commands (an absolute machine path, a specific `--first 45`
already baked in) that will never match again verbatim, while the one repo
that runs all the actual test scripts had zero entries and prompted for
everything.

This does not fail the run for having a long list -- a large *portable*
allowlist (relative paths, trailing-`*` prefixes) is exactly the fix, not the
problem. It flags entries that look like they can only ever match once:

  * an absolute path baked into the pattern (`E:\\...`, `/e/...`, `C:\\...`)
    that is not simply a personally-installed tool this machine looked up
    (those legitimately live here; see this repo's CLAUDE.md's
    "跨仓调用用相对路径" -- the object is cross-repo *script* invocations,
    not per-machine install paths)
  * a `Select-Object -First/-Last N` or similar count baked into the allowed
    string -- the number came from one specific run's output, not from the
    command itself

`.claude/settings.local.json` is gitignored and per-machine by design, so this
is advisory, not a release gate: it warns and exits 0 unless a repo's list has
grown past a size where "prune it" stops being optional (`--fail-over`).

    python tools/check_allow_hygiene.py                 report for every repo
    python tools/check_allow_hygiene.py --repo TEST_REPO just this repo
    python tools/check_allow_hygiene.py --fail-over 400 exit 1 if any repo tops this

Exit 0 = nothing over the line (or purely informational run), 1 = a repo is
over --fail-over, 2 = setup problem (no repo paths resolved at all).
"""

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import cfg, Section, Ok, Warn, Fail  # noqa: E402

REPO_KEYS = ("BOOT_REPO", "CORE_REPO", "TEST_REPO", "IAPTOOL_REPO", "HW_REPO", "REF_REPO")

# A drive-letter or POSIX-absolute prefix baked into the pattern text itself.
ABS_PATH = re.compile(r'[A-Za-z]:[\\/]|(?<![\w.])/[a-zA-Z]/[\w.]')
# Output already sliced to one run's shape -- "-First 45", "-Last 13", "head -20".
BAKED_COUNT = re.compile(r'-(First|Last)\s+\d+|head\s+-\d+|tail\s+-\d+', re.I)


def load_allow(settings_local):
    if not settings_local.is_file():
        return None
    try:
        data = json.loads(settings_local.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data.get("permissions", {}).get("allow", [])


def classify(entries):
    one_off = [e for e in entries if ABS_PATH.search(e) or BAKED_COUNT.search(e)]
    return one_off


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", help="only check this repo key, e.g. TEST_REPO")
    ap.add_argument("--fail-over", type=int, default=None,
                     help="exit 1 if any checked repo's allow list exceeds this many entries")
    ap.add_argument("--list", action="store_true", help="print every flagged entry")
    args = ap.parse_args()

    keys = (args.repo,) if args.repo else REPO_KEYS
    Section("allow-list hygiene (P10) -- advisory, not a release gate")

    resolved = 0
    worst = 0
    for key in keys:
        repo = getattr(cfg, key, None)
        if not repo:
            continue
        repo_path = Path(repo)
        if not repo_path.is_dir():
            continue
        resolved += 1
        entries = load_allow(repo_path / ".claude" / "settings.local.json")
        if entries is None:
            print("  %-10s no settings.local.json (nothing to flag)" % key)
            continue
        flagged = classify(entries)
        worst = max(worst, len(entries))
        line = "  %-10s %4d allow entries, %4d look one-off" % (key, len(entries), len(flagged))
        (Warn if flagged else Ok)(line)
        if args.list:
            for e in flagged:
                print("      %s" % e[:160])

    if resolved == 0:
        Fail("no repo path resolved -- is config/machine.py set up? (see tools/init_machine.py)")
        return 2

    if args.fail_over is not None and worst > args.fail_over:
        Fail("a repo's allow list is over %d entries -- prune it. Cross-repo calls "
             "belong in relative form; see this repo's CLAUDE.md" % args.fail_over)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
