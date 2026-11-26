# Working in this repository

当前接续为 R2 串行包候选，先读 docs/R2_CONTRACT.md、docs/R2_VALIDATION.md 、docs/R2_BOUNDARY_REVIEW.md 与 docs/R2_RECOVERY_RECHECK.md。固定运行/安装源码 afccbde，测试接续为 81511bc；12 个运行模块一致，源码/安装各 115 项检查及仓外两协议示例通过。运行包复用 R1 事务核心，r2.store.v1 与旧合成库互相拒绝；只在本项目原环境验证，不替换现有业务安装。下一步按验收缺口接续 R2/R3；旧 R1 过程按原时点保留。所有新 PR 不自动合并。

## R1 历史基线

先读 README、docs/REQUIREMENTS.md、docs/ARCHITECTURE.md、docs/IMPLEMENTATION_PLAN.md。当前为 R1 源码合同/事务原型；具体 API 与边界见 docs/R1_CONTRACT.md，准确验证见 docs/R1_VALIDATION.md、docs/R1_REVIEW.md、docs/R1_CAPACITY_REVIEW.md、docs/R1_CLEANUP_POLICY_REVIEW.md、docs/R1_STORAGE_REVIEW.md、docs/R1_CONNECTION_REVIEW.md 与 docs/R1_CONTROL_RECOVERY_REVIEW.md。当前 state/store 为 v2，拒绝旧合成布局；67 项源码检查通过；最新只补控制/结算的 64 组恢复检查，运行载荷保持 4398a55，尚未安装。没有网络发送、供应商实现、安装包或真实模型证据；不能直接替换应用现用 S1/S2。

R1 以本地单 SQLite 事务维护 Runtime 与应用记录一致；应用经公开端口操作自己表。未知发放不得恢复令牌，部分用量不等于最终用量，业务不可采用不阻止真实终结/用量落账。端口可信且禁外部 I/O，不是恶意代码沙箱。后续 R2 抽取现有串行核心，R3 接入与迁移，不另建第二套生产调用器。

每台机器复用本项目已有虚拟环境，源码、解包和安装证据分开。测试仅使用临时合成库，不重启或升级业务服务、不写真实业务数据、不同步 NAS。所有交付走 PR，不自动合并。没有新的实现和验证条件变化时复用准确历史证据，不累计重合测试数。

README 图示表示完整目标；实现状态在正文和验证表中另列。仅改文字且 Mermaid 源码未变时无需重渲染，不声称重新渲染过。
