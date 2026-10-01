"""T2-15 / T2-16 / T2-17 / T2-18 / T2-26 -- revoking ONE leaf by name.

    python tools/run_revoke_leaf.py --bin <app.bin> --current-key <owner.pem>
    python tools/run_revoke_leaf.py ... --compare-bytes 0x40000
                                             read back less flash, for a faster run
    python tools/run_revoke_leaf.py ... --stop-after T2-16
                                             leaves the board WITHOUT a runnable app

Handing the board to a new root (T2-12 - T2-14) kills every leaf the old root
issued. This is the other revocation: naming ONE leaf and leaving the rest of
them working. The four cases are the four halves of that sentence, so they are
one script -- the state they share (which leaf signed the installed app, which
leaf has been named) is not something an operator should carry between four
invocations by hand.

  T2-18  a revocation with a bad signature does not get recorded
  T2-15  after revoking, the board STILL starts the app that leaf signed
  T2-26  ...and says so, so the operator can find the boards wanting a re-upload
  T2-16  after revoking, that leaf cannot upload either, and the application
         region is not touched by the attempt
  T2-17  a DIFFERENT leaf of the same root still uploads and boots

T2-17 is not optional. Without it "the board refused the revoked leaf" and "this
board refuses everybody" are the same observation, and only one of them is the
product working. It is also the step that leaves the board usable again, so it
runs even when T2-15 or T2-16 failed.

T2-18 runs first because it is the only one that must change nothing: it is
judged by the board still being in the state it was in beforehand.

Requirement: R2-04 (revoking a delegated certificate). The criteria are in
$PROD/docs/modules/M2-ownership.md section 4, and the G table of
$PROD/maps/owner-revoke-and-boot-upgrade/CHANGE-LIST.md.

⚠️ PRECONDITION, established by this script but NOT created out of nothing:

  the board must already be CLAIMED, and --current-key must be the private half
  of the root it currently trusts.

owner_slot_revoke() refuses an unclaimed board outright ("board is unclaimed -
use takeown first"): with no owner there is nobody who can sign a revocation.
Claiming needs somebody holding BOOT0 and is its own case -- run
`python tools/run_takeown.py` first. Given a claimed board, this script builds
the rest of the precondition itself: it issues two leaf certificates from the
current root, uploads the application signed by the first, and checks the board
booted it. If any of that does not happen the run reports SETUP, not FAIL,
because nothing about revocation was tested.

⚠️ NOBODY IS NEEDED AT THE BOARD for this run. Resets go over ST-Link, and a
revocation is authorised by the current owner's signature rather than by a
button.

⚠️ THIS SPENDS OWNER RECORD SLOTS. Every revocation appends a record to the
bootloader's own sector, and those slots cannot be reclaimed without erasing the
bootloader. A full run spends one.

RECOVERY
  Run finished normally
      The board runs an app signed by the second leaf. The first leaf stays
      revoked for good -- that record does not go away.
  Run stopped between T2-15 and T2-17
      The board sits in the bootloader with no runnable application. Upload one
      signed by the SECOND leaf:
          IAPTool ether <app.bin> <ip> --key=<new_leaf.pem> --cert=<new_leaf.pem.cert>
      Both files are in the key directory printed at the start of the run.
  Back to an unrevoked board
      Reflash over ST-Link with `python tools/flash_bootloader.py` -- the owner
      records live in the bootloader's own sector, so erasing it to write the
      bootloader takes the revocation with it. Then claim again with
      `python tools/run_takeown.py`.

⚠️ WHAT THIS RUN PROVES THAT T2-13 DOES NOT. In T2-13 the refusal comes from
IAPTool's pre-flight check (auth.go, verifyIdentityMatchesDevice): the tool sees
the certificate was not issued by the board's root and stops before sending a
byte. Here the certificate IS still one the board's root issued -- revocation
does not change that -- so the tool has no reason to stop, the bytes go out, and
the refusal has to come from the bootloader's own check. That is the difference
worth having both cases for.

Exit 0 = every criterion passed, 1 = a criterion failed, 2 = precondition not
met, or the evidence needed to judge was never captured.
"""

import argparse
import hashlib
import re
import sys
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from common import (Fail, Ok, Section, Warn, assert_target_reachable,  # noqa: E402
                    cfg, get_go_bin, get_iap_tool, get_programmer_cli,
                    get_scratch_dir, get_scratch_file, nonblank_lines,
                    open_log_ports, read_log_ports, run_capture,
                    run_while_draining, tcp_command, wait_for_board)

# Core/Inc/usbd_cdc_flash.h: IAP_APP_ADDRESS, and IAP_APP_MAX_SIZE as the gap up
# to the journal sector. Read back over SWD, which is the only view of the
# application region that does not go through the code under test.
APP_BASE = 0x08020000
APP_MAX_SIZE = 0x1C0000

