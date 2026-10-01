"""Drives the real IAPTool against fake_board.py and checks the decision it
makes about who may talk to this board -- before any firmware is sent. Cases
T1-18a-T1-18g, plus T2-28-T2-30 for claiming a board that has no root.

Why this cannot be done on a real board: the outcomes below differ only in
which root the board trusts, and in what key and certificate are present on
the host. Here both are arguments.

What is under test is IAPTool, not the device. fake_board.py verifies nothing;
device-side verification is covered by S1 against real hardware.

    python run_cases.py              run all cases
    python run_cases.py --keep       keep the scratch directory for inspection

Two things worth knowing:

  * "python is not on PATH" is not checked. This interpreter is what launches
    fake_board.py, so there is nothing to look up -- gating on the literal
    "python" is what would stop the suite on a python3-only machine.
  * the IAPTool copy keeps the platform's executable suffix rather than
    hardcoding ".exe".

Exit 0 = all matched, 1 = at least one did not, 2 = prerequisites missing.
"""

import argparse
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (Fail, Ok, Section, build_iap_tool,  # noqa: E402
                     fixed_bytes, have_cmd, isolated_env, nonblank_lines,
                     parse_pubkey, read_text, resolve_port, run_env,
                     stage_iap_tool, start_fake_board, stop_fake_board,
                     user_key_path, wait_for_listener)


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    if not have_cmd("go"):
        Fail("go is not on PATH")
        return 2

    iap_tool = build_iap_tool()
    port = resolve_port()

    scratch = Path(tempfile.mkdtemp(prefix="fakeboard-"))
    print("scratch: %s" % scratch)

    iap_run = stage_iap_tool(scratch, iap_tool, port)
    # The tool reads and generates its default key under the user config dir;
    # every run here gets one inside scratch, never the real user's.
    env = isolated_env(scratch)
    user_key = user_key_path(env)

    def genkey(name):
        out, _ = run_env([iap_run, "genkey", name], env, cwd=scratch)
        return parse_pubkey(out), scratch / (name + ".pem")

    # The board's root, and an unrelated key pair for the mismatch cases.
    # IAPTool makes both, so nothing has to be committed.
    good_hex, good_key = genkey("good_key")
    bad_hex, _ = genkey("other_key")
    if len(good_hex) != 128 or len(bad_hex) != 128:
        Fail("IAPTool genkey printed no public key")
        return 2

    bin_path = scratch / "app.bin"
    bin_path.write_bytes(fixed_bytes(2048, 31, 7))

    # A root of our own, and a leaf it certifies. Issuing a delegated
    # certificate advances the counter beside the issuing key, so it is done
    # with a scratch root rather than the repository's -- a host test must not
    # write into a checked-out tree.
    root_hex, root_key = genkey("cert_root")
    leaf_hex, leaf_key = genkey("leaf_key")
    if len(root_hex) != 128 or len(leaf_hex) != 128:
        Fail("IAPTool genkey output did not parse to a 128-hex-char key")
        return 2

    # The certificate lives at "<the key it covers>.cert", which is where
    # IAPTool looks when no --cert is given -- the same path an Arduino install
    # would use, since the IDE passes no options at all.
    def issue_cert(leaf_pub_hex, dest):
        out, rc = run_env([iap_run, "cert", leaf_pub_hex, "--key=%s" % root_key], env, cwd=scratch)
        line = next((ln.strip() for ln in out.splitlines()
                     if re.fullmatch(r"[0-9a-f]{256}", ln.strip())), None)
        if line is None:
            Fail("IAPTool cert produced no certificate (rc=%d):\n%s" % (rc, out))
            return False
        Path(dest).write_text(line + "\n", encoding="utf-8")
        return True

    if not issue_cert(leaf_hex, str(leaf_key) + ".cert"):
        return 2
    # Same root, but issued for somebody else's key: the tool must notice
    # before the board does.
    wrong_leaf_cert = scratch / "wrong_leaf.pem"
    if not issue_cert(bad_hex, str(wrong_leaf_cert) + ".cert"):
        return 2
    shutil.copy2(str(leaf_key), str(wrong_leaf_cert))

    # A key already at the default location, for the "reuse it" case.
    existing = scratch / "existing_key.pem"
    existing_hex, _ = genkey("existing_key")

    # id, board's pubkey, --key to pass (or ""), expected lines,
    # key to place at the default location (or None), extra check
    cases = [
        {"id": "key-match", "pub": good_hex, "key": good_key,
         "expect": ["Signing key matches this board"]},
        {"id": "key-mismatch", "pub": bad_hex, "key": good_key,
         "expect": ["verifies against a different signing key",
                    "IAPTool pubkey", "IAPTool cert"]},
        {"id": "old-bootload", "pub": "unknown", "key": good_key,
         "expect": ["skipping key match check"]},
        {"id": "cert-match", "pub": root_hex, "key": leaf_key,
         "expect": ["Certificate was issued by this board's root"]},
        {"id": "cert-wrong-root", "pub": bad_hex, "key": leaf_key,
         "expect": ["was not issued by this board's root"]},
        {"id": "cert-key-mismatch", "pub": root_hex, "key": wrong_leaf_cert,
         "expect": ["was issued for a different key"]},
        {"id": "no-key", "pub": good_hex, "key": "",
         "expect": ["no signing key found"]},
        # T2-28: a factory board, no key anywhere: one is generated at the
        # default location, the board is claimed for it, the upload goes on.
        {"id": "claim-new-key", "pub": "none", "key": "",
         "expect": ["Private key written to: %s" % user_key, "Claimed.",
                    "Signing key: %s" % user_key],
         "check": "claimed-generated"},
        # T2-29: a factory board, a key already at the default location: that
        # key is used and left as it was.
        {"id": "claim-reuse-key", "pub": "none", "key": "", "place": existing,
         "expect": ["Claimed.", "Signing key: %s" % user_key],
         "check": "claimed-existing"},
        # T2-30: the board is claimed by somebody else: both ways to get a key
        # it trusts are named, with this machine's key path in them.
        {"id": "other-owner", "pub": bad_hex, "key": "", "place": existing,
         "expect": ["belongs to another key", str(user_key),
                    "IAPTool pubkey", "IAPTool cert"]},
    ]

    failed = 0

    for c in cases:
        Section("%s  -- expecting: %s" % (c["id"], c["expect"][0]))

        if user_key.exists():
            user_key.unlink()
        if c.get("place"):
            user_key.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(c["place"]), str(user_key))
        placed = user_key.read_bytes() if user_key.exists() else None

        board, board_log, handles = start_fake_board(
            scratch, c["id"], [c["pub"], "30", "--port", port])

        if not wait_for_listener(port):
            Fail("fake board never listened on %s" % port)
            stop_fake_board(board, handles)
            if board_log.exists():
                for line in re.split(r"\r?\n", read_text(board_log)):
                    print("  %s" % line)
            failed += 1
            continue

        argv = [iap_run, "ether", bin_path, "127.0.0.1"]
        if c["key"]:
            argv.append("--key=%s" % c["key"])
        out, _ = run_env(argv, env)

        # log= so the process is provably gone before the next case binds the
        # same port -- see stop_fake_board().
        stop_fake_board(board, handles, log=board_log)
        blog = read_text(board_log)

        # Deliberately case-insensitive.
        problems = ["expected %r" % e for e in c["expect"] if e.lower() not in out.lower()]
        check = c.get("check")
        if check:
            m = re.search(r"TAKEOWN ACCEPTED ([0-9a-f]{128})", blog)
            claimed = m.group(1) if m else ""
            if not claimed:
                problems.append("the board was never claimed")
            elif check == "claimed-generated":
                gen_hex, _ = run_env([iap_run, "pubkey", user_key], env)
                if claimed not in gen_hex:
                    problems.append("the board was claimed for a key other than the generated one")
            elif check == "claimed-existing":
                if claimed != existing_hex:
                    problems.append("the board was claimed for a key other than the existing one")
                if user_key.read_bytes() != placed:
                    problems.append("the existing key was replaced")
            if "IMAGE FULLY RECEIVED" not in blog:
                problems.append("the upload did not follow the claim")

        if not problems:
            Ok("PASS")
        else:
            Fail("FAIL - %s. IAPTool said:" % "; ".join(problems))
            for line in nonblank_lines(out):
                print("    %s" % line)
            failed += 1

    if not args.keep:
        shutil.rmtree(str(scratch), ignore_errors=True)
    else:
        print("kept: %s" % scratch)

    Section("result")
    if failed > 0:
        Fail("%d of %d case(s) failed" % (failed, len(cases)))
        return 1
    Ok("all %d key-match and claim cases behaved as expected" % len(cases))
    return 0


if __name__ == "__main__":
    sys.exit(main())
