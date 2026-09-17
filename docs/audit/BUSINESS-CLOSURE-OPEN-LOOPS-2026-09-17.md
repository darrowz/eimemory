# eimemory 业务闭环未闭合审计报告

| 项目 | 内容 |
| --- | --- |
| 审计日期 | 2026-09-17（Asia/Shanghai / CST+8） |
| 审计对象 | `/workspace/eimemory` |
| git HEAD | `618dd8d403826f2714074b63e058a677eb4dadc0` |
| 版本 | `1.13.14`（`pyproject.toml`） |
| HEAD 说明 | `fix: unblock deploy health probe, quality stats, and diagnostic evolution recall.` |
| 审计性质 | **只读审计 / 不实现产品修复** |
| 参考文档（已对照当前代码复验，非照抄历史 ID） | `eimemory-review/AUDIT-business-loop-2026-09-16.md`、`VERIFY-vs-original-audits.md`、`REGRESSION-FIX-NOTES.md` |
| 审计焦点 | 12 条业务闭环在 1.13.14 + 618dd8d 之后**仍然未闭合 / 半闭合 / 新暴露**的缺口 |

---

## 0. 执行摘要

历史 INT/STO/RET/RSC 等条目在 1.13.12–1.13.14 与 618dd8d 上大多已有针对性修复；本轮**不以历史 ID 清单复读**，而是沿 12 条业务闭环的写路径与 fail-open 位点重新读代码。

**结论（一句话）**：核心安全与检索语义修复大多已落地，但若干**业务成功语义**仍未闭合——尤其是「夜间任务 / CLI 对外宣称成功」与「真实步骤结果」脱节，以及 `create_safety` 未进入宿主创建决策。生产若仍停在 **1.13.11**，应升级到 **1.13.14 / 618dd8d** 以获得健康探针、quality stats、诊断演化召回等修复；但升级后仍**不能**把夜间编排与宿主去重创建视为已闭环。

### 严重度计数（本轮业务闭环口径）

| 严重度 | 含义 | 数量 |
| --- | ---: | ---: |
| P0 | 对外成功语义与真实状态系统性背离，或可导致重复记忆 / 静默跳过治理 | **2** |
| P1 | 主闭环缺关键一环，常态运行会持续产生错误业务判断 | **4** |
| P2 | 半闭合：幂等/可恢复但非原子，或覆盖不全 | **5** |
| P3 | 盲区 / 观测缺口，需补审计而非立刻判开环 | **3** |
| **合计发现** | | **14** |

另有 **Closed and must keep** 条目 10 条（保持回归锁），**Blind spots** 3 条。

### Top 5 仍开放业务闭环

1. **BC-01（P0）** 夜间编排：步骤失败不进入顶层 `ok`，supervisor 硬编码 `ok=True`，CLI `nightly` 恒返回 0  
2. **BC-02（P0）** `create_safety` 仅写入 fusion 解释，OpenClaw 宿主创建路径不消费 → 召回已判 `exists/probable` 仍可再 ingest  
3. **BC-03（P1）** 夜间只跑 `memory_quality_report`，不跑 `repair_memory_quality`；质量修复闭环未进调度  
4. **BC-04（P1）** CLI `ingest` 在 `status=rejected` 时仍 `return 0`（有持久化拒绝轨迹，但进程成功语义错误）  
5. **BC-05（P1）** `run_rule_evolution` 不可用时 `_run_rule_evolution` 返回 `ok: True`（跳过当成功）

### 对仍停在 1.13.11 生产的发布建议

- **建议尽快升级到 1.13.14（含 618dd8d）**：解锁部署健康探针 loopback、quality stats NameError、诊断演化召回 lane 等生产阻断项。  
- **升级后不要宣称「业务全闭环」**：至少先关掉 BC-01/BC-02（夜间成功语义 + 宿主 create_safety 门闩），再放开无人值守夜间/自动创建。  
- **1.13.11 上继续扩容自动化高风险**：缺 618dd8d 修复时，部署 receipt 健康检查、quality CLI、诊断召回均可能误判。

---

## 1. Still-open loops（仍开放）

### BC-01｜夜间任务顶层成功语义与真实步骤脱节（P0）

| 字段 | 内容 |
| --- | --- |
| 闭环 | #6 Nightly/scheduler；#12 success while state diverges |
| 文件 | `eimemory/scheduler/jobs.py`；`eimemory/cli/main.py` |
| 行号 | jobs L29–L43、L67、L140–L141、L227–L229；cli L102–L109、L2649–L2658 |

**机制**

