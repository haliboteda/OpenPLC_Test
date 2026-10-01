# OpenPLC_Test

Contract and system tests for OpenPLC: the tests that need more than one
repository to run. Each component (bootloader, Arduino board package, IAPTool,
PortTool) tests its own logic in its own repository; this one checks that they
agree with each other and that the user's paths work on a real board.

中文：[README.zh-CN.md](README.zh-CN.md)

## What is here

- `*.go` (repo root): `TestCase`, which talks to a real board's bootloader over
  the network. It imports IAPTool's `iapcert` / `iapproto` packages.
- `tools/check_*.py`: contract checks -- version numbers, mirrored code, the
  packaged IAPTool, the golden test vectors.
- `host/`: checks that need no board (fake board for IAPTool, Renode).
- `tools/run_*.py`, `onboard/`: on-board cases and the sketches they flash.

## Requirements

Go 1.23+, Python 3, and these repositories cloned beside this one:
`IAPTranfer_Tool`, `open_plc_cube_ide`, `open_plc_arduino`
(`OpenPLC_PortsTestingTool` optional). On-board cases also need STM32CubeIDE
(for STM32_Programmer_CLI) and the Arduino IDE.

## Running

```
python tools/init_machine.py   # once: detects this machine's paths
python tools/selfcheck.py      # every check that needs no board
go build -o Output/<os>/TestCase .
```