# Larger than IAP_APP_MAX_SIZE, so the board refuses it at the size check before
# erasing or staging anything. The same trick tools/enter_bootloader.py uses to
# move a running application into the bootloader without touching BOOT0.
OVERSIZE_BYTES = 2000000

# IAPServer/owner_slot.h: OWNER_REVOKE_PREFIX_LEN, the bytes of the leaf public
# key a revocation record names it by.
REVOKE_PREFIX_LEN = 16

# Printed on every boot in every ownership state, before the app/bootloader
# decision (IAP_server.c, bootloader_state_init). Its absence means the capture
# failed, not that the board had nothing to say -- and an empty capture must
# never be read as a pass.
LOG_CAPTURE_PROOF = "Bootloader state:"
LOG_APP_REJECTED = "App signature invalid or absent"
LOG_APP_STARTED = "** APP Mod"
LOG_IMAGE_ACCEPTED = "Checksum and signature OK"

# IAPTool's own words about the certificate it presented.
TOOL_CERT_ACCEPTED = "Certificate was issued by this board's root"

# The board's TCP answer to a refused command. The reason it refuses ("signature
# does not verify against the current owner") is printf'd to the SERIAL log, not
# returned over TCP -- so the TCP criterion is the refusal plus the state not
# moving, which is what the case is actually about.
BOARD_REFUSED = "Refused"

PASS, FAIL, SETUP = "PASS", "FAIL", "SETUP"


# ----------------------------------------------------------------- helpers
def hex_token(text, nchars):
    """The first run of exactly `nchars` hex characters, lowercased, or ""."""
    m = re.search(r"(?<![0-9a-fA-F])([0-9a-fA-F]{%d})(?![0-9a-fA-F])" % nchars, text)
    return m.group(1).lower() if m else ""


def iap_genkey(iap, keydir, name):
    """Run `IAPTool genkey` in keydir. Returns the .pem path, or None."""
    out, rc = run_capture([iap, "genkey", name], cwd=keydir)
    pem = Path(keydir) / (name + ".pem")
    if rc != 0 or not pem.exists():
        Fail("genkey %s failed:" % name)
        for line in nonblank_lines(out)[-6:]:
            print("    " + line)
        return None
    return pem


def iap_pubkey(iap, pem):
    """The public half of a private key, as the 128 hex characters the board speaks."""
    out, rc = run_capture([iap, "pubkey", str(pem)])
    pub = hex_token(out, 128)
    if rc != 0 or not pub:
        Fail("could not read the public key of %s:" % pem)
        for line in nonblank_lines(out)[-6:]:
            print("    " + line)
        return ""
    return pub


def iap_issue_cert(iap, root_pem, leaf_pub, cert_path):
    """Issue a leaf certificate for leaf_pub, signed by root_pem. Writes cert_path."""
    out, rc = run_capture([iap, "cert", leaf_pub, "--key=%s" % root_pem])
    cert = hex_token(out, 256)
    if rc != 0 or not cert:
        Fail("could not issue a certificate from %s:" % root_pem)
        for line in nonblank_lines(out)[-6:]:
            print("    " + line)
        return False
    Path(cert_path).write_text(cert + "\n", encoding="ascii")
    return True


def app_digest(cli, nbytes, tag):
    """SHA-256 of the application region, read back over SWD. None when unreadable.

    A digest rather than a word-by-word dump: the claim under test is "not one
    byte moved", and comparing two digests of the same span answers exactly that
    without printing half a megabyte. mode=HOTPLUG so reading does not reset a
    board that may be running the application.
    """
    out_path = Path(get_scratch_file("t2_rev_app_%s.bin" % tag))
    try:
        out_path.unlink()
    except OSError:
        pass
    out, rc = run_capture([cli, "-c", "port=SWD", "mode=HOTPLUG",
                           "-u", hex(APP_BASE), hex(nbytes), str(out_path)])
    if rc != 0 or not out_path.exists():
        Fail("could not read the application region over SWD:")
        for line in nonblank_lines(out)[-6:]:
            print("    " + line)
        return None
    data = out_path.read_bytes()
    if len(data) != nbytes:
        Warn("  read back %d bytes, asked for %d" % (len(data), nbytes))
    return hashlib.sha256(data).hexdigest()


def reset_and_capture(cli, ports, seconds):
    """Reset over ST-Link with the log ports already open, and return what came out."""
    open_ports = open_log_ports(ports)
    run_capture([cli, "-c", "port=SWD", "mode=UR", "-rst"])
    text = "\n".join(read_log_ports(open_ports, seconds).values())
    for line in nonblank_lines(text):
        print("    | " + line)
    return text


