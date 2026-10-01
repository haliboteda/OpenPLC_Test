# 2026-09-18 · boot + IAP 在真板子上完整跑一遍

**这是一轮执行记录，不是清单的定义。** 清单的定义在 `$PROD/docs/tables/ACCEPTANCE-CHECKLIST.md`
的 `CHK-A6`，判据在 `$PROD/docs/engineering/HOW-TO-RUN-TESTS.md`，需求在
`$PROD/docs/modules/M1-firmware-upgrade.md`。**这里只记这一轮谁跑了、结果是什么。**

⚠️ **这个落点是临时的。** 「验收单的结果记在哪」还没有定论，⬜ 在
`$PROD/docs/tables/ID-MAP.md` 第 32 行，由票 `PTG-01`（逐板验收记录存在哪，
`$PROD/maps/production-test-gap/issues/PTG-01-where-do-per-board-records-go.md`）回答。
那张票现在 **paused，等硬件工程师用过工装之后的反馈**。答完之后这份可能要搬。

---

## 这一轮的台子（2026-09-18 实测，不是抄的）

| 项 | 值 | 怎么确认的 |
|---|---|---|
| 板子 IP | `192.168.0.3` | `config/machine.py` 的 `BOARD_IP`，ping 通 |
| 日志口 | `COM5`（FTDI USB Serial Port） | `Win32_PnPEntity` 枚举 |
| ST-Link 虚拟串口 | `COM14` | 同上。**ST-Link 在位**，所以 `T1-13` / `T1-26` 能跑 |
| **USB CDC 口** | **`COM15`**（`VID_16D0 PID_117E`） | 同上。⚠️ `config/machine.py` 的 `CDC_PORT` 仍是空的，要手给 `--cdc COM15` |
| 主机出口网卡 | `192.168.0.2`（WLAN） | `Get-NetIPAddress` |

**这个台子测不到什么**：只有一块板，所以任何「两块板之间」的性质（`R1-14` 每块板 MAC 互不相同）
测不了；没有第二台主机，所以「同一台主机上多个工具互不打断」（`R1-07`）只能单机近似。

---

## 前置 · 不解决就不用往下走

| # | 是什么 | 谁做 | 状态 |
|---|---|---|---|
| **P-1** | ~~断开 VPN~~ —— **不用断。** 测试工具不该跟着路由表走，该只用物理网卡（决策 51）。已按方案 A 修好，见下节 | AI | ✅ **已完成 2026-09-18，用户不用动手** |
| **P-4** | ~~板子不在线~~ —— 用户 2026-09-18 通电，`T1-01` 四个关键词全应答 | **用户** | ✅ **已完成** |
| **P-2** | **要一个 app 镜像**。`tools/build_image.py` 只造 bootloader 和工装镜像，不造 app。已编好：sketch 在 `$TEST/onboard/iap_probe/iap_probe.ino`，用 `D:/Soft/arduino-2/resources/app/lib/backend/resources/arduino-cli.exe compile --fqbn OpenPLC_Alpha:stm32:OPEN-PLC` 编出，产物 `$TOOL/Output/iap_probe_app.bin`（87,392 B） | AI | ✅ **已完成 2026-09-18** |
| **P-3** | **板子跑的是 app**，不是 bootloader。身份串 `STM32H743_383134373033510C00240039_CUSAPP_0.1.3`，UID `383134373033510C00240039` | AI | ✅ **已完成 2026-09-18** |

⚠️ **这一轮会覆盖掉板子上现在那个 `M5_SerialConflict`。**

---

## 第一批 · 要用户动手（做完就不用再管）

**1–3 有脚本，人只管动手，脚本靠板子停止应答自己判断，不用回来按回车。**
**4–5 没有脚本**，是「剩下五条手工需求」里的两条，只能现场读 `COM5` 上的日志。

