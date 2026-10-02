"""Every OpenPLC_Test check that needs no board, in one command.

This repo holds the contract and system layers (decision 78): what needs two or
more repos to run. Each component repo has its own selfcheck for its own logic;
the documents have theirs in OpenPLC_Docs (tools/check_docs.py). On-board cases
are not here -- they need a person or a board; --list names where they live.

    python tools/selfcheck.py              run everything
    python tools/selfcheck.py --quick      skip the slow ones (T1-18 on the bootloader stand-in, T3-05 Renode)
    python tools/selfcheck.py --list       say what each step is, run nothing

Exit 0 = all pass, 1 = at least one failed. A check whose prerequisite is absent
(no go, no Renode) reports SKIP and does not fail the run -- but the summary
always names it, because a silently skipped check reads as a pass.

Step ids ARE the case ids, so "P2 passed" has exactly one meaning.
"""

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from common import (Fail, Ok, Section, Warn, cfg,  # noqa: E402
                    have_cmd, probe, python_exe)
from flash_bootloader import looks_like_bootloader  # noqa: E402

TESTTOOL = HERE.parent
BOOT_IMAGE = Path(cfg.BOOT_REPO) / "Debug" / "open_plc_cube_ide.bin"
results = []

# What each step is, in run order. This is the ONE place the step list lives:
# --list prints it, and run_step() looks up "covers" here and raises if a step is
# missing, so the two can never drift apart.
#
# "covers" is the requirement id in $PROD/docs/tables/STATUS.md that this
# case is the evidence for. A case that covers nothing should not exist.
CATALOG = [
    ("ENV",          "-",                      "this machine has the toolchain"),
    ("H3",           "-",                      "TestCase.exe builds and go vet is clean (imports IAPTool's packages)"),
    ("P1",           "ENG-02",                 "firmware version agrees in all three places"),
    ("P2",           "ENG-03 R1-06 R1-07 R2-01 R1-14 R3-04", "cross-repo mirrored code has not diverged (calibration area: three ways)"),
    ("P11",          "ENG-05",                 "the packaged IAPTool is not behind the repository"),
    ("P20",          "R1-20",                  "$BOOT's golden_vectors.h has the shape the shipping IAPTool produces"),
    ("T1-18a-T1-18g", "R1-21",                 "IAPTool key/certificate match and first-upload claim against a stand-in board"),
    ("T3-05",        "R3-08",                  "the examples boot through the real bootloader in Renode"),
]
COVERS = {cid: covers for cid, covers, _ in CATALOG}

STATUS_DOC = "$PROD/docs/tables/STATUS.md"
CRITERIA_DOC = "$PROD/docs/engineering/HOW-TO-RUN-TESTS.md"


def print_catalog():
    """--list: what would run, and what each step is evidence for."""
    Section("selfcheck steps")
    print("  step ids are case ids; 'covers' points into %s" % STATUS_DOC)
    print("  pass/fail criteria for each case: %s" % CRITERIA_DOC)
    print("")
    width = max(len(c) for c, _, _ in CATALOG)
    cov = max(len(v) for _, v, _ in CATALOG)
    for cid, covers, name in CATALOG:
        print("  %-*s  covers %-*s  %s" % (width, cid, cov, covers, name))
    print("")
    print("  %d steps. --quick skips T1-18a-T1-18g and T3-05." % len(CATALOG))
    print("  On-board cases (tools/run_*.py, TestCase.exe against a board) are not here:")
    print("  each one's command is in %s." % CRITERIA_DOC)


def record(step_id, name, state, note=""):
    results.append({"id": step_id, "name": name, "state": state, "note": note})