def upload(iap, image, ip, key, cert, ports, tail_seconds):
    """One upload through the shipping tool. Returns (exit code, tool text, board text).

    ⚠️ The board is still working when IAPTool exits -- it is verifying the
    staged image, erasing and copying. Do not reset here; keep listening instead,
    which is what catches the board rebooting into the application.
    """
    out_path = get_scratch_file("t2_rev.out")
    err_path = get_scratch_file("t2_rev.err")
    open_ports = open_log_ports(ports)
    argv = [iap, "ether", str(image), ip, "--key=%s" % key, "--cert=%s" % cert]
    print("  IAPTool ether %s %s --key=%s --cert=%s"
          % (Path(image).name, ip, Path(key).name, Path(cert).name))
    rc, buf = run_while_draining(argv, open_ports, out_path, err_path)
    tail = read_log_ports(open_ports, tail_seconds)      # also closes them
    for k in buf:
        buf[k] += tail.get(k, "")
    tool = (Path(out_path).read_text(encoding="utf-8", errors="replace")
            + Path(err_path).read_text(encoding="utf-8", errors="replace"))
    for line in nonblank_lines(tool):
        print("    T | " + line)
    serial = "\n".join(buf.values())
    for line in nonblank_lines(serial):
        print("    B | " + line)
    return rc, tool, serial


def move_to_bootloader(iap, ip, port, key, cert, ports):
    """Get a board that is running the application back into the bootloader.

    Offers an image larger than the application region: IAPTool performs its real
    authenticated reboot first, and the board then refuses the image at the size
    check, before erasing or staging anything.

    Needs an identity the board still trusts, so call it with a leaf that has NOT
    been revoked yet.
    """
    big = Path(get_scratch_file("t2_rev_oversize.bin"))
    if not big.exists() or big.stat().st_size < OVERSIZE_BYTES:
        with open(str(big), "wb") as fh:
            fh.truncate(OVERSIZE_BYTES)
    upload(iap, big, ip, key, cert, ports, 4)
    # The refusal is the expected outcome here, so IAPTool's exit code says
    # nothing useful. Ask the board which mode it is in instead: getpubkey is a
    # bootloader command, so an answer to it IS the answer.
    if not wait_for_board(ip, timeout=30.0, port=port):
        return False
    return re.fullmatch(r"[0-9a-fA-F]{128}",
                        tcp_command(ip, port, "getpubkey")) is not None


REVOKE_FREE_RE = re.compile(r"(\d+)/\d+ revoke slot\(s\) free")


def board_free_slots_from(log):
    """Free REVOCATION slots parsed out of a boot log already captured.

    Shares its pattern with board_free_slots() -- two copies of it drifted
    apart once already, and the stale one made T2-20 unjudgeable rather than
    failing loudly.
    """
    m = REVOKE_FREE_RE.search(log)
    return int(m.group(1)) if m else None


def board_revoked_count_from(log):
    """How many leaves the board reports as revoked, from a captured boot log.

    Judged as a DELTA, never against a fixed number: a board accumulates
    revocations across runs and they cannot be erased without reflashing the
    bootloader, so "expected exactly 2" is only ever true on a fresh board.
    """
    m = re.search(r"(\d+) leaf\(s\) revoked", log)
    return int(m.group(1)) if m else None


def board_free_slots(ip, port, cli, ports, seconds):
    """Free REVOCATION slots, read off a boot log. None when unreadable.

    The count only appears on the serial log, so this costs a reset -- there
    is no TCP command that reports it.

    Revocations have lived in their own segment since format_ver 4, and the
    boot line reports both: "N/32 owner slot(s) free, M/96 revoke slot(s)
    free". This wants M -- watching the owner count would make "a repeat
    revoke spends no slot" pass no matter what a revocation did, because
    revocations never touch that segment.
    """
    log = reset_and_capture(cli, ports, seconds)
    return board_free_slots_from(log)



def move_to_bootloader_selfsigned(iap, ip, port, owner_key, ports):
    """Bring a running application to the bootloader using the OWNER key alone.

    Same mechanism as move_to_bootloader (oversize image, real authenticated
    reboot), but self-signed: no leaf certificate involved. Used where the point
    is only to reach the bootloader, so a leaf that cannot drive the handshake
    would otherwise fail a case about something else entirely. See the ticket on
    leaf certificates that cannot drive the reboot handshake.
    """
    big = Path(get_scratch_file("t2_rev_oversize.bin"))
    if not big.exists() or big.stat().st_size < OVERSIZE_BYTES:
        with open(str(big), "wb") as fh:
            fh.truncate(OVERSIZE_BYTES)
    open_ports = open_log_ports(ports)
    out_path = get_scratch_file("t2_rev_self.out")
    err_path = get_scratch_file("t2_rev_self.err")
    run_while_draining([iap, "ether", str(big), ip, "--key=%s" % owner_key],
                       open_ports, out_path, err_path)
    read_log_ports(open_ports, 3)
    if not wait_for_board(ip, timeout=30.0, port=port):
        return False
    return re.fullmatch(r"[0-9a-fA-F]{128}",
                        tcp_command(ip, port, "getpubkey")) is not None


