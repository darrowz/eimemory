# eimemory 全项目架构及业务闭环审计报告

| 项目 | 内容 |
| --- | --- |
| 审计日期 | 2026-09-22（Asia/Shanghai / CST+8） |
| 审计对象 | `E:\eimemory` |
| 版本 | `1.13.16`（`eimemory/version.py`） |
| git HEAD | `28fecb9` — `fix(recall): keep explicitly requested kinds` |
| 代码规模 | `eimemory/` 约 17.45 万行 Python（378 个 .py） |
| 审计性质 | **只读审计**；未修改任何产品代码；未 commit / push / deploy |
| 审计深度 | 架构分层 + 依赖边界 + 12 条业务闭环端到端贯通 + 实测回归 |
| 参照 | 历史审计 `BUSINESS-CLOSURE-OPEN-LOOPs-2026-09-17`、`REMAINING-ISSUES-2026-09-17`、`POST-DEPLOY-OPEN-2026-09-18` |

---

## 0. 执行摘要

本轮审计**不复读历史 ID 清单**，而是沿当前代码（1.13.16 / `28fecb9`）重新读依赖图、治理门闩与闭环写路径，并以实测回归验证。

**结论（一句话）**：历史审计的 12 条业务闭环（BC-01..BC-11）**均已按契约闭合且有测试锁定**，产品核心安全与检索语义修复扎实；但**架构分层出现系统性侵蚀**（57 处跨平面上向依赖、Data↔Control 真实环），且治理层存在**两处新发现的「评分缺失即放行」缝隙**与**「ledger 失败仍推进状态」的审计轨迹断裂**。

### 严重度计数（本轮口径）

| 严重度 | 含义 | 数量 |
| --- | ---: | ---: |
| P0 | 安全/正确性系统性背离，或可导致未受控状态推进 | **0** |
| P1 | 主闭环存在可被利用的缝隙，或架构约束实质失效 | **4** |
| P2 | 半闭合 / 语义不诚实 / 正确性潜在风险 | **5** |
| P3 | 盲区 / 可维护性 / 观测缺口 | **4** |
| **合计** | | **13** |

### Top 5 发现

1. **ARCH-01（P1）** Data↔Control 真实模块级环：`storage ↔ capabilities` 双向 import（含 `models.records` 反向依赖 Recall 平面）
2. **GOV-01（P1）** `_rollout_gate` 对 L0/L1 的 `safety`/`regression` 评分**缺失时默认 1.0**（满分放行），L2+ 才 fail-closed
3. **GOV-02（P1）** `_record_candidate_lifecycle` 返回值**从不检查**：ledger 写入失败仍执行副作用并 `rewrite` 候选状态 → **决策发生但未落账**
4. **SCH-01（P1）** nightly 多处把「执行器不可用/结构缺失」规范化为 `ok:True`（含 `recall_quality_gate` 缺失即通过）
5. **SCORE-01（P2）** 证据缺失时置信度默认 **0.8**（`evidence_gate`）与评估器基线 **0.62**（`evaluator`）→ 「无证据当高分」倾向

---

## 1. 架构分层审计

### 1.1 声明的分层模型

README 声明四平面：Data → Recall → Control → Integration，隐含依赖方向应自上而下（下层不依赖上层）。

### 1.2 实测依赖图（AST 静态分析）

对 `eimemory/` 全量 AST 解析 `ImportFrom`，按平面归并，测得**跨平面上向依赖**：

| 方向（违规） | 模块数 |
| --- | ---: |
| Control → Integration | 33 |
| **Data → Recall** | **10** |
| **Data → Control** | **6** |
| Recall → Control | 4 |
| Recall → Integration | 3 |
| Data → Integration | 1 |
| **合计** | **57** |

### ARCH-01｜Data 平面反向依赖，构成真实模块级环（P1）

**证据**

```python
# eimemory/storage/runtime_store.py:24
from eimemory.capabilities.models import AdapterCapabilityAdvertisement
# eimemory/storage/capability_store.py:18,23
from eimemory.capabilities.contracts import (...)
from eimemory.capabilities.models import (...)
# eimemory/storage/sqlite_store.py:59,60,74,75
from eimemory.governance.tool_receipts import MAX_ELIGIBLE_RECEIPTS_PER_RUN
from eimemory.governance.policy_rollout import (...)
from eimemory.governance.outcome_evidence import outcome_evidence
from eimemory.scoring import ScoreContext, evaluate_recall_score, ...
# eimemory/models/records.py:13
from eimemory.scoring import ScoreContext, evaluate_memory_score, ...
```

**反向也成立**（形成环）：