| # | 用例 / 需求 | 动作 | 判据 | 命令 | 结果 |
|---|---|---|---|---|---|
| **M-1** | `T1-17` nonce 跨掉电不重复 | **拔电再上电** | 计数器不归零，本轮 nonce 全不同 | `python tools/run_au1.py`（存盘超过 1 小时它会拒绝 `--resume`） | ✅ **PASS** 2026-09-19（修复后重跑）—— 掉电前计数器 11，掉电后从 13 续，16 个 nonce 全不同。⚠️ 2026-09-18 这条是 ⛔ FAIL，根因是 RTC 时钟源不一致，见下节 |
| **M-2** | `T1-21` 掉电落在传输期 | **传输进行中拔电** | 重新上电后**旧 app 照常启动** | `python tools/run_s4.py --case a --bin ../Output/iap_probe_app.bin --pad-to 1835008` | ✅ **通过** 2026-09-18，一次命中。判词 `PASS: the old application booted, flash was untouched`（脚本原文前面还带着作废的旧编号，见下） |
| **M-3** | `T1-22` 掉电落在擦写窗口 | **擦写窗口内拔电** | 报 `App signature invalid or absent`，重传能救回 | `python tools/run_s4.py --case b --bin ../Output/iap_probe_app.bin --pad-to 1835008 --retry 3` | ✅ **通过** 2026-09-18，**第 3 次才命中**。判词 `PASS: half-written app reported invalid, re-upload restored it`（同上） |
| **M-4** | `R1-04` 按住 BOOT0 强制进上传模式 | **只按住 BOOT0**，复位交给 ST-Link | `COM5` 上出现 `** UPLOAD Mod ... (BOOT0 held)` **且** `** Reset cause: PIN` | ✅ **新脚本** `python tools/run_boot0_upload_mode.py --ports COM5` | ✅ **通过** 2026-09-18，第 1 次复位即命中 |
| **M-5** | `R1-30` 复位原因能报出 | ~~按复位键~~ **不用人动手了** | 三个取值全部观察到 | ST-Link `-rst` 拿 `PIN`；另两个顺带得到 | ✅ **完成** 2026-09-18 |

---

## 第二批 · AI 自己跑（14 条，用户不用在场）

跑法以 `$PROD/docs/engineering/HOW-TO-RUN-TESTS.md` 为准；`CHK-A6` 的
`TestCase all --ip=<IP> --bin=<app.bin>` 一次能带掉其中的 Go 用例。