def board_generation(ip, port):
    """The board's current owner generation as an int, or None."""
    gen = tcp_command(ip, port, "getowner")
    return int(gen) if re.fullmatch(r"\d+", gen or "") else None


# ------------------------------------------------------------------ setup
def build_precondition(st, args):
    """Claimed board + an app signed by leaf A, and a second leaf B in reserve.

    Everything that goes wrong in here is a SETUP result: none of it is what the
    four cases are about, and a run that never reached the starting line has not
    disproved anything.
    """
    Section("Precondition 1/4  is the board claimed, and is --current-key its root?")
    if not wait_for_board(st["ip"], timeout=60.0, port=args.port):
        Fail("the board did not answer UDP discovery at %s" % st["ip"])
        return False

    # Everything below talks to the bootloader. A board running its application
    # answers none of it, so bring it over first -- self-signed with the owner
    # key, which needs no leaf certificate to exist yet.
    if board_generation(st["ip"], args.port) is None:
        print("  the board is running its application - bringing it to the bootloader")
        big = Path(get_scratch_file("t2_rev_oversize.bin"))
        if not big.exists() or big.stat().st_size < OVERSIZE_BYTES:
            with open(str(big), "wb") as fh:
                fh.truncate(OVERSIZE_BYTES)
        run_capture([st["iap"], "ether", str(big), st["ip"],
                     "--key=%s" % args.current_key])
        if not wait_for_board(st["ip"], timeout=30.0, port=args.port):
            Fail("  the board did not come back after the reboot request")
            return False

    gen = board_generation(st["ip"], args.port)
    root = tcp_command(st["ip"], args.port, "getpubkey").lower()
    print("  generation: %s" % gen)
    print("  root:       %s" % root)
    if gen is None:
        Fail("getowner did not answer with a number")
        Fail("  is the board sitting in the bootloader?")
        return False
    if gen == 0:
        Fail("this board is unclaimed, and a revocation needs an owner to sign it.")
        Fail("  The board answers: revoke refused: board is unclaimed - use takeown first")
        Fail("  Claim it first: python tools/run_takeown.py")
        return False
    if not re.fullmatch(r"[0-9a-fA-F]{128}", root):
        Fail("the board did not answer getpubkey with a key: %s" % root)
        return False

    current_pub = iap_pubkey(st["iap"], args.current_key)
    if not current_pub:
        return False
    if current_pub != root:
        Fail("--current-key is not the key this board trusts.")
        Fail("  board trusts: %s" % root)
        Fail("  %s is:        %s" % (Path(args.current_key).name, current_pub))
        return False
    Ok("  claimed at generation %d, and --current-key is its root" % gen)

    Section("Precondition 2/4  issue THREE leaf certificates from the current root")
    for tag in ("doomed_leaf", "spared_leaf", "third_leaf"):
        pem = iap_genkey(st["iap"], st["keydir"], tag)
        if pem is None:
            return False
        pub = iap_pubkey(st["iap"], pem)
        if not pub:
            return False
        cert = Path(str(pem) + ".cert")
        if not iap_issue_cert(st["iap"], args.current_key, pub, cert):
            return False
        st[tag] = {"pem": pem, "pub": pub, "cert": cert}
        Ok("  %-12s %s  (names %s...)" % (tag, pem.name, pub[:2 * REVOKE_PREFIX_LEN]))
    if st["doomed_leaf"]["pub"][:2 * REVOKE_PREFIX_LEN] == \
            st["spared_leaf"]["pub"][:2 * REVOKE_PREFIX_LEN]:
        # Astronomically unlikely, and it would make T2-17 meaningless: the
        # record names leaves by their first REVOKE_PREFIX_LEN bytes only.
        Fail("the two leaves share their first %d bytes - rerun" % REVOKE_PREFIX_LEN)
        return False

    Section("Precondition 3/4  upload the application signed by the doomed leaf")
    rc, tool, serial = upload(st["iap"], st["bin"], st["ip"],
                              st["doomed_leaf"]["pem"], st["doomed_leaf"]["cert"],
                              st["ports"], args.tail_seconds)
    if rc != 0:
        Fail("the precondition upload failed (IAPTool exit %d)" % rc)
        return False
    if TOOL_CERT_ACCEPTED not in tool:
        Fail("IAPTool never said %r." % TOOL_CERT_ACCEPTED)
        Fail("  Without that line the app may be signed by the root itself rather")
        Fail("  than by a delegated leaf, which is not the precondition these cases need.")
        return False
    if LOG_IMAGE_ACCEPTED not in serial:
        Fail("the board did not say %r" % LOG_IMAGE_ACCEPTED)
        return False
    Ok("  the board accepted an image signed by the doomed leaf")

    Section("Precondition 4/4  the board actually runs it")
    log = reset_and_capture(st["cli"], st["ports"], args.seconds)
    if LOG_CAPTURE_PROOF not in log:
        Fail("no boot log captured (%d bytes) - check the log ports above" % len(log))
        return False
    if LOG_APP_STARTED not in log:
        Fail("the board did not start the application (no %r)" % LOG_APP_STARTED)
        return False
    Ok("  the board boots the leaf-signed application")

    st["baseline"] = app_digest(st["cli"], st["compare_bytes"], "baseline")
    if st["baseline"] is None:
        return False

    # Every case below talks to the bootloader, and the board is running the app.
    # The doomed leaf is still good at this point, which is what makes this work.
    Section("Precondition 4/4b  move the board into the bootloader")
    if not move_to_bootloader(st["iap"], st["ip"], args.port,
                              st["doomed_leaf"]["pem"], st["doomed_leaf"]["cert"],
                              st["ports"]):
        Fail("could not get the board into the bootloader")
        return False
    Ok("  the board is in the bootloader and answering")
    return True