```python
# eimemory/capabilities/registry.py:29,35
from eimemory.storage.capability_store import (...)
from eimemory.storage.runtime_store import RuntimeStore
```

**机制**：`storage → capabilities → storage` 是模块级双向环。Python 能容忍（运行时可解析），但导致：
- import 顺序敏感、易触发部分初始化（partial init）错误；
- 契约类型（`AdapterCapabilityAdvertisement`）无法下沉，架构意图失效；
- 测试与打包难以按平面隔离。

**影响**：非现网故障，但违反项目自述的「四平面」契约，且随功能增长会持续恶化。评估为**架构债 P1**（约束实质失效）。

**建议**：将跨平面共享契约下沉到 `models/`（无内部依赖），`storage` 只依赖 `models`；`inline_digest_repair` 的 channel 归一化改为入参注入。

### ARCH-02｜Recall 平面反向依赖 Integration（P3）

```python
# eimemory/retrieval/vector_sync_worker.py:8
from eimemory.api.runtime import Runtime
```

`engine.py:1114/2198` 亦存在函数级 `from eimemory.api.*` 延迟 import（作者已刻意规避导入期耦合），但设计上仍为反向耦合。**建议**由调用方注入 Protocol 限定的 runtime 接口。

---

## 2. 治理与门禁审计

### GOV-01｜`_rollout_gate` 评分缺失默认满分放行 L0/L1（P1）

**文件**：`eimemory/governance/promotion_manager.py:723-726`

```python
if _score_value(scores, "safety", default=1.0 if tier in {"L0","L1"} else 0.0) < (0.95 if tier=="L2" else SAFETY_THRESHOLD):
    blocked.append("safety_gate")
if _score_value(scores, "regression", default=1.0 if tier in {"L0","L1"} else 0.0) < ...:
    blocked.append("regression_gate")
```

**机制**：L0/L1 候选若 `eval_result.scores` **缺失** `safety`/`regression` 字段，默认取 **1.0（满分）** 直接过门；L2+ 默认 0.0（fail-closed）。`tier` 源自候选自报字段 `candidate.meta["authority_tier"]`（`promote_candidate:253`）。

**影响**：形成「**评估缺字段 → 门禁放行**」缝隙。虽 L0/L1 受 `CODE_ASSET_TARGETS` 之外的额外门闩限制，仍绕过 safety/regression 基线。

**建议**：所有 tier 的 safety/regression 缺失一律视为 0.0；如需豁免，改由部署侧机器策略显式声明减免清单，而非硬编码 default。

> **后续深入核查（2026-09-22 补充）**——本项的根因位点应修正：
> 1. **主风险位点在上游**：`eimemory/governance/learning_eval.py:35,37` 在 `evaluate_candidate` 中主动注入 `safety`/`regression` 默认 **1.0**，故 `_rollout_gate` 的 `default=1.0` 很少真正触发。修复须**两者同改**。
> 2. **生产主路径已有缓解**：`autonomous_learning.py:1193` 设 `requires_measured_gates=True`，配合 `compute_verdict:118-119`，无实测门即 `verdict=fail`。
> 3. **风险面收窄**：仅影响绕过 `autonomous_learning` 的直接调用点（`cli/main.py:1048,2314`、`api/runtime.py:397`、`autonomous_evolution.py:779,1083`）。
> 4. **严重度维持 P1**，但依据从「系统级放行」修正为「**绕过主路径时不安全**」。
>
> 详见 `docs/audit/REMEDIATION-DESIGN-2026-09-22.md` 第 0、1 节。

### GOV-02｜ledger 写入失败仍推进状态（P1）

**文件**：`eimemory/governance/promotion_manager.py:386-414`；`rollout_lifecycle.py:169-170`

```python
# promotion_manager.py:386 —— 返回值未检查
_record_candidate_lifecycle(runtime, candidate, scope=scope, action_type="gate_passed", ...)
side_effect = _apply_candidate(...)
...
candidate.status = post_promotion_status          # 状态已推进
runtime.store.rewrite(candidate)
```

```python
# rollout_lifecycle.py:169
if not callable(record_ledger):
    return {"ok": False, "error": "rollout_ledger_unavailable"}
```

**机制**：`_record_candidate_lifecycle` 在 `record_ledger` 不可用时返回 `{"ok": False}`，但 `promote_candidate` **不检查该返回值**，仍执行副作用并将候选置为 promoted 并 `rewrite`。全文件 20+ 处调用点（218/254/264/295/336/340/357/386/409…）**均未检查返回值**。