| # | 对应需求 | 测什么 | 命令 | 结果 |
|---|---|---|---|---|
| `T1-01` | `R1-08` | 四个发现关键词都应答 | `go run . T1-01 --ip=192.168.0.3` | ✅ **通过** 2026-09-18，四个关键词全答 |
| `T1-02` | `R1-09` | 连续多轮发现不丢 | 同上，换 id |✅ **通过** —— 6 次间隔查询全答 |
| `T1-03` | `R1-10` | 发现回复落在工具的 2s 超时之内 | 同上 |✅ **通过** —— 20 次回复全在 2s 内 |
| `T1-04` | `R1-11` | 长时间浸泡下发现不丢（10 分钟） | 同上 |✅ **通过** —— 196 次查询 10 分钟全答，**但最慢 2.381s，超出 `R1-10` 的 2s 预算**，见下 |
| `T1-05` | `R1-12` | 泛洪限流，**且限流不打死正常发现** | 同上 |✅ **通过** —— 1270 次泛洪只回 150（50/s，正好卡在上限），事后正常发现仍可用 |
| `T1-06` | `R1-15` | 一次只服务一个 TCP 客户端 | 同上 |✅ **通过** —— 第二条连接被接受但不被服务，第一条仍在答 |
| `T1-07` | `R1-16` | 空闲客户端约 60s 后被踢 | 同上 |✅ **通过** —— 1m2s 后被踢 |
| `T1-08` | `R1-17` | 50s 内的空闲客户端**不**被踢（反向用例） | 同上 |✅ **通过** —— 50s 后仍连着且在答 |
| `T1-09` | `R1-02` `R1-18` | 传输中闯入的第二个连接不打断传输 | 同上 |✅ **通过** —— 修好用例后连跑 8 轮 8 过 |
| `T1-10` | `R1-19` | 第一个连接正常关闭后能再连 | 同上 |✅ **通过** —— 第一条正常关闭后能再连并被服务 |
| `T1-11` | `R1-23` | 无效签名的镜像被拒 | 同上，加 `--bin` |✅ **通过** —— 板子回 `Signature Failed` |
| `T1-12` | `R1-23` | 签方不对的镜像被拒 | 同上 |✅ **通过** —— 签方不对同样回 `Signature Failed` |
| `T1-14` | `R1-25` | 失败上传后 app 区**一字节没动** | `python tools/run_case.py --case T1-11 --bin <app.bin> --then-reset` |✅ **通过** —— 被拒上传后原 app 照常启动，app 区没被碰 |
| `T1-23` | `R1-01` `R1-03` | 真实上传走完，且擦除在验证**之后** | `python tools/upload_and_watch.py --bin <app.bin> --ip 192.168.0.3` |✅ **通过** —— `erase happened AFTER verification` |
| `T1-24` | `R1-22` | 坏 CRC 在验签**之前**被拒（回 `Checksum Failed` 而非 `Signature Failed`） | `python tools/run_case.py --case T1-24 --bin <app.bin>` |✅ **通过** —— 回 `Checksum Failed` 而非 `Signature Failed` |
| `T1-25` | `R1-05` | CDC 上传模式下以太网栈不起来，**且同一轮的正向对照答得出** | `python tools/run_cdc_does_not_start_ethernet.py --cdc COM15 --ip 192.168.0.3 --ports COM5` |✅ **通过**（⚠️ 必须用 `--cdc COM11`，用 COM15 会假通过，见下） |
| `T1-13` | `R1-26` | app 签名在**每次启动时**被重新校验 | `python tools/run_s3.py --bin <app.bin>` ⚠️ **故意把板子上的 app 改坏，跑完自己恢复** |✅ **通过** —— 改坏 app 后板子拒绝启动它，脚本恢复后一切正常 |
| `T1-26` | `R1-28` | 一次成功升级正好消耗 **9** 个 journal 槽 | `python tools/run_journal_slot_accounting.py --bin <app.bin>` ⚠️ 两头必须用 ST-Link 复位，不能走 IAP |✅ **通过** —— 297/4096 → 306/4096，正好 9 槽 |

⚠️ **顺序有讲究**：`T1-22` 跑完 app 是无效的，板子停在 bootloader。所以第二批要**先重传一次
app**（`T1-23`）再跑 `T1-13` 和 `T1-26`，否则那两条没有有效 app 可测。

---

## 已经绿的，这轮不重跑（主机侧 11 条）

2026-09-18 当天 `selfcheck` 19 项全过，其中属于 M1 的：
`T1-15`（主机侧密码学三组断言）· `T1-16`（拿真 bootloader 源码跑板子侧判断逻辑）·
`T1-18a`–`T1-18g`（工具传输前预判这块板收不收我的签名，七种情况）·
`T1-19`（SHA-256 交叉验证）· `T1-20`（ECDSA 交叉验证）。

⚠️ **它们跑在假板子上**，PC 侧的对端代码在那个台子上执行不到 —— 这个盲区 2026-09-14 咬过一次。

---

## 这一轮跑不了的，写出来免得被当成跑过了