# ------------------------------------------------------------------ cases
def case_t2_18(st, args):
    """A revocation with a bad signature does not get recorded.

    Runs first because it is the one case judged by nothing having changed. The
    board must be in exactly the state the precondition left it in.
    """
    Section("T2-18  a revocation with a bad signature is refused")
    before = board_generation(st["ip"], args.port)
    if before is None:
        Fail("could not read the generation before the attempt")
        return SETUP

    # The shipping tool cannot produce a bad signature -- it signs correctly or
    # not at all -- so this one goes over raw TCP, the same exception T2-04 makes.
    prefix = st["doomed_leaf"]["pub"][:2 * REVOKE_PREFIX_LEN]
    bogus = "11" * 64
    cmd = "revoke %s %s" % (prefix, bogus)
    print("  -> %s" % cmd)
    reply = (tcp_command(st["ip"], args.port, cmd) or "").strip()
    print("  <- %s" % reply)

    verdict = PASS
    if BOARD_REFUSED not in reply:
        Fail("  the board did not refuse: %r" % reply)
        verdict = FAIL
    else:
        Ok("  the board refused it")

    after = board_generation(st["ip"], args.port)
    if after is None:
        Fail("  could not read the generation back")
        return SETUP
    if after != before:
        Fail("  generation moved %d -> %d: something WAS recorded" % (before, after))
        verdict = FAIL
    else:
        Ok("  generation is still %d, nothing was recorded" % after)
    return verdict


def case_t2_15(st, args):
    """After revoking, the board still starts the app that leaf signed.

    Inverted 2026-09-22 with decision 60: revocation only blocks the next
    upload. What used to be this case's pass -- the board refusing the
    installed image -- is now its failure.
    """
    Section("T2-15  revoking leaves the installed app running (only future uploads are blocked)")
    before = board_generation(st["ip"], args.port)
    out, rc = run_capture([st["iap"], "revoke", st["ip"],
                           "--key=%s" % args.current_key,
                           "--leaf=%s" % st["doomed_leaf"]["pub"]])
    for line in nonblank_lines(out):
        print("    T | " + line)
    # ⚠️ IAPTool's exit code is NOT the criterion here, and must not be.
    # RunRevoke() reads getowner back and expects generation to have advanced,
    # but a revoke record never becomes the effective owner record, so it never
    # does -- the tool calls a successful revocation a failure. That is a defect
    # in the tool, tracked separately; what this case judges is the board.
    if rc != 0:
        Warn("  IAPTool exited %d - known defect in RunRevoke's read-back," % rc)
        Warn("  not evidence about the board. Judging the board instead.")
    after = board_generation(st["ip"], args.port)
    if after is None or before is None:
        Fail("  could not read the generation around the revocation")
        return SETUP
    # The effective generation is EXPECTED to stay put: the revoke record is
    # appended at generation+1 but never becomes effective.
    Ok("  revocation sent; effective generation %d (unchanged, as designed)" % after)

    log = reset_and_capture(st["cli"], st["ports"], args.seconds)
    if LOG_CAPTURE_PROOF not in log:
        Fail("  no boot log captured (%d bytes)" % len(log))
        return SETUP
    verdict = PASS
    if LOG_APP_REJECTED in log:
        Fail("  the board rejected the installed app (%r)." % LOG_APP_REJECTED)
        Fail("  That is the PRE-decision-60 behaviour -- is this bootloader current?")
        verdict = FAIL
    else:
        Ok("  the board did not reject the installed app")
    if LOG_APP_STARTED not in log:
        Fail("  but it did not start it either -- expected %r" % LOG_APP_STARTED)
        verdict = FAIL
    else:
        Ok("  and it started it: %r" % LOG_APP_STARTED)
    return verdict


