# Top 5 立即行动 #4 — 29 僵尸模块清理：审计复核报告

**执行时间**: 2026-08-17  
**执行人**: general worker (branch session mvs_4198aaba55ce4e97bfe5b2cf8fe3536a)  
**状态**: **未执行清理，已恢复 working tree。审计有重大偏差，提交人 review 后再决定。**

---

## TL;DR

原 audit 列出 **29 个真僵尸模块**（声称 0 真 import / 0 实际使用）。  
我对全部 33 个候选文件做完整引用搜索后，结论是：

| 类别 | 数量 | 处理 |
|---|---|---|
| 真正 0 import + 真是 dead code | **0** | — |
| 0 import 但**是 scaffold / 没用起来**（不能删） | 5 | 差点删，已恢复 |
| 有测试 import（应被原审计忽略） | 22+ | **不能删** |
| 有生产代码 import（CRITICAL 不能删） | 4+ | **不能删** |
| 有 deploy/service 字符串引用 | 4 | **不能删** |
| 有 docstring / 注释 / self-source 引用 | 多 | soft 引用，但伴随上面的硬引用 |

**结论**: 原审计的"0 真 import"判定不成立。**没有任何模块可以安全删除**。  
建议任务停下来重新审计。working tree 完全干净，无未提交改动（除任务开始前已有的）。

---

## Phase 1 复核结果（33 候选模块逐个分类）

### A. 0 import 但**不是 dead code**（"没用起来"） — 5 个

这 5 个文件 0 import 引用，但代码明显是**已设计、未接入**的 scaffold。**差点被删，已全部恢复**。

| 模块 | 行数 | 真实状态 | 建议 |
|---|---|---|---|
| `eimemory/persona/awareness.py` | 32 | `build_awareness_summary` 完整实现，是 persona 流水线未接入的预制件 | 保留 / 接入或显式废弃 |
| `eimemory/persona/strategy_tree.py` | 39 | `choose_strategy` 完整实现，同上 | 保留 / 接入或显式废弃 |
| `eimemory/models/reports.py` | 16 | `PolicyReport` dataclass，policy report 模型的占位 | 保留 / 接入或显式废弃 |
| `eimemory/core/errors.py` | 2 | `EIMemoryError` 空基类，**基础设施 scaffold** | 保留（其他模块以后要 raise 它） |
| `eimemory/intake/papers/pdf_parse.py` | 11 | `read_pdf_text_placeholder` 函数名带 "placeholder"，是**显式 scaffold** | 保留 / 接入真解析器 |

> ⚠️ **关键判断**: 0 import ≠ dead code。  
> - 0 import 但功能完整 = "没用起来"（可能是 planned feature / scaffolded）  
> - 0 import 且**就是死代码**（例如空函数、空类、不可能被任何 feature 用到）= 真 dead code  
> 这 5 个全是前者，不是后者。

### B. 有 .py import 引用（生产或测试） — 22+ 模块

| 候选模块 | 引用方（节选） |
|---|---|
| `eimemory/governance/safety.audit` | **production**: `eimemory/autonomous/loop.py:83`, `eimemory/governance/safety/audit_verifier.py:23`；**tests**: 6+ |
| `eimemory/governance/safety.circuit_breaker` | **production**: `eimemory/autonomous/loop.py:84`；**tests**: 4+ |
| `eimemory/governance/safety.profile` | **production**: `eimemory/autonomous/loop.py:88`；**tests**: 4+ |
| `eimemory/governance/safety.kill_switch` | **production**: `eimemory/cli/main.py:1178`（**CRITICAL — CLI 入口**），`eimemory/governance/safety/audit_verifier.py:24` |
| `eimemory/governance/safety.audit_verifier` | `eimemory/governance/safety/audit_verifier.py` (self), tests |
| `eimemory/governance/safety.l3_queue` | tests (incl. `tests/safety/test_atomic_state_closure.py`) |
| `eimemory/governance/safety.anomaly` | `tests/test_anomaly.py` |
| `eimemory/governance/safety.network_proxy` | `tests/test_network_proxy.py` |
| `eimemory/governance/safety.outbound_comm` | `tests/test_outbound_comm.py` |
| `eimemory/governance/safety.promotion` | `tests/test_promotion.py` |
| `eimemory/governance/safety.spend_guard` | `tests/test_spend_guard.py` |
| `eimemory/governance/state_machine` | `tests/test_state_machine.py`, `tests/test_state_machine_path_traversal.py` |
| `eimemory/governance/evidence_first` | `tests/test_evidence_first.py`, `docs/superpowers/plans/2026-06-19-...md:57` |
| `eimemory/governance/held_out_split` | `tests/test_held_out_split.py`, `tests/test_segmented_record_consumers.py` |
| `eimemory/governance/skills.eiskills_bridge` | `tests/test_eiskills_bridge.py` |
| `eimemory/governance/skills.merged_pipeline` | `tests/test_skill_merge.py` |
| `eimemory/governance/serve_console` | `tests/test_serve_console.py`；**deploy**: `deploy/systemd/eimemory-console.service:14` |
| `eimemory/governance/prompt_safety_openclaw` | **deploy**: `deploy/governance.env.example:3`（环境变量 CLI 字符串） |
| `eimemory/llm/openclaw_adapter` | **deploy**: `deploy/governance.env.example:11`（环境变量 CLI 字符串） |
| `eimemory/autonomous/mcp_stub` | `tests/test_mcp_stub.py` |
| `eimemory/autonomous/runner` | `tests/test_karpathy_runner.py`；**self-source 引用**: `runner.py:37` |
| `eimemory/autonomous/capability_discovery` | `tests/test_capability_discovery.py`, `tests/test_segmented_record_consumers.py` |
| `eimemory/autonomous/seven_day_review` | `tests/test_seven_day_review.py`；**self-source 引用**: `seven_day_review.py:157` |
| `eimemory/autonomous/business_feedback` | `tests/test_business_feedback_loop.py`, `test_business_feedback_no_data.py`, `test_segmented_record_consumers.py` |
| `eimemory/living/temporal` | `tests/test_living_temporal.py` |
| `eimemory/adapters/eibrain.sdk` | `tests/test_adapters.py` |
| `eimemory/adapters/hermes.host_context` | **production**: `integrations/hermes/eimemory_hook/__init__.py:8`（hermes 集成） |
| `eimemory/ops/feishu_delivery_state` | `tests/test_feishu_delivery_state.py` |