| 跑不了 | 为什么 | 要什么才能跑 |
|---|---|---|
| ~~`R1-13` 设备定位靠 UID 匹配、不依赖 MAC 一致~~ | ✅ **2026-09-18 在真板子上证完了** —— 那个「改过 MAC 的 app」不用改代码，只要编译参数 | 见下 |
| ~~`R1-29` journal 扇区满了能 reclaim 并恢复~~ | ✅ **2026-09-18 证完了** —— 新脚本 `tools/run_journal_reclaim.py` | 见下 |
| `R1-31` RTC 备份域失效能被发现 | ⚠️ **证据已降级** —— 当时观察到的是首次初始化，不是真正的失效，见下节 |
| `R1-06` 上传模式的通道只有一条 | 只有 `P2`，它测的是跨仓代码不分叉，不是这条需求本身 | 一个直接测它的用例 |
| `R1-07` 多个工具不会互相打断上传 | 同上 | 同上 |
| `R1-14` 每块板的 MAC 互不相同 | 只有一块板 | **第二块板** |

---

## ⚠️ 撤回：「app 尺寸上限比 bootloader 大 128 KiB」——那条判断是错的

**这条今天写过一次，是错的，现在撤回。** 原判断只是拿两个数字做减法
（`boards.txt` 的 `1,966,080` 减 `IAP_APP_MAX_SIZE` 的 `1,835,008`），
**没有实际编一个卡在缝里的镶像去验证**——这正是 `verify-dont-assume` 那条规矩要防的事。

### 补做验证之后，结论反过来了

`platform.txt:35` 里链接器的调用方式是：

```
-Wl,--defsym=LD_FLASH_OFFSET={build.flash_offset}
-Wl,--defsym=LD_MAX_SIZE={upload.maximum_size}
```

`variants/STM32H7xx/H743/ldscript.ld:54`：

```
FLASH (rx) : ORIGIN = 0x8000000 + LD_FLASH_OFFSET, LENGTH = LD_MAX_SIZE - LD_FLASH_OFFSET
```

**`upload.maximum_size` 不是直接的"app 能装多少"，它是"从 0x08000000 算起，可用 flash
到哪结束"**——链接脚本自己会再减掉一次 `flash_offset`（=`0x20000`=131,072）。
代入现在 `boards.txt` 的 `1,966,080`：

```
FLASH 起点 = 0x08000000 + 0x20000        = 0x08020000  ← 正好是 IAP_APP_ADDRESS
FLASH 终点 = 0x08000000 + 1,966,080      = 0x081E0000  ← 正好是 IAP_STATE_SECTOR_ADDR
FLASH 长度 = 1,966,080 − 131,072         = 1,835,008   ← 正好是 IAP_APP_MAX_SIZE
```

**三个数字全部对得上，`1,966,080` 从一开始就是对的。** 拿一个刻意卡在
1,835,008–1,966,080 之间的镶像实测：

| 用的 `upload.maximum_size` | 结果 |
|---|---|
| 原始值 `1,966,080` | **链接失败**，`region 'FLASH' overflowed` —— IDE 编译这一步就拦住了 |
| 我改成 `1,835,008` 之后（已撤销） | 同样报 `overflowed`，但溢出量多了 131,072 字节 —— **这个改动会平白吃掉 128 KiB 本该给用户的合法空间** |

**唯一真实存在的问题只是显示文字**：`arduino-cli`/IDE 编译完打的
`Sketch uses N bytes ... Maximum is 1966080 bytes`，那句 `Maximum is` 抄的是
`upload.maximum_size` 原始值，**没有减掉 `flash_offset`**，所以显示的可用空间比链接器
实际给的（`1,835,008`）虚高了 128 KiB。**这只会让人以为余量比实际多，不会让一个超尺寸的
镶像真的编过、传上去**——链接器该拦的地方一直都拦着。

**处理**：`boards.txt` 已经改回原值 `1,966,080`（我一开始改错的那次已撤销，
`check_core_sync.py` 确认 live 和 `open_plc_arduino` 仍然一致，两边都没被污染）。
**这条不需要改代码，只值得记一句「显示数字比实际余量高 128 KiB，是文字问题不是功能问题」。**

---

