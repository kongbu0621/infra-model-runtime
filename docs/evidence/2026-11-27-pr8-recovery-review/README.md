# PR #8 旧累计与新调用恢复补验（2026-11-27）

本轮只增加两个测试方法，测试提交 `1ab7dfd5bbcfc40a3e1fe8781a33e52c6baf70eb`。运行源码保持 `12d6c20a419a6eda17a83762c8ad61737c6af2d4`，见[保持核对](source-preservation.json)。未确认新的运行缺陷。

| 场景 | 实际断言 |
| --- | --- |
| 响应已保存、确认丢失 | 旧累计 2 次/400 输入字节/8 输出单位保持，新调用增加 1 次和实际 wire 字节，另保留 32 输出预留及一个执行槽 |
| 上述故障后取消、改 profile 与重开 | 累计和预留不变；按原响应恢复后只增加真实 2 输出单位、释放本次 32 预留和执行槽；登记重放/重复 run 不再发送或再次写入内容 |
| 新调用 maybe_sent、用量未知 | 新旧累计与 32 输出预留在取消、重开和反复恢复后保持，执行槽继续占用；发送次数仍为一次 |

- [源码接续专项](source-tests.txt)：12 项通过。
- [准确原 wheel 的 R3 检查](candidate-tests.txt) / [包及加载来源](candidate.json)：39 项通过，13 个运行文件与源码逐字节一致。执行入口 [verify_candidate.py](verify_candidate.py) 使用项目 Python `-I -B`，并校验 RECORD 与准确 wheel 摘要。
- [编译](compile.json)：48 文件，优化级别 0/1/2 和 Python 3.11 语法解析通过。实际解释器为原公共项目 Python 3.12.3 / Linux aarch64。

复用原 `infra_model_runtime-0.1.0a2-py3-none-any.whl`，SHA-256 `6b73da173a031a1b8eed753de889403b77119c304ccbfcb188f321a2c8690b74`。没有重新构建或安装。

前轮完整源码 178 项与准确运行来源已核对，按[原记录](../2026-11-27-pr8-review/README.md)复用，不声称本轮重跑；12/39 项存在重合，不相加。真实模型、真实部署、最低 3.11 实际运行、并发及完整 R3 仍待完成。原安装与业务保持。
