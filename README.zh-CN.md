# OpenPLC_Test

OpenPLC 的契约测试和整机测试：要用到两个以上仓库才能跑的测试。每个部件（bootloader、
Arduino 板卡包、IAPTool、PortTool）在自己的仓里测自己的逻辑；这个仓检查它们彼此对得上，
以及用户的使用路径在真板子上走得通。

English: [README.md](README.md)

## 里面有什么

- 根目录的 `*.go`：`TestCase`，通过网口对着真板子的 bootloader 测。它 import IAPTool 的
  `iapcert` / `iapproto` 包。
- `tools/check_*.py`：契约检查 —— 版本号、跨仓镜像代码、板卡包里的 IAPTool、黄金测试向量。
- `host/`：不需要板子的检查：bootloader 替身（真 bootloader 代码编成的 PC 程序，IAPTool 的上传用例对着它跑），以及 Renode。
- `tools/run_*.py`、`onboard/`：上板用例和它们烧进板子的 sketch。

## 需要什么

Go 1.23 以上、Python 3，以及克隆在本仓旁边的这几个仓库：`IAPTranfer_Tool`、
`open_plc_cube_ide`、`open_plc_arduino`（`OpenPLC_PortsTestingTool` 可选）。上板用例还要
STM32CubeIDE（用它带的 STM32_Programmer_CLI）和 Arduino IDE。

## 怎么跑

```
python tools/init_machine.py   # 第一次：探测本机路径
python tools/selfcheck.py      # 所有不需要板子的检查
go build -o Output/<os>/TestCase .
```