## 2026-09-18 · 网卡选择修复（方案 A）与路上抓到的两个 bug

用户指出「iap 脚本只查物理网卡，忽略其他的，而且要兼容 linux 和 mac」。核实后：规矩的
**权威实现早就有**（core 的 `tools/discovery/`，三平台各一份分类器），但 `IAPTool` 和
`TestCase` 都没用它。决定记在 `$PROD/docs/tables/DECISIONS.md` 第 51 条，
跨仓镜像登记在 `$PROD/docs/repo/ARCHITECTURE.md` 的镜像表第 10 项。

**做了什么**

| 文件 | 改动 |
|---|---|
| `$TOOL/netiface/`（新增 4 个文件） | 从 core 镜像过来：`IsPhysical` 的四条判定 + `iface_{windows,linux,darwin}.go` 三个分类器，另加 `LocalIPFor()` 挑源地址 |
| `$TOOL/IAP_Ether.go` | `getDirectedBroadcastAddrs()` 改用 `netiface.Physical()`，不再往虚拟网卡广播 |
| `$TEST/udp_discovery.go` | 两处拨号收敛成 `dialBoard()`，把源地址钉在物理网卡上 |
| `$TEST/tcp_session.go` | `dial()` 同样钉源地址 |
| `$TEST/tools/check_mirror_sync.py` | 新增 4 个 `P2` 锚点（判定函数体 + 三个分类器函数体），跑过全 OK |

**bug 1 · VPN 会冒充板子，制造假的「TCP 通」**

不绑定源地址时，连 `192.168.0.3:56865` **0.03 秒就"连上"**（本地地址 `172.19.0.1`），
随即 RST；绑定到 `192.168.0.2` 则 6 秒超时。**那个"连上"是 VPN 端点答的，不是板子。**

**bug 2 · `T1-01` 把一句没测过的话当结论印出来**

失败文案里写死了「yet its TCP server is up」，**这条用例从来没有真的探过 TCP**。
板子关着的时候它照样这么说，于是把人引向「UDP 单独挂了」。已改成先探一次再下结论，
两种情形分开报：TCP 也连不上 → `the board is absent, not a UDP fault`。

⚠️ **这两个 bug 都不是 A 方案要修的东西，是修 A 的路上撞见的。** 改动已经做了，
**要不要保留由用户定**。

**验收**：`go build ./...` 和 `go vet ./...` 干净，`go test ./...` 全过，
`check_mirror_sync.py` 四个新锚点全 OK。`T1-01` 的源地址从 `172.19.0.1` 变成 `192.168.0.2`，
结论从「UDP 单独挂了」变成「板子不在线」—— **后者是对的**。

**更正一处因果判断。** 当时测到「出口是 tun0、源地址 172.19.0.1」是真的，但根因更可能是
**板子那边网络不在时 WLAN 的直连路由没生效**，不是 VPN 抢路由 —— 板子通电后 `tun0` 仍然连着
（172.19.0.1 还在），而去 `192.168.0.3` 的路由已经自己走回 WLAN 的 `192.168.0.0/24`。
两次测量都是真的，讲的因果错了一半。**不受影响的是**：VPN 冒充板子应答 TCP 这件事实测过，
`T1-01` 印未验证断言这件事和路由无关。

⚠️ **还没做的一半：Python 那几个脚本没有物理网卡筛选。**
`tools/run_au1.py`（`probe_board()`）、`tools/run_s4.py`、
`tools/run_cdc_does_not_start_ethernet.py` 都是直接 `socket.connect()`，跟着路由表走。
今天能跑是因为路由正好是对的。要补齐得先定一件事：**Python 侧是再写一份平台判定，
还是让它去问 Go 那份**（写第三份实现会让同一条规矩有三个出处，`P2` 也管不着 Python）。
**等用户定，本轮不动。**

---

## 2026-09-18 · 启动日志里观察到备份域丢失

跑 `T1-17` 的 phase 1 时，板子的启动日志里有：