def app_revoked_says(st, args, expect_revoked):
    """Ask the board whether its installed image's signer has been revoked.

    Returns PASS/FAIL. Both answers are checked by the same code on purpose:
    a board that always says REVOKED is as useless as one that never does.

    getapprevoked is a BOOTLOADER command, and every caller reaches this after
    a case that deliberately left the board running its application -- which is
    the whole point of T2-15. Without this step the command cannot reach the
    board at all, and the case reports a product failure having asked nothing.
    Self-signed, because the leaf under test is revoked by now.
    """
    if not move_to_bootloader_selfsigned(st["iap"], st["ip"], args.port,
                                         args.current_key, st["ports"]):
        Fail("  could not reach the bootloader; nothing was asked")
        return SETUP
    out, rc = run_capture([st["iap"], "getapprevoked", st["ip"]])
    for line in nonblank_lines(out):
        print("    T | " + line)
    if rc != 0:
        Fail("  IAPTool getapprevoked exited %d" % rc)
        return FAIL
    said_revoked = "REVOKED" in out
    if said_revoked != expect_revoked:
        Fail("  expected the board to report %s, it reported %s"
             % ("REVOKED" if expect_revoked else "not revoked",
                "REVOKED" if said_revoked else "not revoked"))
        return FAIL
    Ok("  the board reported %s, as expected"
       % ("REVOKED" if expect_revoked else "not revoked"))
    return PASS


def case_t2_26(st, args):
    """The board can still SAY the installed image's signer was revoked.

    T2-15 is what makes this case necessary: once a revoked leaf's firmware
    keeps running, this reply is the only thing in the field that distinguishes
    a board wanting a re-upload from one that does not.

    The negative half runs at the end of T2-17, where the board has just been
    given an image signed by a leaf nobody revoked.
    """
    Section("T2-26  the board reports that its app's signer was revoked")
    return app_revoked_says(st, args, True)


def case_t2_16(st, args):
    """After revoking, that leaf cannot upload either, and app flash is untouched."""
    Section("T2-16  the revoked leaf cannot upload either")
    before = app_digest(st["cli"], st["compare_bytes"], "t2_16_before")
    if before is None:
        return SETUP

    rc, tool, serial = upload(st["iap"], st["bin"], st["ip"],
                              st["doomed_leaf"]["pem"], st["doomed_leaf"]["cert"],
                              st["ports"], args.tail_seconds)
    verdict = PASS
    if rc == 0:
        Fail("  the upload SUCCEEDED (IAPTool exit 0) - the revocation did not hold")
        verdict = FAIL
    else:
        Ok("  the upload failed, as it must (IAPTool exit %d)" % rc)

    after = app_digest(st["cli"], st["compare_bytes"], "t2_16_after")
    if after is None:
        return SETUP
    if after != before:
        Fail("  the application region CHANGED during the refused attempt")
        Fail("    before %s" % before)
        Fail("    after  %s" % after)
        verdict = FAIL
    else:
        Ok("  the application region is byte-for-byte unchanged")
    return verdict


def case_t2_17(st, args):
    """A different leaf of the same root still uploads and boots.

    The control. Runs even when T2-15 or T2-16 failed, because without it a
    refusal above cannot be told apart from a board that accepts nothing -- and
    it is also what leaves the board with a runnable application.
    """
    Section("T2-17  a leaf that was NOT revoked still works (control)")
    rc, tool, serial = upload(st["iap"], st["bin"], st["ip"],
                              st["spared_leaf"]["pem"], st["spared_leaf"]["cert"],
                              st["ports"], args.tail_seconds)
    verdict = PASS
    if rc != 0:
        Fail("  the upload failed (IAPTool exit %d) - revocation hit the wrong leaf," % rc)
        Fail("  or the board stopped accepting uploads for some unrelated reason")
        verdict = FAIL
    elif LOG_IMAGE_ACCEPTED not in serial:
        Fail("  the board did not say %r" % LOG_IMAGE_ACCEPTED)
        verdict = FAIL
    else:
        Ok("  the board accepted an image signed by the spared leaf")

    log = reset_and_capture(st["cli"], st["ports"], args.seconds)
    if LOG_CAPTURE_PROOF not in log:
        Fail("  no boot log captured (%d bytes)" % len(log))
        return SETUP
    if LOG_APP_STARTED not in log:
        Fail("  the board did not start it (no %r)" % LOG_APP_STARTED)
        verdict = FAIL
    else:
        Ok("  and it boots")

    # T2-26's negative half: the image now installed was signed by the spared
    # leaf, so the board must stop reporting itself as wanting a re-upload.
    # Judged here because this is the only point in the run where a KNOWN-GOOD
    # image is installed.
    if verdict == PASS:
        Section("T2-26 (negative half)  and it stops saying so once a good image is installed")
        if app_revoked_says(st, args, False) != PASS:
            verdict = FAIL
    return verdict