**影响**：构成「**决策发生、状态推进，但 ledger 无记录**」的审计轨迹断裂窗口。与产品自述的「fail-closed + 不可篡改审计」承诺不符。

**建议**：将 ledger 写入作为 promote/rollback 的前置事务条件——失败时中止副作用并 fail-closed 返回；或引入「先落账、后生效」两阶段提交。

### GOV-03｜自动提交/部署默认关闭（保持，非缺陷）

`code_automation_policy.py` 严格校验环境策略、kill-switch、无符号链接、mode 0600、owner、时间窗、`max_transactions==1`；`machine_policy_context_from_mapping` 明确不授予权限。**此项设计正确，必须保持**。

---

## 3. 调度与成功语义审计

### SCH-01｜nightly「不可用/缺失」规范化为成功（P1）

**文件**：`eimemory/scheduler/jobs.py:167-180`、`34-48`、`470-492`

```python
# L167-180
if isinstance(quality_report, dict) and "ok" not in quality_report:
    quality_report = {**quality_report, "ok": True}
if isinstance(quality_repair_report, dict) and "ok" not in quality_repair_report:
    quality_repair_report = {**quality_repair_report, "ok": True}
```

```python
# L470-492 —— 质量门缺失时构造 ok:True + skipped_reason="recall_quality_unavailable"
"recall_quality_gate": production_recall_report.get("quality_gate") or (
    {"ok": True, ..., "skipped_reason": str(... or "recall_quality_unavailable"), ...}
    if bool(production_recall_report.get("ok", True)) else {"ok": False, ...}
),
```

```python
# L34-48 _nightly_step —— 非 dict 结果默认 ok=True
result = fn()
ok = True
if isinstance(result, dict) and result.get("ok") is False:
    ok = False
```

**机制**：三种背离模式并存：
1. 步骤报告**缺 `"ok"` 键** → 补 `ok:True`（执行器返回空/异常结构被当成功）；
2. `recall_quality_gate` **报告缺失** → 构造 `ok:True` + `skipped_reason="recall_quality_unavailable"`（质量门「不可用」等价「通过」）；
3. `_nightly_step` 对**非 dict 返回值**默认 `ok=True`。

**影响**：BC-01 的顶层聚合（`_aggregate_nightly_ok:97-114`）已正确绑定 `step_reports` 与 allowlist，**但上述三处会在聚合之前污染输入**，使顶层仍可能报 `ok:True` 而质量门/质量修复实际未执行。

**建议**：缺 `ok` 键应为 unknown 并阻断聚合；质量门不可用默认 `ok:False`，仅在 `_quality_wait_is_non_actionable`（L88-94）白名单时豁免；非 dict 返回值视为失败。

### SCH-02｜`run_code_sandbox` 无条件 `ok:True`（P3）

**文件**：`eimemory/governance/code_evolution.py:79-84`

即使 `category != "code_fixable"`（未生成 sandbox_plan/worktree），仍报 `ok:True`。报告语义混淆「已产出计划」与「沙箱可执行」。沙箱本身不 commit/push/deploy，风险有限。**建议**引入 `status: planned/not_applicable`。

---

## 4. 检索与评分审计

### SCORE-01｜证据缺失默认高分倾向（P2）

**文件**：`eimemory/knowledge/evidence_gate.py:23-27`、`eimemory/scoring/evaluator.py:164`

```python
# evidence_gate.py —— 缺 confidence 字段默认 0.8
confidence = _float(_deep(payload,"content","confidence"), _deep(payload,"meta","confidence"), default=0.8)
...
tier = "T2" if confidence >= 0.8 else ("T3" if confidence >= 0.5 else "T5")   # :39
```

```python
# evaluator.py:164 —— 无证据即给 0.62 基线下限
confidence = clamp_score(0.62 + source_bonus - min(0.24, uncertain_hits * 0.08))
```

**机制**：research 证据门在缺失 `confidence` 时默认 **0.8**，直接落入 T2「高可信」；评估器对无证据记录直接给 0.62 基线。两者共同构成「缺证据当高分」倾向。

**影响**：虽 `filter_answer_evidence` 的 `reasons`（missing_source/missing_date/conflict）会拦截部分场景，但 `tier` 与 `confidence` 仍被下游排序/展示消费，可能污染 recall 排序与 capture 决策。

**建议**：缺失 confidence 按最低档（≤0.3）或标 `unknown` 并强制降档；评估器基线改从 0 起算。

### RET-01｜热路径 N+1 精确查询（P2）

**文件**：`eimemory/retrieval/engine.py:2395`（`_record_is_unchanged`）经 `validate` 回调在 `lightweight_admission.py:80` 的 per-item 循环中逐条触发