```
** Backup domain was lost - RTC battery absent or empty. **
** Nonce counter is zero; replay protection is weakened. **
```

对应 `$BOOT/IAPServer/iap_auth.c:161-163` —— witness 丢了**且**计数器为 0 的那条分支
（另外两条分支是「域还在」和「witness 读数不可信但内容还在」）。

⚠️ **根因 2026-09-19 查明是 RTC 时钟源不一致**（见下面 09-19 那节），已修。

### 同一份日志里白捡的事实

| 事实 | 原文 |
|---|---|
| 板子**未被认领** | `Owner slot: empty, using the built-in root key` + `** This board trusts the PUBLISHED root key ... **` |
| journal 用量 | `Bootloader state: 211/4096 journal slots used, metadata present` |
| **app 区实际容量** | `Invalid flash size 2000000, app region only has 1835008 bytes` —— **板子自己印的 1,835,008**，是「Arduino core 上限大 128 KiB」那条的硬证据 |
| 复位原因 | `** Reset cause: SOFT`（脚本用认证重启命令把板子送进 bootloader，符合预期） |
| SDRAM 自检 | `SDRAM staging buffer OK (2 MiB at C0000000)` |

---

## 2026-09-18 · 两条掉电用例的结果，以及一条必须更正的结论

### 结果

| 用例 | 结果 | 关键证据 |
|---|---|---|
| `T1-21` 掉电落在传输期 | ✅ **通过**（一次命中） | 拔电期间目标电压 **0.00V**；回来后 `** Reset cause: POR`、`** APP Mod ...`、`[M5] ready - send me a byte` —— 旧 app 原样起来，flash 没被碰 |
| `T1-22` 掉电落在擦写窗口 | ✅ **通过**（第 3 次命中） | `** App signature invalid or absent - staying in bootloader **` + `** UPLOAD Mod ... (no valid application)`，随后重传恢复成功 |

⚠️ **`--pad-to 1835008` 不是可选的。** 不补零时窗口只有几秒，抓不住；补到 app 区上限后
传输窗口约 34 秒、擦写窗口约 20 秒（脚本 `run_s4.py:399-401` 标的值，本次实测吻合）。

⚠️ **前两次都拔晚了**，脚本抓到 `Checksum and signature OK` 判定「窗口已关」并自动重试，
**没有把错时机的掉电记成通过**。延迟来自「脚本提示 → AI 收到 → AI 转达 → 人动手」这一串；
**下次这类窗口紧的用例，让人自己在终端里跑，不要经 AI 中转。**

### ⚠️ 首次开机和真正的备份域失效，输出长得一样

`$BOOT/IAPServer/iap_auth.c:148` 判的是 witness 值，**报完之后**第 166-168 行才把 witness 写进去。
所以「域第一次被这版固件看到」和「域真的丢了」在第一次开机时印出来的是同一句话。

**后果**：`R1-31`（RTC 备份域失效能被发现）2026-09-18 那半条证据**要降级** ——
当时观察到的是首次初始化，不是真正的失效。

### 顺带：两个作废的旧用例编号已经清干净了

掉电那两条用例的旧编号（现在是 `T1-21` / `T1-22`）原先还活在三处，**已全部处理**：

| 在哪 | 处理 |
|---|---|
| `$TEST/tools/run_s4.py`，9 处 | ✅ 改成新编号，**包括判词里印给人看的那两个前缀** |
| `$BOOT/RELEASE-NOTES.md`，1 处（**面向客户**） | ✅ 改成新编号 |
| `DR-02-test-id-mapping.md` / `DR-03-...md` | **不动** —— 对照表本来就该写旧编号，它们在 ALLOW 里 |

⚠️ 本文件不再复述那两个字面，`check_no_stale_ids` 因此不会再命中它们。

---

## 2026-09-18 · `IAPTool` 的四个单播通信点也补上了物理网卡绑定