1. `_nightly_step` 设计为「单步失败不中断批次」，但 `run_nightly_jobs` **只把 `roi` 包进 `_nightly_step`**（L67）。知识刷新、intake、rule evolution、学习、L5、质量报告等后续步骤均为直接调用。  
2. 顶层 `report["ok"]` **仅聚合 `step_reports`**（L141）——而 `step_reports` 实际上几乎只有 `roi`。嵌套报告里的 `ok: False` **不翻转**顶层 `ok`。  
3. 成功路径上 `supervisor_summary(..., ok=True)` **硬编码**（L227–L229），与 `report["ok"]` 无关。  
4. CLI `nightly` 打印摘要后 **`return 0`**（cli L2658），不读 `output["ok"]`。

**业务影响**

- 运维/定时器看到 exit 0 + supervisor ok，会认为质量修复、规则演化、学习、治理晋升均已完成。  
- 实际某步 `ok: False` 或异常被外层吃掉时，业务环在「报告成功」下静默断裂。  
- 这是典型的 **success while state diverges**。

**建议闭合条件**

- 所有有业务后果的步骤进入 `_nightly_step`（或等价聚合）。  
- 顶层 `ok = all(step_reports)` **且** 关键子报告 `ok` 参与聚合（显式 allowlist）。  
- `supervisor_summary.ok` 绑定聚合结果。  
- CLI `nightly`：`ok is not True` → 非零退出。

---

### BC-02｜Recall → create_safety → 宿主创建决策未闭合（P0）

| 字段 | 内容 |
| --- | --- |
| 闭环 | #3 Recall → create_safety → host create |
| 文件 | `eimemory/retrieval/engine.py`；`eimemory/adapters/openclaw/hooks.py` |
| 行号 | engine L1391–L1432、L1834–L1870、L1990–L1991；hooks L176–L184、L440–L447 |

**机制**

- Engine 侧已计算权威 `create_safety`（`exists` / `probable` / `unknown`）并写入 fusion 解释（L1391–L1432、L1870）。这是 RET-01 修复面。  
- 全库 `rg create_safety eimemory/adapters`：**零命中**。OpenClaw `on_message_received` / `on_agent_end` 直接 `runtime.memory.ingest(...)`（hooks L176、L440），只靠 idempotency / 显著性启发式，**不读取**此前召回的 `create_safety`。  
- 权威存在性查询异常时 `except Exception: return False`（engine L1990–L1991）→ 无法证明 exists 时偏向 `unknown`，宿主若「未见证据就创建」会 **fail-open 双写**。

**业务影响**

- 召回已判定记忆存在/很可能存在时，宿主仍可创建重复记忆。  
- 业务环「先召回再决定是否创建」在引擎内闭合、在宿主侧断开。

**建议闭合条件**

- 宿主创建前强制读取最近一次同 scope/query 的 `create_safety`：`exists` 禁止创建；`probable` 需显式 force 或合并。  
- 存在性查询失败应升级为 `unavailable`（禁止创建），不得静默 `False`。  
- 契约测试：fusion=`exists` 时 hooks ingest 不得落盘 active 记忆。

---

### BC-03｜夜间质量修复未进调度（P1）

| 字段 | 内容 |
| --- | --- |
| 闭环 | #6 Nightly：quality repair / supersede |
| 文件 | `eimemory/scheduler/jobs.py`；`eimemory/api/evolution.py`；`eimemory/cli/main.py` |
| 行号 | jobs ~L80（仅 `memory_quality_report`）；evolution `repair_memory_quality` ~L948+；cli quality repair 子命令 |

**机制**

- 夜间调用 `memory_quality_report`（只读统计）。  
- `repair_memory_quality(apply=...)` 存在于 API/CLI，但 **scheduler 无调用**。  
- 同轮 supersede 依赖 ingest 路径的 `_supersede_matching_memories`；夜间无独立 supersede/repair 批次。

**业务影响**

- 质量退化、缺 quality 元数据的记忆不会在夜间被修复；闭环停在「看见问题」而非「修好问题」。  
- 与 BC-01 叠加时，报告仍可能显示整体成功。

**建议闭合条件**

- 夜间增加受控 `repair_memory_quality(apply=True)`（或 dry-run + 阈值阈值 apply），结果进入 `step_reports`。  
- 明确 supersede 批次归属（ingest 即时 vs 夜间扫尾）并写入报告计数。

---

### BC-04｜CLI ingest 拒绝仍进程成功（P1）

| 字段 | 内容 |
| --- | --- |
| 闭环 | #1 Capture → quality gate → persist；#12 success divergence |
| 文件 | `eimemory/api/memory.py`；`eimemory/cli/main.py` |
| 行号 | memory L281–L285（拒绝仍 `store.append`）；cli L1608–L1621 |