def case_t2_19_20(st, args):
    """Revoke a SECOND, different leaf -- and prove a repeat revoke is idempotent.

    This is the case whose absence let a real defect through: T2-15 to T2-18 all
    passed while a board could only ever be revoked once, because not one of them
    revoked twice. See OWN-06 in $PROD/maps/owner-revoke-and-boot-upgrade/.

    Runs last: it needs the board in the bootloader and it spends owner slots.
    """
    Section("T2-19  a second, different leaf can also be revoked")
    if not move_to_bootloader_selfsigned(st["iap"], st["ip"], args.port,
                                         args.current_key, st["ports"]):
        Fail("  could not get the board into the bootloader")
        return SETUP

    before_log = reset_and_capture(st["cli"], st["ports"], args.seconds)
    revoked_before = board_revoked_count_from(before_log)
    if revoked_before is None:
        Fail("  could not read the revoked count before the second revocation")
        return SETUP
    if not move_to_bootloader_selfsigned(st["iap"], st["ip"], args.port,
                                         args.current_key, st["ports"]):
        Fail("  could not get the board back into the bootloader")
        return SETUP

    out, rc = run_capture([st["iap"], "revoke", st["ip"],
                           "--key=%s" % args.current_key,
                           "--leaf=%s" % st["spared_leaf"]["pub"]])
    for line in nonblank_lines(out):
        print("    T | " + line)
    verdict = PASS
    if rc != 0:
        Fail("  revoking the second leaf failed (IAPTool exit %d)" % rc)
        verdict = FAIL

    log = reset_and_capture(st["cli"], st["ports"], args.seconds)
    if LOG_CAPTURE_PROOF not in log:
        Fail("  no boot log captured")
        return SETUP
    revoked_after = board_revoked_count_from(log)
    if revoked_after is None:
        Fail("  could not read the revoked count back")
        return SETUP
    if revoked_after != revoked_before + 1:
        Fail("  revoked count went %d -> %d, expected +1: the second one did not take"
             % (revoked_before, revoked_after))
        verdict = FAIL
    else:
        Ok("  revoked count %d -> %d, the second leaf took effect"
           % (revoked_before, revoked_after))

    Section("T2-20  revoking the same leaf again is idempotent")
    # The reset above left the board in the bootloader: the running app was
    # signed by spared_leaf, which is now revoked, so it no longer starts.
    free_mid = board_free_slots_from(log)
    out, rc = run_capture([st["iap"], "revoke", st["ip"],
                           "--key=%s" % args.current_key,
                           "--leaf=%s" % st["spared_leaf"]["pub"]])
    for line in nonblank_lines(out):
        print("    T | " + line)
    if rc != 0:
        Fail("  a repeat revoke should succeed, got exit %d" % rc)
        verdict = FAIL
    elif "already revoked" not in out:
        Fail("  the tool did not report that the leaf was already revoked")
        verdict = FAIL
    else:
        Ok("  the board answered 'already revoked'")

    free_after = board_free_slots(st["ip"], args.port, st["cli"], st["ports"], args.seconds)
    if (free_mid is None) or (free_after is None):
        Fail("  could not read the free-slot count around the repeat")
        return SETUP
    if free_after != free_mid:
        Fail("  a repeat revoke spent a slot: %d -> %d" % (free_mid, free_after))
        verdict = FAIL
    else:
        Ok("  no slot was spent (%d free, unchanged)" % free_after)
    return verdict



