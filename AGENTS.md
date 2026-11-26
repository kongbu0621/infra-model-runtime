# Working in this repository

先读 README、docs/REQUIREMENTS.md、docs/ARCHITECTURE.md、docs/IMPLEMENTATION_PLAN.md。当前为 R1 源码合同/事务原型；具体 API 与边界见 docs/R1_CONTRACT.md，准确验证见 docs/R1_VALIDATION.md 和 docs/R1_REVIEW.md。当前 state/store 为 v2，拒绝旧合成布局；36 项源码检查通过，尚未安装。没有网络发送、供应商实现、安装包或真实模型证据；不能直接替换应用现用 S1/S2。

R1 以本地单 SQLite 事务维护 Runtime 与应用记录一致；应用经公开端口操作自己表。未知发放不得恢复令牌，部分用量不等于最终用量，业务不可采用不阻止真实终结/用量落账。端口可信且禁外部 I/O，不是恶意代码沙箱。后续 R2 抽取现有串行核心，R3 接入与迁移，不另建第二套生产调用器。

每台机器复用本项目已有虚拟环境，源码、解包和安装证据分开。测试仅使用临时合成库，不重启或升级业务服务、不写真实业务数据、不同步 NAS。所有交付走 PR，不自动合并。没有新的实现和验证条件变化时复用准确历史证据，不累计重合测试数。

README 图示表示完整目标；实现状态在正文和验证表中另列。仅改文字且 Mermaid 源码未变时无需重渲染，不声称重新渲染过。