def run_step(step_id, name, argv, needs=None, cwd=None, indent=0):
    """One step: announce what it is, run it, record PASS/SKIP/FAIL.

    needs is a command name or an absolute path -- a tool installed for one check
    only (HOST_CC) has no business being on PATH.

    indent shifts a child's output so it reads as belonging to the step. The
    Python suites format their own output and are left alone.
    """
    if step_id not in COVERS:
        raise KeyError("step %r is not in CATALOG -- add it there too" % step_id)

    Section("%s  %s" % (step_id, name))
    # Say what this proves before running it. "A12 passed" told nobody anything.
    print("  covers %s   (%s)" % (COVERS[step_id], STATUS_DOC))

    if needs and not have_cmd(str(needs)) and not Path(str(needs)).exists():
        Warn("SKIP - %s not found" % needs)
        record(step_id, name, "SKIP", "%s missing" % needs)
        return

    sys.stdout.flush()
    if indent:
        proc = subprocess.run([str(a) for a in argv],
                              cwd=None if cwd is None else str(cwd),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, errors="replace")
        for line in (proc.stdout or "").splitlines():
            print("%s%s" % (" " * indent, line))
        code = proc.returncode
    else:
        proc = subprocess.run([str(a) for a in argv],
                              cwd=None if cwd is None else str(cwd))
        code = proc.returncode

    if code == 0:
        Ok("PASS")
        record(step_id, name, "PASS")
    else:
        Fail("FAIL (exit %d)" % code)
        record(step_id, name, "FAIL", "exit %d" % code)


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--quick", action="store_true",
                    help="skip the slow ones (T1-18 on the bootloader stand-in, T3-05 Renode)")
    ap.add_argument("--list", action="store_true", dest="list_only",
                    help="say what each step is and what it covers, run nothing")
    args = ap.parse_args()

    if args.list_only:
        print_catalog()
        return 0

    # ------------------------------------------------------------------ ENV
    # What this machine actually has. Runs first because every failure below is
    # easier to read once you know whether the thing was even installed -- and
    # because on a freshly cloned machine this is the check that says what to go
    # and install. Nothing here fails the run: CubeIDE and a serial port are
    # needed to reach the board, not to pass the host-side checks.
    missing = probe()
    if missing:
        record("ENV", "this machine has the toolchain", "SKIP", ", ".join(missing))
    else:
        record("ENV", "this machine has the toolchain", "PASS")

    repo = cfg.TEST_REPO
    run_step("H3", "TestCase.exe builds and go vet is clean (imports IAPTool's packages)",
             ["go", "vet", "./..."], needs="go", cwd=repo, indent=2)
    run_step("P1", "firmware version agrees in all three places",
             [python_exe(), HERE / "check_version_sync.py"], cwd=repo)
    run_step("P2", "cross-repo mirrored code has not diverged (calibration area: three ways)",
             [python_exe(), HERE / "check_mirror_sync.py"], cwd=repo)
    run_step("P11", "the packaged IAPTool is not behind the repository",
             [python_exe(), HERE / "check_tool_sync.py"], cwd=repo)
    run_step("P20", "$BOOT's golden_vectors.h has the shape the shipping IAPTool produces",
             [python_exe(), HERE / "check_golden_vectors.py"], cwd=repo)

    if args.quick:
        for cid in ("T1-18a-T1-18g", "T3-05"):
            record(cid, dict((c, n) for c, _, n in CATALOG)[cid], "SKIP", "--quick")
    else:
        # The board side is the real bootloader built for the PC, which needs
        # the host compiler (run_cases.py builds it).
        run_step("T1-18a-T1-18g", "IAPTool key/certificate match and first-upload claim against a stand-in board",
                 [python_exe(), TESTTOOL / "host" / "fakeboard" / "run_cases.py"],
                 needs=getattr(cfg, "HOST_CC", "") or "gcc")
        # Debug/ is shared with the fixture build; T3-05 means nothing on that image.
        not_boot = looks_like_bootloader(BOOT_IMAGE) if BOOT_IMAGE.exists() else "no image built"
        if not_boot:
            Section("T3-05  the examples boot through the real bootloader in Renode")
            Warn("SKIP - %s: %s" % (BOOT_IMAGE, not_boot))
            record("T3-05", "the examples boot through the real bootloader in Renode", "SKIP",
                   "$BOOT/Debug/ is not a bootloader: %s" % not_boot)
        else:
            run_step("T3-05", "the examples boot through the real bootloader in Renode",
                     [python_exe(), TESTTOOL / "host" / "renode" / "run.py"],
                     needs=getattr(cfg, "RENODE", "") or "renode")

    Section("summary")
    width = max(len(r["name"]) for r in results)
    idw = max(len(r["id"]) for r in results)
    for r in results:
        line = "%-*s %-*s  %s" % (idw, r["id"], width, r["name"], r["state"])
        if r["note"]:
            line += " (%s)" % r["note"]
        {"PASS": Ok, "SKIP": Warn, "FAIL": Fail}[r["state"]](line)

    failed = sum(1 for r in results if r["state"] == "FAIL")
    skipped = sum(1 for r in results if r["state"] == "SKIP")

    print("")
    if failed > 0:
        Fail("%d failed - do not go to the board until these are green" % failed)
        return 1
    if skipped > 0:
        Warn("%d skipped - those areas are unverified on this machine" % skipped)
    Ok("host-side checks pass; next is ACCEPTANCE-CHECKLIST.md CHK-A4 (build) and CHK-A5 (flash)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