**机制**

- API 层 EXT-03：`capture_decision == reject` 时 `status=rejected` 并 **持久化**（正确的审计轨迹）。  
- CLI：若 `status == rejected` 仅附加 `warnings`，仍 **`return 0`**（L1621）。

**业务影响**

- 脚本/编排以 exit code 判定「已捕获成功」会误判；拒绝被当成成功摄入。  
- 数据层有拒绝记录（半闭合），控制面成功语义未闭合。

**建议闭合条件**

- `rejected` → 非零退出（如 2），JSON 含 `ok: false` / `capture_decision`。  
- `force_capture` 成功才为 0。

---

### BC-05｜规则演化不可用时 ok=True（跳过当成功）（P1）

| 字段 | 内容 |
| --- | --- |
| 闭环 | #8 Outcome/feedback → rule evolution → shadow/active |
| 文件 | `eimemory/scheduler/jobs.py` |
| 行号 | L1514–L1531（及同文件多处 `*_unavailable` → `ok: True` 模式） |

**机制**

```python
if evolve is None:
    return {"ok": True, ..., "evolution_skipped_reason": "run_rule_evolution_unavailable"}
```

- 反馈→候选→shadow→active 依赖 `runtime.run_rule_evolution`（`getattr(..., "run_rule_evolution", None)`）。入口缺失时仍报成功。  
- 同文件另有多处「执行器不可用 → ok: True」（生产召回评估、部分投影/发现等）；学习路径在 required 时能 `ok: False`，但规则演化默认 fail-open。

**业务影响**

- 结果反馈看似驱动了规则演化，实则整段跳过；shadow/active 晋升停滞却无告警。

**建议闭合条件**

- 业务必需步骤：unavailable → `ok: False`（或 `ok: True` 仅当显式 `optional=True` 且进入 `skipped_optional`）。  
- 顶层聚合必须看见 `evolution_skipped_reason`。

---

### BC-06｜权威存在性查询异常 fail-open（P1）

| 字段 | 内容 |
| --- | --- |
| 闭环 | #3 create_safety；#5 empty vs unavailable |
| 文件 | `eimemory/retrieval/engine.py` |
| 行号 | L1990–L1991 |

**机制**

- `_authoritative_identity_exists` 在任意异常时返回 `False`，调用方解释为「不存在」而非「查不清」。  
- 与 OpenClaw 将 recall 异常标为 `unavailable`（hooks L734–L738）不一致：引擎内存在性检查更偏 fail-open。

**业务影响**

- 存储抖动/锁超时期间可能发出 `create_safety=unknown` 并放行创建，放大重复记忆。

**建议闭合条件**

- 区分 `False`（确认不存在）与错误（`unavailable`）；错误时禁止自动创建。

---

## 2. Partially closed（半闭合 / 残留）

### BC-07｜晋升幂等但非事务原子（P2）

| 字段 | 内容 |
| --- | --- |
| 闭环 | #2 Review/promotion → memory append |
| 文件 | `eimemory/intake/review.py` |
| 行号 | L145–L210 |

**机制**

- INT-12 残留修复：确定性 `mem_{sha256(promoted-from:id)[:24]}` + 先写 pointer 再 `append`，中断可重放。  
- 仍是两步写：`_save(candidate)` 后 `store.append(memory)`，非单事务。

**业务影响**

- 不会再随机双 ID；但窗口期内候选已 `promoted` 而记忆未可见，召回短暂空洞。

**建议闭合条件**

- `mutate_records_atomically` 同时提交候选终态 + 记忆行；或 append-first + CAS 终态。

---

### BC-08｜Persona 文件成功 vs 审计记录 append 分裂（P2）

| 字段 | 内容 |
| --- | --- |
| 闭环 | #7 Persona identity persistence |
| 文件 | `eimemory/persona/store.py` |
| 行号 | L53–L72 |

**机制**

- `atomic_write_json` 写 `persona_state.json` + snapshot（EXT-02 面）后，再 `_append` 反射记录。  
- append 失败时：磁盘身份已新，审计/可召回轨迹缺失。

**业务影响**

- 运行时人格以文件为准可能正确，但诊断/演化车道看不到对应 snapshot 事件。

**建议闭合条件**

- 先写 staging 记录再替换文件，或 append 失败时标记 `persona_audit_gap` 并重试队列。

---

### BC-09｜多臂融合跳过零权重臂后继续接地（P2）

