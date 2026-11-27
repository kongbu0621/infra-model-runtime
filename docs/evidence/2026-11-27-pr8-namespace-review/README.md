# PR #8 SQLite 名称大小写复核（2026-11-27）

运行/测试源码 `7fe39a22c06dba9891d563bbe6fa668de61e852f`；公共项目 Python 3.12.3 / Linux aarch64。只使用临时合成库、回环 HTTP 和临时解包 wheel，没有安装或部署。

## 已确认问题

SQLite 将 ASCII 大小写不同的对象名视为同一个标识符，但此前的保留前缀检查使用区分大小写的 GLOB / startswith。`RT_Future` 等未知 Runtime 对象能漏过初次接入检查，`Rt_meta` 的冲突直到建表时才发现并误报存储错误；表、视图、索引和触发器均有同类缺口。宿主只读名单中的 `RT_META` 等拼写也未按非法输入提前拒绝。后者是输入校验缺口，本轮没有证明 SQL 端口允许读取私有内容。

[反例来源](counterexample-source.json)固定旧 `01bcea8` 的运行模块（与 `93f10fe` 相同）。[三个新方法在旧实现上](regression-before.txt)得到 8 个失败子例：5 类对象场景、3 个私有表名单别名；相似但不带 `rt_` 前缀的 `RTFuture` 对照原本通过。

现公共接入按 SQLite 的 ASCII 大小写规则识别所有 `rt_*` 对象，写入前返回 `CONFLICT/not_committed`。宿主名单同样拒绝任何大小写的 `rt_` 名称，在进入 guard 前返回 `INVALID_INPUT`。保持下划线为字面字符；不把相似名称误判为保留前缀，也不转换请求 ID、内容、原绑定或其他业务值。

每次拒绝后均独立比较完整逻辑模式和记录；仅删除合成冲突对象后，原输入正常接入且旧预算保持。合法建表格式及公共协议版本未变。

## 验证与准确来源

| 检查 | 本轮结果 |
| --- | --- |
| 名称、绑定、接续专项 | [18 项通过](source-tests.txt) |
| 完整公共源码 | [185 项通过](source-full.txt)，无失败、错误或跳过 |
| 准确候选 wheel R3 | [44 项通过](candidate-tests.txt)，[报告](candidate.json)核对 13 个运行文件与源码、RECORD 及全部加载来源 |
| 编译 | [52 文件](compile.json)，优化级别 0/1/2 与 3.11 语法解析通过；不是实际 Python 3.11 运行 |

重新构建 `infra_model_runtime-0.1.0a2-py3-none-any.whl`，SHA-256 `4c904b11c348609fe0a8a9cad474aeca4822aead81254c7d33b65f1d4df45bc4`。用项目 Python `-I -B` 执行[核验入口](verify_candidate.py)，仅临时解包。18/185/44 范围重合，不累计；前轮 182/41 保留其原来源。本目录文本日志仅去除行尾空白。

原项目安装 `9536de7 / 0.1.0a1`、业务与 NAS 保持。原预算、配置切换、确认丢失、UNKNOWN、取消和原身份恢复均纳入回归；真实模型、生产迁移、未决历史接管、其他平台与完整 R3 仍未验收。