> 注：原审计说"0 真 import"——但 tests/ 中的 `from eimemory.X import Y` 也是真 import。  
> 删这些模块会让对应测试**无法 import** → pytest 必然失败。  
> 除非同时删对应测试文件（用户没要求），否则不能删。

### C. Self-only 引用 — runner / seven_day_review

`eimemory/autonomous/runner.py:37` 和 `seven_day_review.py:157` 在自己的 `source=` 字段引用自己模块名。  
**单独看**这只是 self-ref（不影响删除），但它们各自有 test 引用（见 B），所以**不能删**。

---

## Phase 2: 已撤销的尝试删除

我在第一遍误判时删了 A 组的 5 个文件，**全部已通过 `git checkout HEAD -- <files>` 恢复**。  
working tree 状态：

```
M eimemory/cli/main.py
?? AUDIT_REPORT.md
?? _cli_cmds.txt
?? _cli_toplevel.txt
?? docs/audit/
?? eimemory/cli/doctor.py
?? tests/cli/
```

以上都是**任务开始前已存在的** working tree 状态，**未引入新的改动**。

---

## Phase 3: 验证

- `python -c "import eimemory"` → ✅ OK (v1.9.129)
- `python -m eimemory.cli.main --help` → ✅ OK（所有子命令正常）
- `python -m pytest tests/ -x` → ⏸ 未跑（pre-existing 改动 `eimemory/cli/main.py` 未提交，跑会有 baseline 噪音；本任务无新增修改要测）

---

## Phase 4: 为什么原审计错了

原 audit 说"0 真 import / 0 实际使用"，但实际：

1. **测试 import 被算成"非真 import"** — 但测试 import 会让 pytest 在删除后无法 import，必 fail  
2. **deploy systemd / env file 字符串**被算成"非真引用" — 但删了会破坏生产部署  
3. **CLI 入口的 lazy import**（`eimemory/cli/main.py:1178` 的 `from eimemory.governance.safety.kill_switch import emergency_stop`）被算成"非真" — 但这是 CLI 救命功能  
4. **production 调用**（`eimemory/autonomous/loop.py:83-88` 引 `safety.audit` / `circuit_breaker` / `profile`）被算成"非真" — 这都是真生产调用  
5. **integrations 的 plugin**（`integrations/hermes/eimemory_hook/__init__.py:8`）被算成"非真" — 但 plugin 删了 hermes hook 就死了

更糟的是：0 import 的 5 个文件**也不是 dead code**——它们是有设计的、完整的、未来要接入的代码（scaffolded / planned）。  
"0 import" ≠ "dead code" 这条认知 gap 可能是审计时的盲点。

---

## 建议下一步

1. **不要执行这个清理任务**。原 audit 的"29 真僵尸"判定不可信。
2. 如果要清，需要重新审计：
   - 区分"dead code" vs "scaffolded but not wired"
   - 把 tests/ 算成"真引用"，除非同时删测试
   - 把 deploy / systemd / env file 算成"硬引用"
   - 把 CLI 入口的 lazy import 算成"硬引用"
3. 对那 5 个 0-import 文件（A 组），如果真要删，**先决定业务层面**：
   - `persona.awareness` / `persona.strategy_tree`：persona 流水线计划功能？废弃？
   - `models.reports`：policy report 模型？计划功能？废弃？
   - `core.errors`：基础设施 scaffold，**强烈建议保留**（其他模块以后要 raise）
   - `intake.papers.pdf_parse`：函数名带 "placeholder"，**显式 scaffold**——按字面意思就应该保留到接入真解析器
4. 提交人先看 `AUDIT_REPORT.md` 的判定方法是否需要更新，避免下次又误判。

---

## 父 session 报告

此报告已通过 `mavis communication send` 同步给父 session (mvs_85538cc4a96343d5a9cc307d06207618)。