| 字段 | 内容 |
| --- | --- |
| 闭环 | #4 Multi-arm → fusion → grounding → injection |
| 文件 | `eimemory/retrieval/fusion.py`；`eimemory/retrieval/engine.py` |
| 行号 | fusion L80–L119；engine 接地阈值与 `source_reports` |

**机制**

- RET-04：`skipped_components` 已记录。  
- 零权重/缺臂时融合用剩余臂继续；接地层可能在向量臂缺失时仍靠词法/图证据放行。

**业务影响**

- 部分召回质量下降但不标 `unavailable`；宿主当作完整混合召回。

**建议闭合条件**

- 策略要求的必需臂缺失 → `retrieval_status=degraded/unavailable`，并限制 injection。

---

### BC-10｜SQLite lock 合约覆盖不全（P2）

| 字段 | 内容 |
| --- | --- |
| 闭环 | #11 SQLite lock contract vs callers |
| 文件 | `eimemory/storage/sqlite_store.py` |
| 行号 | L219–L234；L3140–L3141；L4895–L4896 |

**机制**

- STO-18：`assert_connection_lock_held` 在 `upsert` / `get_by_id` 上生效。  
- 同文件大量其他写/读路径未见同等断言；依赖 RuntimeStore 包装约定。

**业务影响**

- 绕过 RuntimeStore 的调用不会在所有入口失败；锁失败（busy）对业务层的映射不统一（有的变空结果，有的抛错）。

**建议闭合条件**

- 所有 conn 使用点断言；busy → 明确错误码，禁止吞成空召回。

---

### BC-11｜部署 receipt 健康探针依赖 loopback 例外（P2，残留风险）

| 字段 | 内容 |
| --- | --- |
| 闭环 | #9 Deploy receipt / health / rollback authority |
| 文件 | `eimemory/governance/deployment_receipt.py`；`eimemory/intake/safe_transport.py` |
| 行号 | receipt ~L737 `allow_loopback=True`；safe_transport 默认拒绝 loopback |

**机制**

- 618dd8d 修复：仅部署健康探针允许 loopback，intake SSRF 默认仍拒绝。  
- 权威模型（受信 URL、ledger、rollback ancestor）仍在；若探针误配非受信 URL 会 fail-closed（好）。

**业务影响**

- 闭环基本可用；残留是「健康通过 vs ledger 权威」操作纪律，而非代码开环。  
- 仍建议把 rollback 演练与 receipt `ok` 绑定到发布门禁（流程层）。

**建议闭合条件**

- 发布门禁：receipt.ok、health 身份、rollback_commands 非空（非 bootstrap）三者齐备才标可发布。

---

## 3. Closed and must keep（已闭合且必须保持）

以下为当前树上已闭合、回归测试应锁住的能力（防止「修别处时扳开」）：

| ID | 闭环 | 证据（当前树） | 必须保持的契约 |
| --- | --- | --- | --- |
| CK-01 | #1 拒绝落盘 | `api/memory.py` L281–L285 拒绝仍 `append` | 拒绝必须 durable + 可审计 |
| CK-02 | #1/#2 写入前身份 | ingest 在评分/append 前冻结 `record_id` / semantic_key（EXT-21） | 禁止先写后补 ID |
| CK-03 | #2 晋升幂等 ID | `intake/review.py` `_deterministic_promoted_memory_id` | 禁止随机第二记忆 ID |
| CK-04 | #3 引擎侧 create_safety | `retrieval/engine.py` 权威 exists，不靠 pool-only | 禁止 pool-only → exists |
| CK-05 | #5 宿主空 vs 不可用 | OpenClaw `_run_recall_safely` → `retrieval_status=unavailable` | 禁止失败当 no_evidence |
| CK-06 | #4 关键词臂资格 | `_keyword_component_eligible` 用自身证据（RET-02） | 禁止「无 vector_score 字段」启发式 |
| CK-07 | #4 接地向量 | standalone `dense_vector_score`（RET-03/RET-09） | 禁止缺键当高分 |
| CK-08 | #10 诊断 vs chat | `sqlite_store._allowed_recall_lanes`：`include_report_records` 含 `operational`（618dd8d） | chat 默认仍排除 operational |
| CK-09 | #9 部署健康 | `allow_loopback` 仅 receipt 探针；quality stats import `business_metadata` | 禁止扩大 loopback 到 intake |
| CK-10 | #7 Persona 文件原子写 | `persona/store.py` `atomic_write_json` | 禁止非原子截断写 |

---

## 4. Blind spots（本轮未完全审计）