# -------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--bin", required=True,
                    help="the application image to upload (a real .bin, not a stand-in)")
    ap.add_argument("--current-key", required=True,
                    help="private half of the root this board trusts right now")
    ap.add_argument("--ip", default="")
    ap.add_argument("--port", default="56865")
    ap.add_argument("--stop-after", choices=["T2-18", "T2-15", "T2-26", "T2-16", "T2-17"],
                    default="T2-17",
                    help="stopping before T2-17 leaves the board without a runnable app")
    ap.add_argument("--second-leaf", action="store_true",
                    help="also run T2-19/T2-20: revoke a SECOND leaf, then repeat it; spends two more owner slots and leaves the board without a runnable app")
    ap.add_argument("--compare-bytes", default="0",
                    help="bytes of the application region to read back; 0 = all of it")
    ap.add_argument("--seconds", type=int, default=10, help="boot log capture window")
    ap.add_argument("--tail-seconds", type=int, default=15,
                    help="how long to keep listening after IAPTool exits")
    ap.add_argument("--keydir", default="",
                    help="where the generated keys and certificates go")
    ap.add_argument("--ports", nargs="*", default=None)
    args = ap.parse_args()

    ip = args.ip or getattr(cfg, "BOARD_IP", "")
    if not ip:
        Fail("need --ip (or set BOARD_IP in config/machine.py)")
        return 2
    image = Path(args.bin)
    if not image.exists():
        Fail("no such image: %s" % image)
        return 2
    current_key = Path(args.current_key)
    if not current_key.exists():
        Fail("no such key: %s" % current_key)
        return 2
    args.current_key = current_key

    try:
        iap = get_iap_tool()
    except SystemExit:
        # get_iap_tool exits 1 when the board package holds no IAPTool. That is a
        # setup problem, and this script's exit codes say so with 2, not 1.
        iap = get_go_bin("IAPTool")
        if not iap.exists():
            Fail("no IAPTool: not in the board package, and not built in Output/")
            return 2
        Warn("using the locally built IAPTool, not the one in the board package")

    compare = int(str(args.compare_bytes), 0) or APP_MAX_SIZE
    if compare > APP_MAX_SIZE:
        Fail("--compare-bytes is larger than the application region (%d)" % APP_MAX_SIZE)
        return 2

    keydir = (Path(args.keydir) if args.keydir
              else get_scratch_dir() / ("revoke-leaf-" + uuid.uuid4().hex[:8]))
    keydir.mkdir(parents=True, exist_ok=True)

    cli = get_programmer_cli()
    st = {"ip": ip, "bin": image, "iap": iap, "cli": cli, "keydir": keydir,
          "ports": args.ports if args.ports else cfg.LOG_PORTS,
          "compare_bytes": compare}

    Section("Setup")
    print("  board:   %s" % ip)
    print("  image:   %s  (%s B)" % (image, format(image.stat().st_size, ",d")))
    print("  IAPTool: %s" % iap)
    print("  key dir: %s" % keydir)
    Warn("  a full run spends one owner record slot, and those do not come back")
    Warn("  without erasing the bootloader.")

    Section("Target check")
    assert_target_reachable(cli)

    if not build_precondition(st, args):
        Section("Verdict")
        Warn("SETUP - the precondition was never reached, so nothing was tested.")
        print("  keys and certificates: %s" % keydir)
        return 2

    order = ["T2-18", "T2-15", "T2-26", "T2-16", "T2-17"]
    stop_at = order.index(args.stop_after)
    runner = {"T2-18": case_t2_18, "T2-15": case_t2_15,
              "T2-26": case_t2_26, "T2-16": case_t2_16, "T2-17": case_t2_17}
    results = []
    for i, name in enumerate(order):
        if i > stop_at:
            break
        if results and results[-1][1] == SETUP:
            break
        results.append((name, runner[name](st, args)))
    # The control runs even when something above failed: it is what tells a
    # correct refusal apart from a board that accepts nothing, and it is what
    # leaves the board usable.
    ran = dict(results)
    if args.stop_after == "T2-17" and "T2-17" not in ran and \
            (not results or results[-1][1] != SETUP):
        ran["T2-17"] = case_t2_17(st, args)

    if args.second_leaf and (ran.get("T2-17") == PASS):
        ran["T2-19/T2-20"] = case_t2_19_20(st, args)
        order = order + ["T2-19/T2-20"]
    elif args.second_leaf:
        Warn("T2-19/T2-20 skipped: the control T2-17 did not pass first")

    Section("Verdict")
    for name in order:
        if name in ran:
            {PASS: Ok, FAIL: Fail, SETUP: Warn}[ran[name]]("  %-7s %s" % (name, ran[name]))
        else:
            Warn("  %-7s NOT RUN" % name)

    if any(s == FAIL for s in ran.values()):
        if ran.get("T2-17") != PASS:
            Warn("  T2-17 did not pass either, so a refusal above may only mean this")
            Warn("  board accepts nothing. Fix the control before reading the rest.")
        Fail("FAIL - at least one criterion did not hold.")
        rc = 1
    elif any(s == SETUP for s in ran.values()) or len(ran) < len(order):
        Warn("INCONCLUSIVE - a case could not be judged or was not run.")
        rc = 2
    else:
        Ok("PASS - one leaf was revoked by name, and the others still work.")
        rc = 0

    if ran.get("T2-17") != PASS:
        Warn("  The board may have no runnable application. See RECOVERY at the top")
        Warn("  of this file.")
    print("")
    print("  keys and certificates: %s" % keydir)
    return rc


if __name__ == "__main__":
    sys.exit(main())