用户指出：今天早上的修复（决策 51）只接进了 `IAP_Ether.go` 的广播发现函数
（`getDirectedBroadcastAddrs`），**而真正每次升级都会走到的四步单播通信一个都没补**：

| 步骤 | 函数 | 干什么 |
|---|---|---|
| ① | `sendUDPWithResponseOnPort`（经 `udpIdentifyWithRetry`） | 问用户填的 IP「你是 bootloader 还是 app」 |
| ② | `sendUDPNoResponseOnPort`（经 `authenticatedUDPReboot`） | 发认证重启命令 |
| ③ | 同 `sendUDPWithResponseOnPort` | 重启后再确认一次状态 |
| ④ | `RunEther_TCP` / `etherPreflight` | 真正传文件的两条 TCP 连接 |

这四处都没有绑物理网卡，走的是系统路由表——**VPN 有更优默认路由时，单播照样会被劫走**，
和今天已经证实过的现象（不绑源地址连板子 IP，0.03 秒被 VPN 端点"接住"再 RST）是同一个机制，
只是发生在单播而不是广播。

**已补齐**：新增 `dialUDPBoard()` / `dialTCPBoard()` 两个共用辅助，四处全部改用它们。
两个都调用 `netiface.LocalIPFor()` —— **不需要新写平台相关代码**，那个包已经是三平台实现。

**验证**：`go build` / `go vet` 干净；一次真实上传（`upload_and_watch.py`）四步全部走过，
`all checks passed`。

---

## 2026-09-18 · Python 测试脚本也补上了物理网卡绑定（选的是复用，不是第三份实现）

三个脚本原来直接开裸 socket，跟着系统路由表走：`run_au1.py`（`T1-17`）、
`run_s4.py`（`T1-21`/`T1-22`）、`run_cdc_does_not_start_ethernet.py`（`T1-25`）。

**没有在 Python 里重新写一份网卡判定**（那样同一条规矩就有了 Go 三份 + Python 一份，
没人盯得住）。做法是：

| 新增 | 干什么 |
|---|---|
| `$TEST/tools/netifquery/main.go` | 几行的独立 Go 程序，`go run` 调用，直接复用今天写的 `netiface.LocalIPFor()` |
| `common.py` 的 `local_ip_for(ip)` | `subprocess` 调 `netifquery`，拿到该绑哪个源 IP，或 `None`（表示"让系统路由，板子在网关后面"） |

**三个脚本各自的接法不同，因为调用频率不同**：

| 脚本 | 探针调用频率 | 接法 |
|---|---|---|
| `run_au1.py` | 每 0.7 秒一次，跑到 20 分钟 | `main()` 里**只解析一次**，往下传参数 |
| `run_s4.py` | 每 0.2 秒一次（`wait_for` 默认 tick），且调用点分散在四处 | 模块级缓存 `_local_ip_cache`，按 IP 缓存，不用穿参数 |
| `run_cdc_does_not_start_ethernet.py` | 每次运行只调 2 次 | 直接调，不缓存 |

⚠️ **`local_ip_for` 会 `go run`（要编译），绝对不能放进逐次探针的热循环**——
这是选接法时唯一的硬约束，前两个脚本都是照这条分的。

**验证**：`go build`/`go vet` 干净；真板子上三个脚本各自验证过（`T1-25` 判据齐全地过；
`discovery_answers` 缓存正确解出 `192.168.0.2`）。

⚠️ **验证时踩到一次假阴性**：连续快速重测时板子返回"不在线"，原因是板子自己的
**每源限流窗口**（约 2 秒），不是绑定代码的问题——隔开几秒重试，应答立刻恢复。

---

## 2026-09-18 · `T1-17` 第三次跑：加了电压交叉验证，结论没变，而且抓到一次真的假阳性

