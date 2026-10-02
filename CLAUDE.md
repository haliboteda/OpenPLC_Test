# 开工入口 —— OpenPLC_Test

**这份文件是给 AI 会话看的。**

这个仓库装 OpenPLC 的**契约测试和整机测试**（决策 78）：跑它要用到两个以上仓里的东西的测试。只测一个部件自己逻辑的测试住在那个部件的仓。

> **产品文档在 `OpenPLC_Docs`**（`$PROD`）。用例的判据、怎么跑在 `$PROD/docs/engineering/HOW-TO-RUN-TESTS.md`；测试为什么这么分在 `$PROD/maps/test-architecture/map.md`。

## 这个仓库自己的东西

| 在哪 | 是什么 |
|---|---|
| 根目录的 `.go` | `TestCase.exe`：对着真板子测 bootloader 的网口协议、签名、认证。import IAPTool 仓的 `iapcert` / `iapproto` 并调 IAPTool.exe |
| `tools/check_version_sync.py`、`check_mirror_sync.py`、`check_tool_sync.py`、`check_golden_vectors.py` | 契约测试 P1、P2、P11、黄金向量：几个仓对同一份格式、常量、版本理解一致 |
| `host/bootstand/` | bootloader 替身：`$BOOT` 的真 bootloader 代码和板卡包 app 一侧的重启握手，编成 PC 程序（`bootstand_boot` / `bootstand_app`），由 `bootstand.py` 按复位轮流启动。设计见 `$PROD/docs/engineering/BOOTLOADER-STAND-IN.md` |
| `host/fakeboard/` | T1-18a–g（IAPTool 上传前的密钥/证书核对）、T1-34（IDE 那条上传命令），都对着上面的替身跑。替身用端口 61865，不用产品端口 56865 |
| `host/renode/` | T3-05：例程在 Renode 里经真 bootloader 启动 |
| `tools/run_*.py`、`onboard/` | 上板用例的驱动和板上 sketch |
| `tools/build_image.py`、`flash_bootloader.py`、`enter_bootloader.py`、`reset_board_to_factory_state.py`、`serial_watch.py` 等 | 上板用的基础设施：命令行编 bootloader、ST-Link 烧录、摆板子状态、抓串口 |
| `tools/common.py`、`platform_info.py`、`init_machine.py` | 平台兼容层和本机路径生成器 |
| `config/machine.py` | **本机路径的唯一出处**，gitignored，由 `tools/init_machine.py` 生成 |
| `Output/` | gitignored：测试密钥、探针镜像、运行记录。⚠️ `Output/five-paths-keys/owner_after_claim/owner_after_claim.pem` 是目前唯一能给实验板签固件的密钥 |

## 依赖并排的哪些仓

| 仓 | 拿来干什么 |
|---|---|
| `IAPTranfer_Tool`（`IAPTOOL_REPO`） | 被测的 IAPTool：Go 包经 `go.mod` 的 `replace` 引用，可执行文件从它的 `Output/` 或板卡包里找 |
| `open_plc_cube_ide`（`BOOT_REPO`） | bootloader 源码、`golden_vectors.h`、命令行编 bootloader |
| `open_plc_arduino`（`CORE_REPO`）和已装的板卡包（`CORE_LIVE`） | 契约比对、编上板 sketch |
| `OpenPLC_PortsTestingTool`（`PORTTOOL_REPO`，可选） | 只给 P2 的校准值区三方比对用 |

## 开工前

```
python tools/init_machine.py       # 第一次：探测本机路径，写 config/machine.py
python tools/selfcheck.py          # 本仓所有不需要板子的检查；--quick 跳过慢的两项
python tools/selfcheck.py --list   # 每一步是什么、证明哪条需求
```

## 脚本的平台规矩

**脚本一律 Python**，且要求**同时能在 Windows 和 Linux 上跑**。`tools/common.py` 是平台兼容层，写新脚本时：

