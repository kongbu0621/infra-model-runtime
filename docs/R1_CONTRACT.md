# R1 最小合同与同库事务原型

2026-11-26，`r1.state.v2` / `r1.store.v2` candidate，其余版本保持 v1。准确运行源码为 `8e1ebf9a5eb91ff31d32a3cc737047540b79864b`，验证见 [R1 记录](R1_VALIDATION.md)与[清理和许可字段复核](R1_CLEANUP_POLICY_REVIEW.md)。这是可执行的源码原型，不是已发行包；没有网络、供应商协议、凭据解析、真实模型或现有业务库迁移。R2 将复用/抽取协议和配置实现，不能将本原型作为第二套生产调用器继续扩建。

## 值对象与操作

值对象为冻结 dataclass，集合使用 tuple；布尔与整数严格区分，非法版本拒绝。字段与容量由 [contracts.py](../src/infra_model_runtime/contracts.py) 实现，序列化只用于本原型存储，不声明兼容原应用布局。

| 对象 | 关键字段与含义 |
| --- | --- |
| `Profile` | 名称、绑定修订、模型标识、能力集合、启停；本轮仅声明合成目标，不证明任一厂商能力 |
| `Config` | profiles、默认目标、稳定 budget_pool/resource_group、累计调用/输入字节/输出单位上限、登记容量；一个资源组、一个预算池、一个执行槽 |
| `Request` | namespace/request_id、task、input_text、allowed_profiles、required_capabilities、output_contract、output_limit、deadline_ms、可选 admission_ref；完整输入指纹判重 |
| `Receipt` | 操作、原身份、修订和指纹；登记回执稳定，后续状态另查 |
| `IssuePermit` | 只在首次发放成功提交后返回的随机令牌及冻结目标；状态查询和重试不返回令牌 |
| `Observation` | 递增修订、终结事实、unknown/ok/error、有界输出及可选累计输出单位；由可信调用器提供，尚无供应商证据解析 |
| `CallState` | 冻结配置、issued/cancelled/terminal/outcome、用量、usage_final、未结预留、观察修订、结果准确 UTF-8 字节的 result_sha256（无正文为 None）；不含结果正文，不可用单个 SUCCESS 替代 |
| `RuntimeFailure` | 版本、错误码、合法操作身份、not_committed/committed/unknown/not_applicable；只描述本次本地操作，不推断远端事实 |

| 入口 | 行为 |
| --- | --- |
| `Runtime.create(path, config, application)` / `Runtime(path, application)` | 建文件前校验应用身份；排他创建遇已有路径 CONFLICT，其它建文件/初始关闭 I/O 故障 STORAGE_ERROR，均 not_committed；初始化事务仍按提交事实分类。普通打开拒绝缺失、部分或另一应用身份的库，不自动建空账 |
| `configure(control_id, expected_revision, config)` | 原管理意图幂等；默认切换只影响新登记；有登记后不能换预算池或资源组，降低限额不抹旧账 |
| `register(request)` | 同身份同完整输入返回原回执；更改输入冲突；业务关联与冻结请求共同提交 |
| `issue(namespace, request_id, now_ms=...)` | 复核当前目标绑定、许可、业务依赖、取消、截止及所有额度，原子记录发放和预留；再次调用只报 ALREADY_ISSUED |
| `cancel(namespace, request_id, control_id)` | 本地取消与业务失效共同提交；已发放时不证明远端取消，不释放未知占用 |
| `observe(namespace, request_id, observation)` | 同修订同内容重放无增量；旧已见修订返回原回执，未见更低修订拒绝，冲突/用量回退拒绝；业务关联与结算共同提交 |
| `status(...)` / `budget()` / `read_result(..., history=False, now_ms=...)` | 本地读取不新增调用；当前结果与历史分别检查应用读权，当前读取显式提供时间供应用期限判定 |
| `change_application(control_id, command, callback)` | 可信应用按完整不可变 command 修改自身记录，与控制回执共同提交；callback 只能使用传入端口与 command |

`deadline_ms` 是原调用最迟发放时间，不声称远端执行届时停止。时间由可信宿主提供；发放拒绝低于本库上次成功发放时间的值。本原型没有服务时钟同步或供应商超时能力。应用意图的期限可另约束当前采用。

Request 的完整输入含 input_text，不允许改正文后重用同一 Runtime 身份。接入已有应用时，稳定业务输入判重、原先成功登记的派生提示保留及业务回执映射仍由消费适配负责；本原型没有取代原应用的这段逻辑。

## 事务端口与所有权

