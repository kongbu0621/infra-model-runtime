# PR #7 宿主、权限与内容确认复核

## 再复核：内容确认事务的提交与丢确认窗口

2026-11-27；测试提交 `c6dbd21a7f6cac64d0dfa99d8eb20da99634cd66`，运行源码保持 `999e374`。本轮未确认新的运行缺陷；增加三个专项方法，分别检查 input、wire、response 的四个窗口：COMMIT 前检查失败、进入提交但 SQL 尚未执行、SQL 已提交但确认丢失、提交已确认后报错。

四个窗口分别得到 `not_committed`、`unknown`、`unknown`、`committed`，均保留准确的 `content-confirm` 操作身份。两个 unknown 分别核验了实际未提交和已提交的状态，不能仅根据错误码猜测写入结果。内容端口已保存的 bytes 保持；重开后用原身份确认，原槽不额外写入，最终每个调用仅一次模型发送。结算后逐字段比较完整预算，重复恢复不改变协调库记录。

| 检查 | 本轮结果 |
| --- | --- |
| 新增源码专项 | 3 个方法通过，内含 12 个提交窗口；窗口与方法数不相加 |
| 原候选 wheel 解包 R3 套件 | 27 项通过，零失败/错误/跳过；13 个运行模块与源码逐字节匹配 |
| 编译与复用 | 新增 1 个文件三级编译通过，Python 3.11 仅语法检查；此前 43 个 Python 文件摘要未变，完整源码 165 项及原编译结果按准确来源复用，不记作本轮重跑 |

复用 wheel SHA-256 `200b6995c4cf4c348116785ead65f1f009792e13dace685de495a328848bcfb6`；未重新构建或安装。证据：[汇总](evidence/2026-11-27-pr7-commit-boundary/validation.json)、[新增源码专项](evidence/2026-11-27-pr7-commit-boundary/source.txt)、[候选报告](evidence/2026-11-27-pr7-commit-boundary/candidate.json)、[候选日志](evidence/2026-11-27-pr7-commit-boundary/candidate.txt)、[编译与未变文件摘要](evidence/2026-11-27-pr7-commit-boundary/compile.json)。

测试脚手架初稿遗漏事务外 checkpoint，出现 IndexError 并影响后续子例；已增加活动事务判断，最终测试仍要求目标故障准确触发。[初稿日志](evidence/2026-11-27-pr7-commit-boundary/harness-draft.txt)保留，不计为产品缺陷。当前验证是受控异常与实际 SQLite 提交，不等同进程强杀、设备断电、网络文件系统或所有并发调度；R3 迁移及正式部署边界保持。

## 再复核：宿主表名单的输入分类

2026-11-27；修复源码 `999e374e4d35ee4e67fb360edf85af7a5b72e1c6`。宿主名单原来先执行去重集合，再检查元素类型；tuple 中含 list、dict 或 set 时泄漏 Python `TypeError`，没有按公共合同返回 `INVALID_INPUT`。现在先验证容器、数量及每个表名，再检查重复；所有这些检查都发生在宿主 guard 和数据库访问之前。

新增一个测试方法覆盖九种非法名单，并核对 guard 零进入、协调库完整快照保持，以及恢复合法名单后重开和提交成功。三个不可哈希元素在旧版分别触发错误，见[旧版反例](evidence/2026-11-27-pr7-integrity-review/before.txt)。这是一个输入校验顺序缺陷，不将三个子例重复计为三个产品问题。

| 检查 | 本轮结果 |
| --- | --- |
| 完整源码 | 165 项通过，零失败/错误/跳过，包含原 R1/R2 与 R3 回归 |
| 新候选 wheel 解包 | 24 项 R3 专项通过；13 个运行模块逐字节匹配修复源码，所有已加载 Runtime 模块来源正确 |
| 编译 | 43 个文件，优化级别 0/1/2；Python 3.11 仅语法检查 |
| 环境 | GX10 / Linux aarch64 / Python 3.12.3，复用原项目解释器；临时合成数据 |

新 wheel SHA-256：`200b6995c4cf4c348116785ead65f1f009792e13dace685de495a328848bcfb6`。证据：[汇总](evidence/2026-11-27-pr7-integrity-review/validation.json)、[源码日志](evidence/2026-11-27-pr7-integrity-review/source.txt)、[候选报告](evidence/2026-11-27-pr7-integrity-review/candidate.json)、[候选日志](evidence/2026-11-27-pr7-integrity-review/candidate.txt)、[编译摘要](evidence/2026-11-27-pr7-integrity-review/compile.json)。本轮没有安装、部署或合并；旧业务迁移、真实模型及完整 R3 验收边界保持。下方各轮结果保留原版本，不累加。

## 再复核：接受其他句柄已完成的准确内容确认

2026-11-27，修复源码 `aeefff35ca5d240f777b732c7f9da87313c37c32`。在上一轮 `10e8f80` 上继续检查合法交错，发现写前复查把“别的句柄已经确认”直接当作内容损坏。另一句柄正常保存并确认 input/wire/response 后，原句柄仍可能报 `INTEGRITY_FAILURE`；响应场景会中断本可完成的结算。

