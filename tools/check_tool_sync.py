"""The IAPTool inside the board package must not be older than the repository's.

Case P11. The Arduino IDE does not run the IAPTool we build and test: its upload
recipe runs the copy shipped inside the board package
(`$A15/packages/OpenPLC_Alpha/tools/STM32Tools/<ver>/<platform>/IAPTool`), with
its own `keys/` beside it. So the binary a customer drives from the Upload
button can be weeks behind the one every other case here exercises, and nothing
would say so -- which is exactly what happened: on 2026-09-03 the packaged copy
predated the ownership commands by three weeks.

    python3 tools/check_tool_sync.py            compare the two
    python3 tools/check_tool_sync.py --list     also print both usage screens

What it compares: the command surface each binary reports itself, by running it
with no arguments and collecting the "IAPTool <verb>" lines out of its usage.
Not a hash -- two builds of the same source differ byte for byte, and a check
that cries wolf gets ignored. A verb the repository has and the package does not
is a real defect: the menu cannot reach that feature.

Also: the package's four IAPTool binaries (win, linux, macosx/IAPTool-amd64,
macosx/IAPTool-arm64) must carry the same vcs.revision, read with `go version -m`
so none has to run on this host; and macosx/IAPTool must be byte-identical to
$TOOL/tools/macos-launcher.sh, the script that picks between the two (IDE-18).

Exit 0 = the package can do everything the repository can, 1 = it is behind,
its platform binaries differ or the macOS launcher is wrong, 2 = one of the two
binaries is missing.
"""

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (Fail, Ok, Section, Warn, cfg, get_go_bin,  # noqa: E402
                    get_iap_tool, run_capture)

VERB_RE = re.compile(r"^\s*IAPTool\s+([a-z]+)", re.M)

# Board package file -> the Go binary it must be, one per shipped platform build.
SHIPPED_BINARIES = (("win", "IAPTool.exe"), ("linux", "IAPTool"),
                    ("macosx", "IAPTool-amd64"), ("macosx", "IAPTool-arm64"))


def verbs(binary):
    """The subcommands a binary admits to having, from its own usage screen."""
    out, _ = run_capture([binary])          # no arguments: it prints usage and exits non-zero
    return set(VERB_RE.findall(out)), out


def shipped_revisions(shipped):
    """{plat/name: vcs.revision or None} for the IAPTool binaries beside shipped."""
    revs = {}
    for plat, name in SHIPPED_BINARIES:
        path = Path(shipped).parent.parent / plat / name
        out, rc = run_capture(["go", "version", "-m", str(path)]) if path.exists() else ("", 1)
        m = re.search(r"vcs\.revision=([0-9a-f]+)", out) if rc == 0 else None
        revs["%s/%s" % (plat, name)] = m.group(1) if m else None
    return revs


def mac_launcher_ok(shipped):
    """(ok, detail): macosx/IAPTool in the package is the repository's launcher."""
    path = Path(shipped).parent.parent / "macosx" / "IAPTool"
    source = Path(cfg.IAPTOOL_REPO) / "tools" / "macos-launcher.sh"
    if not path.exists():
        return False, "missing: %s" % path
    if not source.exists():
        return False, "no launcher source at %s" % source
    if path.read_bytes() != source.read_bytes():
        return False, "%s differs from %s" % (path, source)
    return True, str(path)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--list", action="store_true", help="print both usage screens")
    args = ap.parse_args()

    Section("the two copies")
    built = get_go_bin("IAPTool")
    if not built.exists():
        Fail("no built IAPTool at %s -- run build.py in IAPTranfer_Tool" % built)
        return 2
    shipped = get_iap_tool()
    if shipped is None or not Path(shipped).exists():
        Fail("no IAPTool inside the board package")
        return 2

    for label, path in (("repository", built), ("board package", shipped)):
        st = Path(path).stat()
        print("  %-14s %s" % (label, path))
        print("  %-14s %d bytes, modified %s" % ("", st.st_size,
              __import__("time").strftime("%Y-%m-%d %H:%M", __import__("time").localtime(st.st_mtime))))

    Section("command surface")
    built_verbs, built_out = verbs(built)
    ship_verbs, ship_out = verbs(shipped)
    if not built_verbs:
        Fail("could not read the repository binary's usage screen")
        return 2
    print("  repository:    %s" % " ".join(sorted(built_verbs)))
    print("  board package: %s" % " ".join(sorted(ship_verbs)))

    if args.list:
        Section("repository usage")
        print(built_out.strip())
        Section("board package usage")
        print(ship_out.strip())

    Section("the package's platform binaries")
    revs = shipped_revisions(shipped)
    for plat, rev in revs.items():
        print("  %-21s %s" % (plat, rev or "MISSING or no vcs.revision"))
    platforms_differ = None in revs.values() or len(set(revs.values())) != 1
    launcher_ok, launcher_detail = mac_launcher_ok(shipped)
    print("  %-21s %s" % ("macosx/IAPTool", "launcher" if launcher_ok else launcher_detail))

    Section("result")
    if platforms_differ:
        Fail("the package's %s are not one build -- rebuild them all"
             % " / ".join(revs))
    if not launcher_ok:
        Fail("macosx/IAPTool is not the launcher from build.py -- rebuild and reinstall")
    missing = sorted(built_verbs - ship_verbs)
    extra = sorted(ship_verbs - built_verbs)
    if extra:
        Warn("the package has verbs the repository does not: %s" % " ".join(extra))
        Warn("  that means the package was built from something other than this checkout")
    if missing:
        Fail("the packaged IAPTool cannot do: %s" % " ".join(missing))
        Fail("The IDE's Upload button runs the packaged copy, so those features do not")
        Fail("reach a customer. Rebuild and copy it into the package:")
        Fail("  python build.py      (ends by running tools/install_tool.py)")
        return 1
    if platforms_differ or not launcher_ok:
        return 1
    Ok("the packaged IAPTool exposes everything this checkout does")
    print("  (a matching command surface, not a matching build -- see the docstring)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