Runtime 拥有 `rt_*` 表；应用通过 `ApplicationPort` 与 `LocalTransaction` 操作自己拥有的 `app_*` 表。`initialize/register/issue/settle/cancel/read/audit` 在原 SQLite 事务内运行；`audit` 在操作之前和组合写入提交之前双向核对应用关联与 Runtime 不可变事实。缺一侧或账务投影不同即拒绝，不当作空状态恢复；即使没有应用表，也核对持久登记/控制计数与相应行数，阻止单侧行缺失后重新登记或重执行控制命令。应用结果投影应绑定 result_sha256，业务合格与当前许可分别判断。消费者还须校验自己的许可/修订/额度字段；非法元数据不能依赖 Python 真值放行。独立示例要求单一规则行、正整数修订及 0/1 许可，非法记录报 INTEGRITY_FAILURE。旧 r1.store.v1 合成库明确拒绝，不静默升级或重建。

本原型所有写操作使用一个本地 SQLite 文件和 `BEGIN IMMEDIATE` 排序，读取使用一致快照。没有外部配置文件或内容读写，因此没有模拟原应用的文件锁/文件发布。R2/R3 须接续原 model-control 锁序与外部内容、原工作区绑定检查，不能用本轮 SQLite 证据替代它们。

端口限本次回调、线程和连接；退出即失效。只允许受限 SELECT 和应用表读写，初始化另允许应用建表；拒绝 Runtime 表、事务控制、PRAGMA、ATTACH、触发器和用户函数。关闭语句缓存，防止已编译 SQL 绕过 authorizer。语句错误/限制使端口持续失效，后续 SQL 不再执行；SQL 前后及回调出口均检查事务仍在，避免 SQLite 自动回滚后进入自动提交。错误被回调捕获也会使本次事务失败；嵌套 Runtime 调用同样不能被吞掉后提交。读取/audit 端口只读。游标与保护回调的次生清理异常保留本次主错误；两项保护回调均尝试解除。单独清理失败仍报错，且使端口持续失效；解除失败导致回滚不能确认时仍为 unknown。

这些约束用于可信同进程集成，**不是恶意 Python/SQL 或外部 I/O 沙箱**。回调不得访问私有连接、保留句柄、执行网络/文件/Ledger I/O 或长任务。外部准备须在事务外完成，业务端口在事务内复查原引用和资格。回调异常导致双方整体回滚；业务输出不合格应保存不可采用判断，不抛异常阻止真实费用结算。

## 预算、未知和失败

账务由准确逐调用记录推导，应用可以缓存投影，但须在 audit 与结算端口核对同一事实。调用次数发放即累计，输入计本原型 UTF-8 正文字节，输出计宿主报告的累计单位；它们不是 token 定价或货币预算。R2 需要单独绑定 wire/usage 的真实计量语义。

- 未终结：保留本人执行槽；已报告部分用量计入已用，剩余预留为 `max(输出上限 - 已知用量, 0)`。
- 已终结但最终用量未知：只释放本人槽；部分用量不冒充最终用量，剩余预留保持。
- 终结且最终用量确认：费用采用实际数，解除本人未结预留；迟到更高用量继续记账，不截断，重复观察不重扣。
- 超过原输出预留：保留实际数并隔离本原型协调范围的新发放；没有清除隔离或清空 UNKNOWN 的入口。
- 本地取消、进程退出、配置切换或请求换 ID 都不退款或释放原未知执行。只有第一次成功发放的调用者可能得到令牌；发放 COMMIT 丢确认时按原身份查询，也不恢复发送资格。

提交前失败且回滚确认报告 not_committed；已知 COMMIT 成功后的返回故障报告 committed；COMMIT 确认丢失或回滚不能确认报告 unknown。KeyboardInterrupt 等中断仍向上传播，调用方按原身份查询。读取错误为 not_applicable，清理次生异常不覆盖主错误。本轮实际 I/O 断电和磁盘容量故障尚未覆盖。

## 容量和后续边界

ID 为 1–64 个 ASCII 字母/数字/下划线/点/短横线；正文和合成输出各最多 8 KiB；每配置最多 8 目标；最多 256 请求（默认 64），每调用预留逻辑位置为最多 16 个观察修订与 8 个取消回执。普通配置/应用控制总计最多 128 条，耗尽不占用原调用的观察/取消位置。它是有界逻辑记录方案，不保证磁盘空间预分配。内部单条 JSON 解码上限 1 MiB，覆盖最坏转义的完整合法历史组合；8 KiB 正文上限仍按原 UTF-8 字节计算。独占建文件后的故障可能留下未初始化文件供检查，不自动覆盖或删除。

端口单 SQL 最多 8 KiB/64 参数，单参数最多 16 KiB，单次结果最多 256 行/32 KiB，并设 SQLite VM 步数上限。真实响应存储、协议能力/秘密批准、配置文件恢复、列表分页、fanout/多组、远端查询/取消、跨库交接、旧库迁移及安装发行均不在本原型范围；完整 MR/RV/CR 仍按原计划逐项验收。
