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

Exit 0 = the package can do everything the repository can, 1 = it is behind,
2 = one of the two binaries is missing.
"""

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (Fail, Ok, Section, Warn, get_go_bin,  # noqa: E402
                    get_iap_tool, run_capture)

VERB_RE = re.compile(r"^\s*IAPTool\s+([a-z]+)", re.M)


def verbs(binary):
    """The subcommands a binary admits to having, from its own usage screen."""
    out, _ = run_capture([binary])          # no arguments: it prints usage and exits non-zero
    return set(VERB_RE.findall(out)), out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--list", action="store_true", help="print both usage screens")
    args = ap.parse_args()

    Section("the two copies")
    built = get_go_bin("IAPTool")
    if not built.exists():
        Fail("no built IAPTool at %s -- run compile_tool.sh" % built)
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

    Section("result")
    missing = sorted(built_verbs - ship_verbs)
    extra = sorted(ship_verbs - built_verbs)
    if extra:
        Warn("the package has verbs the repository does not: %s" % " ".join(extra))
        Warn("  that means the package was built from something other than this checkout")
    if missing:
        Fail("the packaged IAPTool cannot do: %s" % " ".join(missing))
        Fail("The IDE's Upload button runs the packaged copy, so those features do not")
        Fail("reach a customer. Rebuild and copy it into the package:")
        Fail("  bash compile_tool.sh")
        Fail("  cp %s %s" % (built, shipped))
        return 1
    Ok("the packaged IAPTool exposes everything this checkout does")
    print("  (a matching command surface, not a matching build -- see the docstring)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
