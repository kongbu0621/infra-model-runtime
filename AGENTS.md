# Working in this repository

2026-11-27 同一 PR #7 [再复核](docs/PR7_REVIEW.md)修复合法确认交错的误报：源码 `aeefff3`，另一句柄已确认时只读核准确内容，不重建或额外写入。源码 164 项、新 wheel 23 项及 42 文件三级编译通过；下方旧修复/轮次保持历史。现用安装未变，不自动合并或部署。

2026-11-27 PR #7 [复核修复](docs/PR7_REVIEW.md)为 `10e8f80`：复制最终读权、宿主重入/异常身份、内容写前确认状态。源码 160 项、新 wheel 专项 19 项及 41 文件三级编译通过。新 wheel 摘要见复核页；原安装未升级，旧记录保持原时点，不自动合并。

2026-11-27 新增 [R3 同库接缝与强制内容确认候选](docs/R3_HOSTED_CONTRACT.md)，源码版本 `0.1.0a2`。保持 R1/R2 合同，新增显式宿主/内容绑定和事务外确认；本轮未安装或部署。原 R2 安装与下方历史记录保持；下一项为消费者旧状态迁移及正式接入验收，新 PR 不自动合并。

当前接续为 R2 串行包候选，先读 docs/R2_CONTRACT.md、docs/R2_VALIDATION.md 与 docs/R2_CONTROL_REVIEW.md。运行/公共原候选安装保持 9536de7，新增测试 c3a4797；源码/安装各 141 项通过，新增三个方法包含 70 组后续控制与原调用结算组合。明确 recover 可先结算再因状态读权返回 DENIED，不得把读错误当作结算回滚。12 模块、原 wheel/RECORD 与环境保持，本轮无新运行修复或安装。历轮载荷分类、导出及旧库接续证据保持各自时点。消费者业务安装未变；后续按 R3 消费/迁移接续，所有新 PR 不自动合并。

## R1 历史基线

先读 README、docs/REQUIREMENTS.md、docs/ARCHITECTURE.md、docs/IMPLEMENTATION_PLAN.md。当前为 R1 源码合同/事务原型；具体 API 与边界见 docs/R1_CONTRACT.md，准确验证见 docs/R1_VALIDATION.md、docs/R1_REVIEW.md、docs/R1_CAPACITY_REVIEW.md、docs/R1_CLEANUP_POLICY_REVIEW.md、docs/R1_STORAGE_REVIEW.md、docs/R1_CONNECTION_REVIEW.md 与 docs/R1_CONTROL_RECOVERY_REVIEW.md。当前 state/store 为 v2，拒绝旧合成布局；67 项源码检查通过；最新只补控制/结算的 64 组恢复检查，运行载荷保持 4398a55，尚未安装。没有网络发送、供应商实现、安装包或真实模型证据；不能直接替换应用现用 S1/S2。

R1 以本地单 SQLite 事务维护 Runtime 与应用记录一致；应用经公开端口操作自己表。未知发放不得恢复令牌，部分用量不等于最终用量，业务不可采用不阻止真实终结/用量落账。端口可信且禁外部 I/O，不是恶意代码沙箱。后续 R2 抽取现有串行核心，R3 接入与迁移，不另建第二套生产调用器。

每台机器复用本项目已有虚拟环境，源码、解包和安装证据分开。测试仅使用临时合成库，不重启或升级业务服务、不写真实业务数据、不同步 NAS。所有交付走 PR，不自动合并。没有新的实现和验证条件变化时复用准确历史证据，不累计重合测试数。

README 图示表示完整目标；实现状态在正文和验证表中另列。仅改文字且 Mermaid 源码未变时无需重渲染，不声称重新渲染过。
