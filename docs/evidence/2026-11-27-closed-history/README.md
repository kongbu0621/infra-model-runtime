# ClosedHistory 候选验证

2026-11-27；运行与测试源码 `5ed7f066cd9740dff88cfdb42e9ba07fbeffa51d`，Linux aarch64 / Python 3.12.3，使用原 Runtime 项目环境。仅临时合成数据库和回环服务，没有真实模型、升级原安装或业务迁移。

| 检查 | 结果 | 来源 |
| --- | --- | --- |
| 完整源码套件 | 175 项，零失败/错误/跳过 | [日志](source-tests.txt) |
| R3 候选 wheel | 34 项，零失败/错误/跳过 | [日志](candidate-tests.txt)、[摘要/加载来源](candidate.json) |
| 模块来源 | 13 个候选运行文件与本轮源码逐字节相同；wheel SHA 和 RECORD 全部核对 | candidate.json |
| 编译 | 46 个 Python 文件，优化级别 0/1/2；另通过 3.11 语法解析 | [编译及文件摘要](compile.json)；不代表 3.11 实际执行 |

固定候选 wheel：`infra_model_runtime-0.1.0a2-py3-none-any.whl`，SHA-256：

```text
1b4a4b8637a5352ff4fccad6e6f2718ea145d2d94f92a557f02997ba549b199e
```

wheel 的 13 个运行文件与上述源码一致；按这个准确 wheel 摘要复用，不用同名/同版本代替来源核对。实际仅解包，原项目环境仍是此前 R2 安装。

新增 7 方法包含：旧累计跨新调用/取消/恢复/配置切换/重开保持，调用次数/输入/输出三轴拒绝超限，旧请求与控制槽占用、满槽原回执重放，载荷预留不释放，原来源遗漏/改变拒绝，非法类型与数值拒绝。它们是 175/34 套件的子集，不相加统计。既有 R1/R2/R3 其余检查继续通过。

开发中修正了新增绑定在部分内容 I/O 检查中遗漏 `closed_history` 导致的误报 STALE；测试首次也存在 configure 参数顺序错误，已修正。最后回审补齐载荷容量接续后，重新执行上述完整源码与候选检查。没有将初次失败或较早 173 项计作最终结果。

[独立核验器](verify_candidate.py)只用标准库，先验证准确 wheel/RECORD 与源模块，再从解包包加载 R3 测试；不 import 消费仓：

```sh
"$RUNTIME_PYTHON" -I -B docs/evidence/2026-11-27-closed-history/verify_candidate.py \
  --repo "$RUNTIME_CHECKOUT" --wheel "$CANDIDATE_WHEEL" --report "$NEW_REPORT"
```

变量由本机已有项目环境和隔离候选路径提供；报告路径必须尚不存在。普通完整源码检查仍为 `PYTHONPATH=src:tests "$RUNTIME_PYTHON" -B -m unittest discover -s tests -v`。需要本机回环套接字权限，不访问真实供应商。

本轮不验证外部源账是否可信；该证明属于宿主。旧未决请求、真实服务/部署、跨平台、网络文件系统与设备断电未覆盖。完整 R3 继续按[阶段边界](../../R3_CLOSED_HISTORY.md)接续。

最后类型复核将持久 `rt_host` 的宽松字典相等改为规范 JSON 的精确比较，拒绝 False 替代整数 0。新增反例在首轮 wheel 上检出预期失败，见[反例日志](old-type-counterexample.txt)，最终 wheel 与源码通过。首轮 wheel `82f0447e…` 和 174/33 结果保留为开发阶段，不作为本页最终候选。
