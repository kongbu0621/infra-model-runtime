# PR #8 宿主绑定表复核（2026-11-27）

运行与测试源码为 `93f10fe7ebb83b91ee7cad96b334510dcd4cd9bd`。复用公共项目 Python 3.12.3 / Linux aarch64；源码与临时解包 wheel 分别验证，没有更新项目安装或业务部署。

## 反例与修复

[旧实现来源](counterexample-source.json)绑定 `28f710b` 的 `hosted.py`（运行载荷与 `12d6c20` 相同）。[新增回归在旧实现上](regression-before.txt)检出六个失败子例：表丢失或错列误报 `STORAGE_ERROR`，单行丢失或文本 id 误报 `NOT_FOUND`，由视图代替原表或将 id 改为 REAL 1.0 则被接受。额外行原本已拒绝，作为保留行为对照；正常表重开也保留。

现在在读取绑定正文前，核验 `rt_host` 是候选创建的原始表定义，恰有一行，且 id 为 SQLite integer 1；其后仍按规范编码核对完整绑定与摘要。不建立缺失表，不从其他表重建绑定。新布局常量与此前创建语句完全相同，没有改变合法既有候选的建表格式。

七种损坏分别通过既有句柄预算读取和重新打开检查：均返回 `INTEGRITY_FAILURE/not_applicable`，每次拒绝后完整逻辑模式与记录保持。正常接入和重开预算一致。该判定不是防止 Owner 重写整套数据库的认证机制，也不代替文件系统或真实部署故障验收。

## 验证结果

| 检查 | 结果 |
| --- | --- |
| 绑定布局与历史接续源码专项 | [14 项通过](source-tests.txt) |
| 完整公共源码回归 | [182 项通过](source-full.txt)，无失败、错误或跳过 |
| 准确 wheel R3 | [41 项通过](candidate-tests.txt)；[报告](candidate.json)核对 13 个运行文件与源码逐字节一致、wheel RECORD 和所有包内加载来源 |
| 编译 | [50 个 Python 文件](compile.json)，优化级别 0/1/2 与 Python 3.11 语法解析通过；不是实际 Python 3.11 运行 |

本轮重新构建 `infra_model_runtime-0.1.0a2-py3-none-any.whl`，SHA-256 为 `b9be6a09f94f4b3f02f21749cde980aa42e27196bd57bfb77608acd2a0cf2e4a`。使用[独立核验入口](verify_candidate.py)，以原项目 Python `-I -B` 加载该候选；只临时解包，未安装。14/182/41 范围重合，不累计；前轮 178/39 是历史来源。本目录文本日志仅去除行尾空白。

复核覆盖新 UNKNOWN、内容确认丢失、取消、重开及旧累计保持，完整源码还覆盖原 R1/R2。原安装 `9536de7 / 0.1.0a1` 保持；真实模型、生产迁移、未决历史接管、多模型并发、设备断电和其他平台仍未验收。
