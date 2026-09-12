# 记忆召回闭环与性能：最小修复验收

日期：2026-09-12。审计基线：`e40ec88`（上一轮业务闭环修复，已推送 GitHub）。
本轮检查召回结果、来源过滤、反馈回写、主动召回缓存及失败恢复；仅修改四个产品文件，无数据库迁移。

## 闭环问题与修复

| 问题 | 复现结果 / 业务影响 | 局部修复与回归证据 |
| --- | --- | --- |
| R1 并发 usage 重试重复加权 | 4 个 Runtime 同时上报同一个 query，产生 4 条反馈，used_count=4、调整值达到 0.30；一次观察应计 1 次、0.08。 | `api/memory.py` 在写事务中按精确 scope、source_id、幂等键重查，兼容历史随机 record_id；反馈与导出 outbox 同事务。覆盖并发、重启、不同来源及共享作用域、导出入队失败回滚后重试。 |
| R2 来源过滤未覆盖 episode 回溯证据 | 主结果遵守 allowed_sources / blocked_sources，但被排除来源的 episode ID、标题仍出现在 cascade_evidence 和 compact evidence。 | 回溯复用显式来源限制；不把默认主结果 lane 限制施加到 episode。覆盖允许、禁止和未指定过滤三种情况，检查完整及 compact 响应。 |
| R3 canonical_first 扩大检索丢失已有命中 | 主作用域事实正文含 PostgreSQL，标题不精确匹配；fallback 执行前清空主候选，旧作用域无结果时最终为空。 | `retrieval/engine.py` 删除清空候选的一行。覆盖旧作用域有 / 无命中两种情况，主作用域事实保留且排首。 |
| R4 主动召回缓存遮住新记忆 / 复用旧排序 | 空查询后写入匹配记忆，新 turn 同查询仍为空；修改已有记忆正文后可能继续按旧评分返回。 | `retrieval/proactive.py` 缓存绑定现有候选源 authority_revision；版本变更或不可用时重新检索。覆盖新增、修改、版本缺失 / 读取失败，以及未变化空缓存继续复用。 |
| R5 临时不可用被持久化成完成的空决策 | 首次检索异常、超时、容量耗尽或真实 relevance gate busy 后，同一 turn 重试走已完成 decision，不再检索。 | 明确的 bypass / unavailable 在持久化前返回可重试空响应，保留诊断。覆盖异常三类及真实 RelevanceAdmission busy→恢复；正常 no_evidence 仍持久幂等。 |

新增回归：

- `tests/test_recall_closure_regressions.py`：6 项。
- `tests/test_recall_canonical_fallback.py`：2 项。
- `tests/test_proactive_recall_recovery.py`：10 项。
- `tests/test_recall_engine.py`：新增 1 项，防止自定义元类通过相等比较冒充内置标量而绕过候选冻结。

## 性能剖析与小规模优化

对 1,500 条合成 SQLite 记忆执行真实 MemoryAPI.recall。初步 cProfile 显示一次召回中有约 9,700 次 typing.Mapping 的运行时检查；候选提示的递归冻结是可减少的 CPU 开销。

`retrieval/contracts.py` 改用 collections.abc.Mapping，并让内置字符串、数字、布尔值和 None 在深度限制检查之后直接返回。容器大小、最大深度、字符串长度、不可变封装以及子类处理保持原有语义。既有候选契约测试检查嵌套结构、恶意迭代器和容量限制。

最终对照在同一进程、同一数据库中交替使用基线和优化后的 Mapping / bounded-freeze 实现，其他召回代码保持一致，以隔离这项优化。脚本显式关闭 PostgreSQL、reranker、lightweight admission 和 caller assistance 可选开关，断言返回非空结果。每种实现先预热 3 次，再测量 20 次；交替执行顺序以减小时间顺序影响。未并行运行其他验收任务。

| 条件 | 结果 |
| --- | --- |
| 环境 | Windows 11，Python 3.14.3，本地 SQLite |
| 数据 | 1,500 条记忆，5 个主题，单一精确 scope / source；仅构造 SQLite 投影 |
| 查询 | Borealis deployment tests before release，limit=6 |
| 基线中位耗时 | 61.867 ms |
| 优化中位耗时 | 57.678 ms |
| 中位耗时降低 | 6.77% |
| 一致性 | 所有轮次的主结果 / 规则顺序、置信度、评分、evidence refs 和 cascade evidence 相同；仅忽略评分溯源的逐次生成时间 |

复现命令（仓库根目录）：

```powershell
python docs/audit/recall-optimization-2026-09-12/benchmark_recall.py --output docs/audit/recall-optimization-2026-09-12/benchmark.json
```

原始逐次耗时及环境记录见 `recall-optimization-2026-09-12/benchmark.json`。该结果仅代表本机合成热缓存样本中候选封装的优化，不能外推生产 p95、PostgreSQL、外部 embedding / reranker 或所有闭环改动的整体性能。

## 验证与边界

最终联合回归 **419 passed in 30.04s**，完整命令与输出记录在 `recall-optimization-2026-09-12/verification.json`。`python -m compileall -q eimemory` 和 `git diff --check` 均通过。

交叉检查及联合回归中发现的两个边界已在最终代码补齐：真实 relevance gate 的 unavailable 响应也可重试；候选缓存失效后重新检索仍保留原有的精确存储校验，避免来源返回的旧对象复活已删除记忆。性能快路径使用类型身份比较，保留不可信对象的冻结边界。

- 仅阻止新的重复 usage 写入；不合并历史已重复反馈。
- 历史已持久化的失败空 decision 未迁移，同一旧 turn 仍使用原决策；新 turn 可重新检索。
- authority_revision 是现有全局记录版本，审计 / 反馈写入也会使缓存失效，可能减少正结果缓存命中。读取版本为 O(1)；本轮优先保证写入后的召回一致性。
- 不改召回权重、相关性阈值、存储格式或线上部署。此次验证为针对受影响模块的回归，不是全仓库或生产验收。