同文件 `engine.py:2345` 已实现 `_hydrate_records_batch` 批量版本，但该路径**未复用**。候选上限越大延迟越放大。**建议** validate 改为接收批量 refs。

### RET-02｜`id(item)` 作字典键的隐性风险（P3）

**文件**：`eimemory/retrieval/engine.py:1292,1300-1384`

```python
record_key_by_id = {id(item): self._record_key(item) for item in pre_pool_items}
... component_hints_by_ref.get(record_key_by_id[id(item)]) ...
```

`id()` 非唯一标识，依赖对象存活期与地址不复用。当前对象在同函数内保活，属 **latent bug**（潜在错误 grounding）。**建议**直接以 `self._record_key(item)` 为键。

### 确认无损项（检索平面）

- **fusion 异常处理**：未发现 fail-open；仅对数值做 bounded 兜底，无吞异常返回空结果。
- **deadline 处理**：`engine.py:709/762`、`lightweight_admission.py:96-101` 均 fail-closed（标 `unavailable`/drop），未把超时当空结果——**设计正确**。
- **缓存无界增长**：未发现；`proactive.py:478`、`postgres_vector.py:2069` 均带 maxsize LRU。

---

## 5. 并发与锁合约审计

### LOCK-01｜`assert_connection_lock_held` 覆盖严重不足（P2）

**文件**：`eimemory/storage/sqlite_store.py:230-249`（定义）、全文件 571 处 `.execute()`，**仅 12 处**调用断言

```python
# :213
self.conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
# :236-238 —— lock 未绑定时静默放行
lock = getattr(self, "_runtime_lock", None)
if lock is None:
    return          # 未绑定即完全跳过校验
```

**机制**：`check_same_thread=False` 意味着 RLock 是唯一并发保护。多数写路径无断言；且 `RuntimeStore.append`（`runtime_store.py:88`）直接 `self.sqlite.conn.execute("BEGIN IMMEDIATE")`，`postgres_sync.py`、`incremental_sync.py`、`memory_projection_authority.py` 均绕过 guard 直接用裸 conn。未绑定锁时**静默放行**（应 fail-closed）。

**影响**：绕过 RuntimeStore 的调用不会在所有入口失败；busy 对业务层的映射不统一（有的变空结果，有的抛错）。属半闭合（P2）。

**建议**：把断言下沉到唯一封装入口 `execute/commit`，禁止裸 conn 暴露；未绑定锁 fail-closed。

---

## 6. 12 条业务闭环对照（1.13.16 实测）

| # | 业务闭环 | 状态 | 证据 |
| ---: | --- | --- | --- |
| 1 | Capture → quality gate → persist | **已闭合** | CLI `ingest` 拒绝 → `return 2`（`cli/main.py:1625-1636`） |
| 2 | Review/promotion → memory append | **已闭合** | 确定性晋升 ID + 幂等；BC-07 残留非原子（P2 级，未升级） |
| 3 | Recall → create_safety → host create | **已闭合** | 新增 `adapters/create_safety_gate.py`；四宿主统一经 `gate_host_create` |
| 4 | Multi-arm → fusion → grounding → injection | **基本闭合** | RET-02 残留（latent） |
| 5 | Host recall empty vs unavailable | **已闭合** | `unavailable` 语义已入 admission（`lightweight_admission.py:97,100,194`） |
| 6 | Nightly quality/supersede/learning/governance | **半闭合** | BC-01/03 聚合已修；SCH-01 三处仍可污染输入 |
| 7 | Persona identity persistence | **基本闭合** | `atomic_write_json`；BC-08 append 分裂未升级 |
| 8 | Outcome → rule evolution → shadow/active | **已闭合** | `_run_rule_evolution` 不可用 → `ok:False`（`jobs.py:1867-1877`） |
| 9 | Deploy receipt / health / rollback | **已闭合** | loopback 仅限 receipt 探针；`collect_release_health.py` 退出码 0/1/2 |
| 10 | Diagnostic vs chat recall lanes | **已闭合** | `include_report_records` 含 `operational` |
| 11 | SQLite lock vs callers | **半闭合** | LOCK-01（覆盖不足） |
| 12 | CLI/API success while state diverges | **半闭合** | SCH-01 / GOV-02 |

**对比 2026-09-17 报告**：原 2×P0（BC-01 nightly、BC-02 create_safety）+ 4×P1 已全部闭合，且有 `tests/test_business_closure_bc.py`（17 用例）锁定。

---

## 7. 实测回归结果

**环境**：托管 Python 3.13.12 + `pip install -e .`（editable，使 catalog entry-points 可解析）