现在先看最新确认标记：仍未确认才允许首次保存；已确认则只读回实际内容。准确 bytes 可继续，缺失、不同字节或错误类型仍拒绝，禁止从暂存重建已确认内容。这保留了上轮缺失拒绝规则，同时修复正常进展的误报。

| 检查 | 本轮结果 |
| --- | --- |
| 旧版复现 | 两个新方法的 input、wire、response 三个场景均误报，见[旧版日志](evidence/2026-11-27-pr7-confirmation-recheck/before.txt) |
| 新增边界 | 双句柄受控交错的准确内容只读接续、原响应结算、错误字节/类型拒绝，以及确认后中断再重开；没有额外内容写入或重复 POST |
| 完整源码 | 164 项通过，零失败/错误/跳过；包含此前缺失拒绝、读权、宿主锁与 R1/R2 回归 |
| 候选解包 | 23 项 R3 专项通过；13 个运行模块与修复源码逐字节一致，加载来源均在候选目录 |
| 编译 | 42 文件，优化级别 0/1/2；Python 3.11 仅语法检查 |

新 wheel SHA-256：`cf3742c704b749ee218f8cc9bb76ff2f5bbff1e4bd8db703aeab21e34358c26d`。验证继续使用 GX10 / Linux aarch64 / Python 3.12.3 原项目解释器；没有安装或部署。

证据：[汇总](evidence/2026-11-27-pr7-confirmation-recheck/validation.json)、[源码日志](evidence/2026-11-27-pr7-confirmation-recheck/source.txt)、[候选报告](evidence/2026-11-27-pr7-confirmation-recheck/candidate.json)、[候选日志](evidence/2026-11-27-pr7-confirmation-recheck/candidate.txt)、[编译摘要](evidence/2026-11-27-pr7-confirmation-recheck/compile.json)。新交错为同进程两个独立句柄的确定顺序，不等于所有多进程调度或存储故障已验收；跨组件全局原子性、旧业务迁移和真实部署仍未完成。

## 首次复核（历史记录）

2026-11-27；修复源码 `10e8f80ffb2e2b4828c16235693caaffaf04f5a7`，继续原 PR #7。源码版本仍为 `0.1.0a2` 候选，没有安装或部署。首轮 `813da037` 的 155/14 项保留原时点，不能代替本次修复验收。

## 确认的问题及修复

| 问题 | 修复与反向核对 |
| --- | --- |
| 显式复制内容仅在 I/O 前检查读权，期间撤权仍成功返回 | 三种角色在 I/O 后再次检查读权；拒绝后预算与发送次数不变，恢复读权后原内容可取回。内部恢复仍能结算已发生费用 |
| 宿主进入时捕获重入错误后，内层事务清空违规标记；退出时违规也被忽略 | 进入内层前拒绝，原库不变；退出时拒绝但保持原 `committed` 身份，原请求可幂等重放 |
| 宿主进入/首次检查错误缺少操作身份，普通 I/O 错误直接泄出，只读 BUSY 被报未提交 | 统一携带原操作；只读为 `not_applicable`、尚未进入写事务为 `not_committed`。内层已确认的提交状态、停止信号及主异常保持 |
| 内容读取期间另一句柄已确认槽，原句柄仍依赖旧的未确认状态重建丢失内容 | 写入前短事务重新核对最新确认标记；受控交错中拒绝重建，保留一次原写入、零发送 |

前四个新测试方法在旧版出现 9 个断言失败与 4 个错误子例，见 [旧版日志](evidence/2026-11-27-pr7-review/before.txt)。内容交错的第五个方法另有 [旧版反例](evidence/2026-11-27-pr7-review/stale-confirmation-before.txt)。方法数与子例数不累加。

内容写入前检查只约束该次决定；跨组件 I/O 之间没有全局锁。检查后已开始的写入无法被后来确认/删除原子撤回，不能宣称验证了所有并发存储故障。详见 [合同边界](R3_HOSTED_CONTRACT.md)。

## 验证

| 检查 | 结果 |
| --- | --- |
| 完整源码回归 | 160 项，零失败/错误/跳过，含原 R1/R2 与 19 项 R3 |
| 新 wheel 解包专项 | 同组 19 项通过；所有已加载 Runtime 模块来自该解包目录，13 个运行模块逐字节匹配修复源码 |
| 编译 | 41 个 Python 文件，优化级别 0/1/2；Python 3.11 只检查语法 |
| 环境 | GX10 / Linux aarch64 / Python 3.12.3，使用既有项目解释器及临时合成库 |

候选 wheel SHA-256：`594a8322506910541c9e6c4b05e4f3bc5dbae5aad55e6ef1f666c8294c84ec3e`。同名版本号不意味着与首轮 wheel 相同，必须按摘要区分。没有修改已安装的 R2 包。

证据：[汇总](evidence/2026-11-27-pr7-review/validation.json)、[源码日志](evidence/2026-11-27-pr7-review/source.txt)、[候选报告](evidence/2026-11-27-pr7-review/candidate.json)、[候选日志](evidence/2026-11-27-pr7-review/candidate.txt)、[编译与文件摘要](evidence/2026-11-27-pr7-review/compile.json)。源码复现命令沿用原验证页；新专项发现模式为 `test_r3*.py`。

本次未验证真实模型、旧业务迁移、正式部署、网络文件系统或断电。源码/解包通过不等于安装与业务验收，PR 不自动合并。
