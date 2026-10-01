# fakeboard — IAPTool's pre-transfer decisions, without a board

`fake_board.py` is the stand-in device. `run_cases.py` drives the real
`IAPTool.exe` against it, covering the decision the tool makes *before* any
firmware moves:

| Suite | Case | Covers | What it checks |
|---|---|---|---|
| `run_cases.py` | T1-18a–T1-18g | R1-21 | which key and certificate this board will accept |

---

# T1-18a–T1-18g · the key-match decision

## What this covers

Before IAPTool sends a single byte of firmware it asks the device `getpubkey`
and decides whether the key it would sign with is one the device will accept.
That decision has seven outcomes. All seven are checked here.

| Case | Board answers `getpubkey` | Host has | Expected |
|---|---|---|---|
| `key-match` | the key IAPTool signs with | private key, no certificate | `Signing key matches this board` |
| `key-mismatch` | a different key | private key, no certificate | refuses: `verifies against a different signing key` |
| `old-bootload` | `Unknown command` | private key, no certificate | proceeds: `skipping key match check` |
| `cert-match` | the root that issued the certificate | leaf key + its certificate | `Certificate was issued by this board's root` |
| `cert-wrong-root` | a different key | leaf key + its certificate | refuses: `was not issued by this board's root` |
| `cert-key-mismatch` | the issuing root | a certificate covering somebody else's key | refuses: `was issued for a different key` |
| `no-key` | any | neither | refuses: `no signing key found` |

The last four are the delegated-leaf story: an administrator holds the root and
issues certificates for colleagues' keys, so a colleague can upload without the
root private key ever being on their machine. The first three are the same
board seen by whoever holds the root itself, whose certificate is self-signed.

Both go through one check -- does the root this board trusts vouch for this
certificate -- because for a self-signed certificate that question *is* "is
this my key". No branch, no second code path.

## Why it is not a hardware test

Each row differs only in which key the *bootloader was compiled with*. On real
hardware, moving between rows means rebuilding and reflashing the bootloader
with a different key — one ST-Link round per case to check one branch of host-side
logic. Here it is a command-line argument.

`fake_board.py` verifies nothing at all. It answers protocol commands with fixed
strings. **What is under test is IAPTool**; the device's own signature checking
is case T1-11, against real hardware.

## Running

```
python run_cases.py              # all seven
python run_cases.py --keep       # keep the scratch directory to inspect logs
```

Needs `python` and `go` on PATH. Builds `IAPTool.exe` if it is missing.
Also run as steps K1-K7 of `tools/selfcheck.py`.

## Two things the runner has to do that are not obvious

**It runs a copy of IAPTool from a scratch directory.** IAPTool resolves
`local_config.json` and its fallback `keys/` directory relative to *its own
executable*, not the working directory. So the three "the host has no private
key" cases cannot be produced by simply omitting `--key` — the tool falls back
to the `signing_key` in the repo's config and signs anyway. The first version of
this script did exactly that and reported three passes that tested nothing.

**The scratch `local_config.json` is written without a BOM.** A BOM-writing editor's
`Set-Content -Encoding utf8` adds one, Go's `json.Unmarshal` rejects it, and
IAPTool exits before doing anything — which reads as a broken tool rather than a
broken config.

## Keys

Every key is generated per run by `IAPTool genkey` in the scratch directory,
so nothing needs committing and openssl is not required. The tool runs with its
user config dir inside scratch too, so it never reads or writes the real
user's key at the default location.

---