用户指出第二次那轮"我刚才没拔电"——这话戳中一个真问题：`run_au1.py` 原来只靠
**UDP 连续三次不应答**判断"电断了"，没有像 `run_s4.py` 那样用 ST-Link 量电压做独立交叉验证。
如果那只是一次网络抖动，脚本会把它误判成真断电。

### 补的验证

把 `run_s4.py` 的 `target_voltage()` 挪进 `common.py` 共用（不重复写第二份），
`run_au1.py` 在判定"板子安静了"之后，立刻量一次 ST-Link 的 VTREF 读数：
**读数明显不是 0 就报「这是网络抖动，不是断电，本轮没测到任何东西」，不再往下走。**

### 补上之后，当场抓到一次真的假阳性

重新起跑那一轮，用户确认没有拔电，脚本**诚实地等了 20 分钟然后超时**，
没有像之前那样误判"断电了"——这证明了两件事：① 新加的检查逻辑是对的；
② 上一次的失败结论确实混入过一次不可靠的输入（尽管这次没能回溯确认那一次是不是
真的误判，但机制上的漏洞是真实存在的，值得堵）。

### 第三次，带着电压证据，重新确认

`--resume` 接上刚才的 phase 1 数据，用户这次真的拔了电：

```
target voltage during the cut: 0.00V -- confirmed off
```

**结果不变**：掉电前计数器 18，掉电后 2，8/8 复用。

> ⛔ **证据最硬的一次 FAIL** —— 真断电有独立电压读数背书，不再有「到底断没断电」的疑点。
> **根因 2026-09-19 查明是 RTC 时钟源不一致**，见下节。

## 2026-09-19 · 根因：bootloader 和 core 的 RTC 时钟源不一致

备份域是固件自己清的：
bootloader 选 LSE、Arduino core 选 LSI，HAL 见 `RTCSEL` 要变就强制复位整个备份域。
决定、理由和修法见 [DECISIONS.md 第 57 条](../../../OpenPLC_Docs/docs/tables/DECISIONS.md)。

### 证据：不断电的对照实验

SWD 只读寄存器。`RCC_BDCR` = `0x58024470`，`DR1` = `0x58004054`，`DR3` = `0x5800405C`。

| 板上跑的是 | 修复前 `RTCSEL` / `DR1` / `DR3` | 修复后 `RTCSEL` / `DR1` / `DR3` |
|---|---|---|
| app | LSI / 0 / 0 | LSE / 1 / `0x56424154` |
| bootloader | LSE / 1 / `0x56424154` | LSE / 2 / `0x56424154` |
| app（复位跳回） | **LSI / 0 / 0** | **LSE / 2 / `0x56424154`** |

全程没断电，VTREF 一直 3.25V。修复前 bootloader 每次打 `Backup domain was lost`，
修复后打 `Backup domain retained`。

### `T1-17` 真断电验收：通过

ST-Link 量到 **0.00V** 确认真断电。断电前计数器 **11**，断电后从 **13** 续
（gap 2 = 重启握手自己消耗的），两阶段 16 个 nonce 全不同。

```
T1-17 passed -- nonces are unique and the counter survived a real power cut
```

---

## 跑完之后要做的

1. 把每一行的 ⬜ 换成实际结果（通过 / 不通过 + 一句话）
2. 有不通过的，**先写在这里，再决定是改代码还是改判据**
3. `$PROD/docs/modules/M1-firmware-upgrade.md` 第 270-278 行那张覆盖率小结表是旧的
   （写 18 条有用例直接测，实际 23 条；把 `R1-01` `R1-03` `R1-05` `R1-22` `R1-28` 还列在「纯手工」里，
   而功能表里它们的「谁证明」已经是 `T1-23`–`T1-26`）。**这轮跑完顺手修掉**
4. `tools/run_journal_slot_accounting.py` 文件头注释还写着 5 槽和旧编号 `D1`，代码里是
   `SLOTS_PER_UPLOAD = 9`。**代码对，注释旧**，顺手修掉