| 要做的事 | 用这个 | 不要用 | 为什么 |
|---|---|---|---|
| 判断平台 | `PLATFORM` | 直接读 `os.name` | 三个平台三套叫法，集中在一处映射 |
| 拼可执行文件名 | `EXE`、`get_go_bin()`、`get_iap_tool()`、`get_programmer_cli()`、`get_cube_ide_exe()` | 硬写 `.exe` | — |
| 临时文件 | `get_scratch_file()` / `get_scratch_dir()` | 自己拼 `$TEMP` | 平台之间那个变量不一致，拼错会往文件系统根目录写 |
| 相对路径 | `/` 分隔 | `\` | Windows 接受 `/`，Linux 不接受 `\` |
| Go 输出目录 | `GOOS_DIR` | 硬写 `windows` | — |
| 板卡包平台目录 | `A15_DIR`（`win`/`linux`/`macosx`） | 硬写 `win` | — |
| CubeIDE 插件后缀 | `CUBE_PLUG`（`win32`/`linux64`/`macos64`） | 硬写 `win32` | — |

**遇到一个新的、只有本机知道的路径** —— 加进 `tools/init_machine.py` 的 `SETTINGS` 表（连同探测方式和一段说明），不要硬编码，也不要猜。加进去它就会在每台机器上被自动找出来（理由见 `open_plc_cube_ide/CLAUDE.md` 第七节）。

> ⚠️ 双平台能力**只在 Windows 上验证过**。Linux 侧是逐条消除平台依赖做的，**没有真机验证**。

**机器相关的路径一律进 `$TEST/config/machine.py`，而那份是 `tools/init_machine.py` 生成的** —— 不手写，也没有模板可抄。

⚠️ **判断一个值该不该进配置：另一台同样系统的机器会不会有不同的值？** 不会就不属于那里 —— 那是平台派生量，归 `common.py`。

⚠️ **新的、只有本机知道的路径不许硬编码，也不许猜。** 该往哪儿加、为什么，在 `$TEST/tools/init_machine.py`。

> **2026-08-20 删掉了 `machine.example.py`。** 它们和 `SETTINGS` 表是同一份清单的两个出处。而且模板那套"两个平台的值都给、删掉不用的那套"的用法，**忘记删是最常见的错误** —— 第一次在 Debian 上就踩了，表现是一堆互不相关的 MISSING，加上一句让人去查串口线的错误建议。

⚠️ **这条不止管脚本内部，还管 AI 会话怎么打命令。** 从一个仓库的会话里调另一个仓库的脚本（例如在 `open_plc_cube_ide` 里跑 `OpenPLC_Test/tools/selfcheck.py`），**用相对路径 `../OpenPLC_Test`，不要绝对路径**。第三节那张兄弟目录表已经把布局钉死了，相对路径换机器天然成立；绝对路径（`E:\WorkSpace\...`）只在这台机器上对，写进 `.claude/settings.local.json` 的 allow 列表里，换机器就是一条永远不会再命中的死记录。

**2026-08-23 实测：allow 列表不支持在字符串中间用 `*` 匹配任意前缀**，只有结尾通配符是文档确认支持的（`Bash(git *)` 这种）。所以"允许这条命令、不管前面的绝对路径是什么"这件事做不到，唯一可移植的办法就是从一开始就不在命令里写绝对路径。

⚠️ **提交进 `.claude/settings.json` 的 allow 规则只放不碰硬件、不改产品文件的命令**（读文件、跑静态检查、编译到本地产物）。**给板子刷固件、生成/替换密钥、往真实设备发升级命令**——这些哪怕本人已经批准过一百次，也不进那份**团队共享**的文件，因为一进去就是给每个 clone 这仓库的人默认放行，没人再会被问一句。这类命令要保留就放个人的 `.claude/settings.local.json`（本机专属、不进 git）。2026-08-23 把 `IAPTool.exe cdc/ether/genkey/sign` 和 `flash_bootloader.py` / `run_*.py` 这批从提交文件里挪回本地文件时发现的。

### 脚本和工具不许放临时目录

**任何要用第二次的东西，都直接写进仓库里的固定位置**，不要放 `%TEMP%` / scratchpad。

放临时目录的东西**不进 git**，下次就找不到了，于是同一个脚本被重写一遍。

| 东西 | 去哪 |
|---|---|
| 测试脚本、自动化工具（烧写、抓串口……） | `$TEST/tools/` |
| 一次性的探查命令（`grep` 一下、看个尺寸） | 不落盘，直接跑 |

> 2026-08-16 犯过：把自动烧写脚本写进 scratchpad，还硬编码了 `D:\ST\STM32CubeIDE_1.10.0` 和工作区绝对路径 —— 换电脑双重报废。**机器相关的路径一律进 `config/machine.py`。**

## 边界

**用例的目的是验证出货的那套工具和软件好不好用**（用户 2026-09-03 原话）。客户的入口是 **Arduino IDE 的菜单**，IDE 按 `platform.txt` 的配方调**板卡包里的那份 IAPTool**。用例要从那一层进去，不在用例里另写一份实现 —— 那样测的是用例自己。判据反过来：结果问板子要，不问工具要。

四条原则：

1. 测真实代码路径
2. 加密逻辑 import，不重写
3. 反向用例和正向用例一样重要
4. 破坏性用例标 `destructive`
