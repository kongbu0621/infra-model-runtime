# R1 组合容量与创建入口复核

2026-11-26，继续 PR #5。输入 `b36de0fadfd5e32d1b50442233e6bb47cd89391e`，修复运行源码与测试 `529b99ffa2ebbb1601f3ea4eaa1f62f2d6c516ea`。前轮[事务复核](R1_REVIEW.md)及其 36 项证据保持，不重复累计。

## 发现和修复

| 问题 | 旧实现的实际表现 | 修复与验证 |
| --- | --- | --- |
| 单项合法、历史组合超出内部解码上限 | 8 KiB 输入和结果包含大量 JSON 转义字符时，第 9 次合法观察被报 INTEGRITY_FAILURE；前 8 次仍保留，后续用量不能落账 | 内部单 JSON 上限由 512 KiB 校正至 1 MiB，覆盖完整 16 次观察和 8 次取消。最坏转义、最长 ID/能力集合、上限用量、重开与原回执重放通过 |
| 创建入口未遵守错误封装和预检查 | 已有路径抛 FileExistsError，缺父目录抛 FileNotFoundError；非法应用身份已被拒绝，却先留下空文件，修正后无法按原路径创建 | 应用身份在建文件前校验；排他创建遇已有路径报 CONFLICT，其它创建/初始关闭 I/O 错误报 STORAGE_ERROR，均为 not_committed 并带 create 身份。实际已有库保持，补齐父目录或修正身份后按原路径接续 |

输入/结果的公开 8 KiB 上限、16 次观察、8 次取消及其它额度未放大。JSON 控制字符最坏占原 UTF-8 字节的 6 倍；18 份正文（请求、当前结果、16 份观察结果）上界为 `18 × 8192 × 6 = 884736` 字节，其余为有界 ID、回执和元数据。完整极值组合实测落在 1 MiB 内，不把每项单独通过当作组合通过。

简单独立探针的第 8 次原记录为 494416 字节；修复后完整 16 次为 889734 字节，见[前后测量](evidence/2026-11-26-r1-capacity-review/payload-probe.json)。测试另独立检查解码器恰好 1048576 字节接受、1048577 字节拒绝，避免依靠后续 schema 错误误判容量检测有效。

创建文件以后仍可能因真实存储故障留下未初始化文件；不自动删除它或覆盖已有路径。中断仍按原规则传播。state/store 结构与版本保持 v2，本轮修正同一公开容量合同的实现；旧源码不能可靠读取新写入的大历史，不能仅据 v2 标签推断所有旧读者已兼容。

## 验证

| 检查 | 结果 |
| --- | --- |
| 固定旧版专项 | 5 项：1 失败、3 错误、1 合法拒绝对照通过；[原日志](evidence/2026-11-26-r1-capacity-review/before-tests.log)、[准确来源](evidence/2026-11-26-r1-capacity-review/before-validation.json)和[当时测试快照](evidence/2026-11-26-r1-capacity-review/before-test-source.txt) |
| 修复后全套 | 41 项，0 失败/错误/跳过，含原 36 项与新增 5 项；[日志](evidence/2026-11-26-r1-capacity-review/tests.log) |
| 编译与源码 | 4 运行模块、1 示例、4 测试文件，共 9 文件按 0/1/2 编译；[文件摘要](evidence/2026-11-26-r1-capacity-review/source-manifest.json)、[结果](evidence/2026-11-26-r1-capacity-review/validation.json) |
| 回归与状态守恒 | 原三对真实进程竞争、事务回滚、重入防护、缺失控制回执拒绝继续通过；完整大历史重放后逐表内容与回执不变，用量/预留/隔离逐轴核对 |

复用本项目 Python 3.12.3 / SQLite 3.45.1 / Linux aarch64。仓外显式加载固定源码，只有临时合成库；重现命令仍为：

```bash
"$IMR_PYTHON" -I -B -m unittest discover -s "$IMR_SOURCE/tests" -p 'test_r1*.py' -v
```

[本轮证据清单](evidence/2026-11-26-r1-capacity-review/manifest.json)独立保存，前两轮证据未改。没有安装新包、真实模型、NAS 同步或业务部署；Python 3.11、设备断电、完整故障矩阵与真实效果仍未验。README Mermaid 未改，文档只核链接、表格与图源码保持。继续原 PR，接续顺序 R2 → R3 不变。
