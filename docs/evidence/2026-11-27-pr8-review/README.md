# PR #8 复核证据（2026-11-27）

运行/测试源码 `12d6c20a419a6eda17a83762c8ad61737c6af2d4`；复用公共项目原 Python 3.12.3，Linux aarch64。候选只临时解包，未安装。

- 修复前来源 `b9c9cc12896614f05eb2a9fdfde4ebab9aaa29f9`（运行载荷 `5ed7f06`）；[反例](before-pool.txt)证明首条新请求前预算池改名被错误允许。累计数字仍存在，问题是历史归属约束被绕过。
- [源码完整检查](source-tests.txt)：178 项通过，无失败、错误或跳过。
- [准确 wheel 检查](candidate-tests.txt) / [来源报告](candidate.json)：R3 37 项通过，13 个运行文件与源码逐字节相同，RECORD 与实际加载来源通过。执行入口为 [verify_candidate.py](verify_candidate.py)，必须使用项目 Python `-I -B`。
- [编译报告](compile.json)：47 个 Python 文件，优化级别 0/1/2；3.11 只做语法解析，未使用 3.11 解释器运行。

准确候选文件名 `infra_model_runtime-0.1.0a2-py3-none-any.whl`；SHA-256：

```text
6b73da173a031a1b8eed753de889403b77119c304ccbfcb188f321a2c8690b74
```

本轮源码与解包结果分列，37 项属于 R3 子集，不与 178 项累计。前轮证据不覆盖本轮修复，但保持原件。没有真实模型、真实工作区迁移、安装更新或服务切换；完整 R3 与生产资格未声明完成。
