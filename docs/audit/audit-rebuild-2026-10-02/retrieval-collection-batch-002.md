# 从零模块审计 · 候选收集与降级边界第二批

基线：`4763001d1c4f3f4af6e6dda17e008e1b4c9b5609`，v1.14.31。

## 结果

本批 81 个函数已完成两次独立源码检查；复核者先看到候选问题，因此是独立复核，非盲审。累计第一遍 148、双遍 148。存储模块累计 101/722 个函数双遍，检索模块 47/449 个函数双遍；两个大模块均未全部完成。

新增确认 A-STO-003：候选收集因预算耗尽而提前跳过时，如果尚未收集到行，可能被报告为正常空结果。该结果来自源码控制流的双人确认，尚不代表真实数据库/模型环境的运行复现或性能测量。

## 用图导航，用源码证明

图索引仅提供静态定义和导入关系。本轮沿已核实的调用点前进：

1. `GovernedRecallEngine._recall.search_scope_groups`，engine.py:724，调用动态的 `candidate_source.search`；只有配置为 SQLite source 时才走下一项
2. `SQLiteCandidateSource.search` → `_search` → `_search_locked`
3. `_search_locked` 的 sqlite_source.py:256 → `RuntimeStore.search_with_diagnostics`
4. runtime_store.py:1206 → `SqliteRecordStore.search_with_diagnostics`
5. sqlite_store.py:4408 → `_candidate_rows` → FTS、anchor、lane seed、recent collectors
6. 返回报告经 `_batch` 和检索状态规范化消费，必须保留“不完整”信息

`graph-audit-overlay.json` 把 49 个文件和 1,171 个函数的审计状态绑定到原图 node_id，共 1,220 个节点；未审节点明确标记。14 条人工核实的源码调用边与 95 条范围内静态模块导入边分开保存，不代表运行时路径已经执行或语义已验证。缺陷节点另绑定修复、复核、远程提交状态；没有已确认远程 SHA 时保留 null，不能把本地 patch 当成已提交修复。

## 精确双遍范围

| 文件 | 范围 | 函数 |
| --- | --- | ---: |
| storage/sqlite_store.py | 4164–4248、4250–5232、6763–6781 | 34 |
| retrieval/sqlite_source.py | 全文件 1–376 | 16 |
| retrieval/contracts.py | 全文件 1–272 | 18 |
| retrieval/diagnostics.py | 全文件 1–197 | 9 |
| retrieval/stage_diagnostics.py | 全文件 1–101 | 4 |

四个检索文件全文双遍。SQLite 大文件仅上述函数范围；诊断模块对外部 evidence helper 的导入未继续深入，schema helper 的调用也不代表被调用迁移实现已审。累计尚未审：存储 621 函数，检索 402 函数。函数定义包含嵌套 def/async def 和 protocol 方法，不含 class 声明。

## A-STO-003 · P1 · 截止时间导致的空结果失去降级信号

- 图节点：`n9677` `_candidate_rows` → `n9676` `search_with_diagnostics` → `n9421` `_search_result`
- 触发条件：前置 SQL 成功，进入收集阶段时预算已耗尽，尚无候选
- 关键分支：sqlite_store.py:4677、4687、4698、4706、4714 的 deadline 条件跳过后续 collector；4733–4738 因 ordered_keys 为空返回普通零计数报告
- 传播：scorer 的 timeout 检查在逐行循环内部（4426–4429），零行不进入循环；最终报告保持 `recall_index_hybrid` 和空 blocked_counts，RuntimeStore 的结果包装因此不标 degraded
- 用户影响：直接存储检索调用方无法区分“查完没有”与“来不及查”。上层 GovernedRecallEngine 有自己的时间检查，不能把它的额外防线当作底层接口已满足契约

### 修复与测试要求

- 已排在 A-STO-002 后交给修复线；未混入第一批事务修复
- 使用受控 fake clock，在成功的 setup 后推进到 deadline，验证 RuntimeStore 边界仍能观察到不完整状态
- 不加载真实 embedding 模型；如需走 scorer，使用显式替身
- 正常预算内的真实空结果、`empty_exact_scope` 快速空结果仍保持正常
- 已有部分候选/部分评分的既有行为不可被一律清空
- 不只依赖 retrieval_mode：SQLiteCandidateSource 的 identity 分支会改成 `identity_hybrid`，因此可保留的 blocked-count 信号也要验证
- `_batch` 只保留排序后的八个 blocked reasons；新的不完整指示需确认不会被截掉

## 本批未建立的新缺陷

精确 scope/source 引用复核、请求/结果边界、不可变快照与诊断有界化均已检查；没有据此新增已确认问题。这并非召回质量、执行计划、真实 SQL 隔离或全测试套件通过的结论。

下一批沿候选输出 → 融合/排序继续，选择 `retrieval/fusion.py` 和 `retrieval/query_identity.py`，提前给复核者仅提供路径，以便下一次独立检查不先看到候选缺陷。