| 测试集 | 结果 |
| --- | --- |
| `tests/test_business_closure_bc.py`（BC-01..BC-11 契约锁） | **17 passed** |
| 治理/证据/检索/夜间宽集（`-k "promotion or governance or evidence or evaluator or scoring or recall_fusion or safety or nightly or create_safety"`） | **815 passed / 14 failed / 4 skipped** |

### 14 项失败分类（**均非产品缺陷**）

| 分类 | 数量 | 代表 | 判定 |
| --- | ---: | --- | --- |
| **Windows/POSIX 环境** | 6 | `test_private_proposals_are_exclusive_and_mode_0600`（`0o600` vs `0o666`）；`test_release_closure_summary_cli_...`（GBK 控制台解码）；`test_code_preflight_rejects_symlinked_update_target` | Windows 无 POSIX 权限/符号链接语义 |
| **语义硬化后测试未对齐** | 7 | `test_unanswerable_high_similarity_is_no_evidence`（预期 `no_evidence`，实返 `unavailable`，源自 WIP 提交 `e0fcdaa`）；`test_cli_nightly`（预期 0，实返 1——**这正是修复后的正确行为**） | 测试滞后于 fail-closed 硬化 |
| **fixture/env 未配置** | 1 | `test_release_refresh_feeds_nightly_exact_v2_incubation_receipts`（`production_recall_dataset_unconfigured`） | 需 sealed catalog / 数据集 |

> **关键澄清**：`test_code_preflight_rejects_symlinked_update_target` 在 Windows 上失败系 `os.symlink` 未真正创建链接（`os.listdir` 为空、`lstat` FileNotFoundError），触发 `_has_symlink_component`（`promotion_manager.py:3006-3009`）对 `FileNotFoundError` 的 `continue` 分支。**POSIX 生产平台上守卫正常**。但该 `continue` 分支意味着「无法解析的条目」被当作「非符号链接」——建议补一条 `lexists` 校验作为纵深防御。

---

## 8. 发布建议

| 项 | 说明 |
| --- | --- |
| **可直接发布** | 1.13.16 无 P0；BC 闭环已闭合且有测试锁定；GOV-03 自动提交/部署默认关闭且 fail-closed |
| **发布前建议处置** | GOV-01、GOV-02（同为治理层，且与 SCH-01 构成「评估缺失 → 门禁放行 → 无账推进」完整链路，建议一并修） |
| **监控** | 解析 nightly JSON 的嵌套 `ok` 与 `skipped_reason`，**不要**只信 exit code 或 `supervisor_summary.ok` |
| **测试维护** | 对齐 7 项「语义硬化后滞后」用例；为 POSIX-only 用例补 `skipif(sys.platform!='linux')` 以消除跨平台噪声 |
| **架构债** | 单独排期处理 ARCH-01 的 Data↔Control 环（建议先下沉共享契约到 `models/`） |

---

## 9. 盲区（本轮未完全审计）

| ID | 范围 | 建议后续 |
| --- | --- | --- |
| BS-01 | 跨进程 SQLite busy 在负载下的业务表现 | 注入 busy 故障，断言 recall/ingest/nightly 错误码一致性 |
| BS-02 | Outcome 内容质量 → 规则是否「变好」 | 抽样 outcome_trace → 新 rule diff 端到端回放 |
| BS-03 | 四宿主适配器在真实宿主下的端到端 lifecycle | 生产环境 replay 已部分覆盖；补 codex/openclaw 独立验证 |
| BS-04 | 冷路径（CLI 子命令全量）成功语义 | 系统性扫描所有 CLI `return 0` 位点 |

---

## 10. 统计摘要（供回传）

```
Report: docs/audit/ARCH-BC-AUDIT-2026-09-22.md
HEAD:   28fecb9 (eimemory 1.13.16)
Counts: P0=0  P1=4  P2=5  P3=4  (合计 13)
Top5:   ARCH-01 Data<->Control import cycle;
        GOV-01 L0/L1 safety/regression default 1.0;
        GOV-02 ledger failure still advances state;
        SCH-01 nightly unavailable -> ok:True;
        SCORE-01 missing-evidence default confidence 0.8
Tests:  BC contracts 17 passed; broad gov/recall set 815 passed / 14 failed (all non-product)
BC:     BC-01..BC-11 all closed vs 2026-09-17 report (was 2xP0 + 4xP1)
Verdict: no P0; releaseable; fix GOV-01/02 + SCH-01 before unattended automation
```

---

*本报告仅描述当前代码证据与建议闭合条件，不包含产品补丁实现。*