| ID | 范围 | 为何是盲区 | 建议后续 |
| --- | --- | --- | --- |
| BS-01 | Hermes / eibrain / codex 宿主创建路径 | 已确认 OpenClaw 不消费 `create_safety`；其他适配器未逐行核对是否复用同一缺口 | 对所有 `memory.ingest` 调用点做同一门闩审计 |
| BS-02 | 跨进程 SQLite busy 在负载下的业务表现 | 有 `busy_timeout` 与 maintenance 锁，但 API/CLI 对 `database is locked` 的用户可见语义未系统追踪 | 注入 busy 故障，断言 recall/ingest/nightly 的错误码 |
| BS-03 | Outcome 内容质量 → 规则是否「变好」 | 有 shadow/active 与 promotion_manager，但未评估规则文本/策略是否被反馈真正改写 | 抽样 outcome_trace → 新 rule diff 的端到端回放 |

---

## 5. 按 12 条业务闭环对照表

| # | 业务闭环 | 状态 | 主导发现 |
| ---: | --- | --- | --- |
| 1 | Capture → quality gate → persist（拒绝留痕） | **半闭合** | CK-01 已留痕；BC-04 CLI 成功语义错误 |
| 2 | Review/promotion → memory append | **半闭合** | CK-03 幂等；BC-07 非原子 |
| 3 | Recall → create_safety → host create | **开放** | BC-02、BC-06 |
| 4 | Multi-arm → fusion → grounding → injection | **半闭合** | CK-06/07；BC-09 |
| 5 | Host recall empty vs unavailable | **基本闭合** | CK-05；BC-06 为关联残留 |
| 6 | Nightly quality/supersede/learning/governance | **开放** | BC-01、BC-03、BC-05 |
| 7 | Persona identity persistence | **半闭合** | CK-10；BC-08 |
| 8 | Outcome → rule evolution → shadow/active | **开放** | BC-05（及 skip-as-ok 族） |
| 9 | Deploy receipt / health / rollback | **基本闭合** | CK-09；BC-11 流程残留 |
| 10 | Diagnostic vs chat recall lanes | **闭合（618dd8d）** | CK-08 |
| 11 | SQLite lock vs callers | **半闭合** | BC-10 |
| 12 | CLI/API success while state diverges | **开放** | BC-01、BC-04、BC-05 |

---

## 6. 与历史审计的关系（避免重复劳动）

- `VERIFY-vs-original-audits.md` 称 160 个历史 ID 在 1.13.14 为 FIXED：本轮**抽查**了与 12 闭环相关的修复面（拒绝落盘、确定性晋升、create_safety 引擎侧、unavailable 语义、diagnostic operational lane、deploy loopback、quality stats import），与 FIXED 断言一致。  
- 本轮新增的是 **闭环编排与宿主消费层** 问题：历史条目多在库内函数正确性，未覆盖「夜间 ok 聚合」「CLI exit code」「hooks 是否读取 create_safety」。  
- 因此：**不要**把 VERIFY 的 160 FIXED 解读为「12 条业务闭环已全部闭合」。

---

## 7. 发布 / 部署建议（生产仍停 1.13.11）

| 建议 | 说明 |
| --- | --- |
| **升级** | 部署 **1.13.14 + 618dd8d**（或等价包含三回归修复的构建） |
| **升级理由** | 1.13.11 缺：部署健康探针 loopback、quality stats 修复、诊断演化召回 lane |
| **升级后门禁** | 在关闭 BC-01/BC-02 前，夜间与自动创建不得作为「已治理成功」信号 |
| **监控** | 解析 nightly JSON 的嵌套 `ok`/`evolution_skipped_reason`，**不要**只信 exit code 或 supervisor.ok |
| **回滚** | 继续使用 deployment receipt 的 prior_commit + install 脚本；bootstrap 与非 bootstrap 路径分开验证 |

---

## 8. 统计摘要（供父代理回传）

```
Report: /workspace/eimemory/docs/audit/BUSINESS-CLOSURE-OPEN-LOOPS-2026-09-17.md
        /workspace/eimemory-review/BUSINESS-CLOSURE-OPEN-LOOPS-2026-09-17.md
HEAD:   618dd8d (eimemory 1.13.14)
Counts: P0=2  P1=4  P2=5  P3=3  (open/partial/blind findings = 14)
Top5:   BC-01 nightly success divergence;
        BC-02 create_safety not consumed by host;
        BC-03 quality repair not in nightly;
        BC-04 CLI ingest exit 0 on reject;
        BC-05 rule evolution unavailable => ok True
Deploy: Upgrade prod from 1.13.11 → 1.13.14/618dd8d ASAP;
        do not treat business loops as fully closed until BC-01/BC-02 gated.
```

---

*本报告仅描述当前代码证据与建议闭合条件，不包含产品补丁实现。*
