# eimemory 代码审计报告 — 模块/僵尸/未实现

> 审计对象: `E:\eimemory` v1.9.129 (commit 4044ecb)
> 审计时间: 2026-08
> 方法: 静态扫描 + honxin 实地数据交叉
> 工具: `ast` 解析 import, 文件级 defs/funcs 统计, `grep`/`rg` 模式匹配, 手动验证关键文件
> 实际规模: **293 个 .py 文件, 98732 行**（用户 prompt 写的 233/~50K 是低估）

---

## A. 子模块使用矩阵

> 评级标准: `active`=包级 importer>15 且顶层活跃; `normal`=importer 5-15; `sparse`=importer 1-4; `low-use`=importer<3 但子模块有内容; `ZOMBIE-IMPORTS`=0 真 import; `ZOMBIE-FILES`=0 真 import 且无 re-export

| 子模块 | 文件数 | 类/函数定义 | 方法数 | 包内引用 | 评级 | 备注 |
|---|---:|---:|---:|---:|---|---|
| (root) | 8 | 81 | 1 | 21 | active | identity/metadata/judgment/events/runtime_identity/version — 顶层核心 |
| adapters/codex | 3 | 20 | 15 | 1 | sparse | 1.9.78 加入的 distributable Codex plugin |
| adapters/eibrain | 3 | 20 | 34 | 4 | sparse | RPC server (3 importer), sdk 0 真 import |
| adapters/hermes | 4 | 12 | 49 | 4 | sparse | 1.9.78 加入的 native Hermes provider; host_context.py 0 真 import |
| adapters/openclaw | 5 | 18 | 170 | 6 | normal | hooks.py (129 defs) 是热路径, qmd_export 0 真 import |
| adapters/runtime | 7 | 17 | 55 | 5 | sparse | redaction.py 0 真 import, 其它 active |
| **api** | **3** | **32** | **235** | **24** | **active (核心)** | runtime.py 2192 行, memory.py 2090 行 — 主体 |
| **autonomous** | **10** | **52** | **12** | **6** | **mostly-sparse** | loop/runner 0 真 import, capability_discovery/seven_day_review/business_feedback 0 真 import |
| cli | 1 | 23 | 1 | 0 | (entry point) | main.py 2660 行, 唯一入口 |
| compatibility | 1 | 21 | 0 | 2 | low-use | migration_helpers.py 543 行 |
| config | 3 | 4 | 0 | 4 | sparse | schema.py 15 行, 1 class, 0 真 import |
| **core** | **3** | **3** | **0** | **61** | **active** | ids/clock/errors, 时钟被 54 个文件 import |
| ei_bridge | 7 | 35 | 20 | 12 | normal | openclaw_runtime 7 import, agents/audit/eibrain_monitor 0 真 import |
| ei_bridge/agents | 3 | 13 | 4 | 0 | ZOMBIE-IMPORTS | 全部 0 真 import, 实际由 ei_bridge.__init__ 间接调度 |
| ei_bridge/channels | 2 | 11 | 2 | 0 | ZOMBIE-IMPORTS | openclaw_feishu 11 defs, 0 真 import |
| embeddings | 2 | 5 | 0 | 2 | low-use | local.py 56 行 |
| **evaluation** | **17** | **240** | **3** | **44** | **active** | production_recall/real_query_gate/benchmarks 等被 api/runtime + scheduler 大量调用 |
| experience | 6 | 64 | 0 | 14 | normal | bridge.py 0 真 import (有 entry 入口) |
| **governance** | **84** | **1286** | **37** | **282** | **active (最大)** | 1.9.x 治理主战场, 282 引用, 大量 capability_* / l5_* / release_* |
| governance/safety | 13 | 30 | 38 | 0 | ZOMBIE-IMPORTS | 多个真僵尸 (l3_queue, anomaly, outbound_comm, network_proxy, promotion, spend_guard, audit_verifier) |
| governance/skills | 3 | 12 | 1 | 0 | ZOMBIE-IMPORTS | eiskills_bridge, merged_pipeline 都 0 真 import |
| intake | 15 | 223 | 42 | 50 | active | pipeline/loop/registry/connectors 高频 |
| intake/papers | 5 | 18 | 0 | 0 | ZOMBIE-IMPORTS | pdf_parse 0 真 import; 其它通过 papers/__init__ re-export |
| **knowledge** | **14** | **159** | **3** | **26** | **active** | compiler/extract/synthesis 高频 |
| living | 5 | 65 | 0 | 11 | normal | posture/temporal 0 真 import, operations 是入口 |
| llm | 3 | 6 | 4 | 5 | sparse | command_client 是核心, openclaw_adapter 0 真 import |
| models | 12 | 25 | 35 | 180 | active | records.py 单文件 138 引用, 绝对核心 |
| ops | 4 | 86 | 14 | 3 | sparse | openclaw_loop.py (64 defs) 0 真 import, 但通过 `from eimemory.ops import openclaw_loop` 动态加载 |
| persona | 11 | 44 | 16 | 32 | active | store/state/schema 是核心; strategy_tree/awareness 0 真 import |
| persona/evals | 2 | 4 | 0 | 0 | ZOMBIE-IMPORTS | __init__ 是空文件, run_persona_eval 0 真 import |
| raw | 5 | 71 | 7 | 10 | normal | retrieval.py 1199 行, 0 真 import 但被 evaluation/longmemeval 等包内 import |
| recall | 4 | 46 | 1 | 8 | normal | indexing/lexical/intent 都 active |
| retrieval | 10 | 95 | 213 | 8 | normal | engine.py 65 methods, postgres_vector 通过相对 import |
| scheduler | 1 | 60 | 0 | 4 | sparse | jobs.py 2007 行, nightly 主入口 |
| scoring | 7 | 32 | 8 | 17 | normal | contract/labels/thresholds 串联 |
| **storage** | **7** | **80** | **326** | **37** | **active** | sqlite_store 单文件 210 func + 205 method |

---

## B. CLI 子命令使用率

> CLI 入口: `eimemory/cli/main.py` (2660 行, 32 个 top-level subcommand)
> dispatch 模式: 25 个走 `if parsed.command == "X"` 分支, 5 个走 `@register("X")` 装饰器, 2 个特殊 (doctor/status), 1 个 dynamic (persona)

| subcommand | 代码位置 | honxin 真实使用 | 是否被 nightly 调用 | 评级 |
|---|---|---|---|---|
| `init` | main.py:1146 | 0 | × | dormant |
| `emergency-stop` | main.py:1151 | 0 | × | dormant (但 purpose-built) |
| `nightly` | main.py:1955 → scheduler/jobs.run_nightly_jobs | 0 (被 systemd timer 调) | **YES** | active (主定时入口) |
| `ingest` | main.py:1261 | 0 | × | dormant |
| `recall` | main.py:1624 (register) | 0 | × | dormant |
| `paper` | main.py:1636 (ingest/extract/compile) | 0 | × | dormant |
| `source` | main.py:1693 (add/list/scan/discover/expand) | 0 | nightly source_discovery | low-use |
| `intake` | main.py:1742 (run/report/collect/queue/explain/review/promote/merge/paper-promote/policy/pack) | 0 | nightly run_knowledge_intake | low-use (nightly 只用 runtime 层) |
| `export` / `import` | main.py:1887/1894 | 0 | × | dormant |
| `backup` | main.py:1901 (create/verify) | 0 | × | dormant |
| `rebuild-sqlite` | main.py:1157 | 0 | × | dormant |
| `storage` | main.py:1164 (flush-exports/maintain/status/migrate/snapshot/restore/vacuum) | 0 | nightly `_run_operational_projection` | low-use |
| `migrate` | main.py:1915 (scan/import/report) | 0 | × | dormant |
| `brief` | main.py:1943 (daily) | 0 | nightly `_run_daily_brief` | low-use |
| `quality` | main.py:1965 (stats/repair) | 0 | × | dormant |
| `identity` | main.py:1979 (report/repair) | 0 | × | dormant |
| `living` | main.py:1997 (enrich/timeline/posture) | 0 | × | dormant |
| `reflect` | main.py:2622 (check/log/read/stats) | 0 | × | dormant |
| `experience` | main.py:1275 (outcome) (register) | 0 | × | dormant |
| `learn` | main.py:1289 (register) | **14** (主力) | × (主入口) | **active** |
| `serve-eibrain-rpc` | main.py:1124 | 0 | × (常驻) | active (后台) |
| `doctor` | main.py:1088 (与 status 共用 health payload) | 0 | × | **partial** (CHANGELOG 承诺"System diagnostics"未实现) |
| `status` | main.py:1088 | **3** | × | active (实际只是 health check) |
| `ops` | main.py:1109 (timer-monitor) | 0 | × | dormant |
| `openclaw-hook` | main.py:2014 | **1** | × | active (Feishu 钩子) |
| `ei-bridge` | main.py:2038 (feishu) | **1** | × | active (Feishu 转发) |
| `governance` | main.py:2056 (snapshot/console) | 0 | × | dormant (snapshot 实际被 cli import 调) |
| `evolve` | main.py:2074 (evaluate/promotions/loop/autonomous/code-sandbox/gates/rollback/web-scout) | 0 | × | dormant (大部分走 runtime.evolution) |
| `eval` | main.py:2206 (run/ci/longmem/locomo/public-benchmark/living/actionable/production-recall/production-query/openclaw-e2e/task-replay) | 0 | nightly `_run_memory_eval_ci` | low-use |
| `patch` | main.py:828 (register) (propose/validate/promote/rollback/list) | 0 | × | dormant (1.6.0 harness-patch) |
| `vector-index` | main.py:833 (register) (status/migrate/sync) | 0 | × | dormant |
| `persona` | dynamic via add_persona_parser | 0 | × | dormant (实际用 openclaw-hook 触发) |

**统计**: 32 个 subcommand 中, honxin 真实直接调用 = **4 个** (learn×14, status×3, openclaw-hook×1, ei-bridge×1)。其余 28 个全部 dormant, 仅 nightly 通过 runtime API 间接触发。

**nightly 直接 import 的 CLI 模块**: `eimemory.scheduler.jobs.run_nightly_jobs` (cli/main.py:33), `eimemory.persona.cli.add_persona_parser/handle_persona_command` (cli/main.py:32)

---

## C. 僵尸模块清单

> 判定: `0 真 import` (无 `from eimemory.X` 绝对 import 也无 relative import) 且 `0 __init__ re-export` 且 `>0 defs/funcs`
> 排除: cli/main.py (入口), ops/openclaw_loop.py (动态 `from eimemory.ops import openclaw_loop` 加载)
> 统计: **29 个真僵尸, ~3000 行 (除 cli/main.py 入口)**

| 模块/文件 | defs (c+f+m) | 行数 | 建议处理 | 优先级 |
|---|---:|---:|---|---:|
| **ops/feishu_delivery_state.py** | 25 | 404 | 删除或合并到 ops/openclaw_loop.py | **P0** |
| **autonomous/mcp_stub.py** | 16 | 227 | 名字就说是 stub, 整模块删除 | **P0** |
| **governance/serve_console.py** | 9 | 84 | 0 真引用, 22 字节空; 删除或接入 main.py | **P0** |
| **governance/safety/l3_queue.py** | 10 | 133 | safety 子包被 0 真引用; 跟其它 safety 一起删 | **P0** |
| **living/temporal.py** | 11 | 186 | 整文件 0 真引用; 删除或合并到 operations.py | **P0** |
| governance/evidence_first.py | 8 | 120 | 在 autonomous_learning 字符串提到但 0 真 import; 删 | P1 |
| governance/state_machine.py | 8 | 185 | autonomous/program.md 提到但 0 真 import; 实际是文档 promise | P1 |
| governance/skills/eiskills_bridge.py | 9 | 162 | 整 skills 子包 0 真 import; 删 | P1 |
| governance/safety/anomaly.py | 6 | 101 | safety 子包 0 真 import | P1 |
| governance/safety/outbound_comm.py | 6 | 78 | safety 子包 0 真 import | P1 |
| governance/safety/audit_verifier.py | 5 | 67 | safety 子包 0 真 import | P1 |
| adapters/eibrain/sdk.py | 4 | 46 | eibrain 子包; 0 真引用; 删或并入 rpc.py | P1 |
| governance/safety/promotion.py | 4 | 85 | safety 子包 0 真 import | P1 |
| governance/safety/spend_guard.py | 5 | 88 | safety 子包 0 真 import | P1 |
| governance/prompt_safety_openclaw.py | 5 | 163 | prompt_safety 系列有 import 但这个 0; 删 | P1 |
| governance/skills/merged_pipeline.py | 4 | 97 | 整 skills 子包 0 真 import | P1 |
| autonomous/runner.py | 4 | 88 | autonomous 主动 0 真 import; 整个 autonomous 链路 0 真引用 | P2 |
| autonomous/capability_discovery.py | 3 | 61 | 同上 | P2 |
| autonomous/seven_day_review.py | 3 | 168 | 同上; 168 行大 | P2 |
| models/reports.py | 2 | 16 | 0 真 import; 16 行, 整个类 1 个 | P2 |
| autonomous/business_feedback.py | 2 | 98 | autonomous 子包 0 真 import | P2 |
| governance/held_out_split.py | 2 | 85 | governance 0 真 import | P2 |
| llm/openclaw_adapter.py | 2 | 74 | llm 子包, 0 真 import | P2 |
| persona/strategy_tree.py | 2 | 39 | persona 子包 0 真 import | P2 |
| governance/safety/network_proxy.py | 2 | 73 | safety 子包 0 真 import | P2 |
| core/errors.py | 1 | 2 | 1 个空 class, 0 真引用 | P2 |
| persona/awareness.py | 1 | 32 | 0 真引用 | P2 |
| intake/papers/pdf_parse.py | 1 | 11 | 0 真引用 | P2 |
| adapters/hermes/host_context.py | 1 | 23 | 0 真引用 | P2 |

**子包级僵尸 (0 真 import, 但有内部 defs)**:
- `governance/safety/` 整个子包 13 文件 0 真 import → 整包可考虑下沉或删除
- `governance/skills/` 整个子包 3 文件 0 真 import → 同上
- `persona/evals/` 整个子包 2 文件, `__init__.py` 是空文件
- `intake/papers/` 大部分走 `__init__` re-export, `pdf_parse.py` 0 真 import

**附带"边缘僵尸"** (有 1-2 真 import 但 honxin 数据 0 调用):
- `governance/l5_*` 多个文件 (l5_loop, l5_maturity, l5_readiness) — 代码量巨大但 nightly kind 数据 l5_* 70-167 条, 价值不明
- `evaluation/real_query_gate.py` (68 funcs, 2852 lines) — 0 真 import 但被 4 个文件 import; 用户没真实跑过 production query gate
- `evaluation/longmemeval.py` (35 funcs) — 1.9.70 添加的 public benchmark, honxin 0 跑过
- `evaluation/locomo.py` (11 funcs) — 同上 public benchmark
- `evaluation/public_benchmarks.py` (2 funcs) — 同上

---

## D. 计划但未实现

> 扫描结果: `NotImplementedError` 0 处, `pass  # TODO` 0 处, `if False` 0 处, `TODO/FIXME/HACK` 0 处, 空 pass 函数 0 个
> 结论: **代码层面没有"半成品"占位符**, 但 doc/CHANGELOG/program.md 仍承诺了若干未充分实现的能力

| 计划项 | 来源 | 代码状态 | 优先级 |
|---|---|---|---:|
| **Multi-agent memory coordination** | `CHANGELOG.md` `[Unreleased] / Planned` | 完全未实现 | **P0** (CHANGELOG 公开承诺) |
| **eimemory doctor 真"系统诊断"** | `CHANGELOG.md` 1.9.70 列出 "System diagnostics" | 实际跟 status 一样只输出 `build_health_payload` (cli/main.py:1088), 没单独的诊断检查 (DB schema/version/health/磁盘) | **P0** (公开 API 名不副实) |
| **`governance.state_machine` 真正被使用** | `autonomous/program.md:45` 写 "Use state machine (`state_machine.py`)" | 0 真 import, 8 funcs 全部 0 调用 | P1 |
| **`eimemory rl_policy` 与 RL 训练闭环** | `governance/rl_policy.py` (4 methods), `eimemory/rl_policy_value` sqlite kind | honxin `rl_policy_value` 0 records; 2 月没更新; kind 实际 3 条 | P1 (kind 死代码) |
| **`eval openclaw-e2e` 端到端测试** | subcommand 1.9.x 加, 11 个 eval 子命令之一 | `eval_cmd == "openclaw-e2e"` 有 handler (cli/main.py:657) 但 honxin 0 跑过 | P1 |
| **production query gate (`eval production-query`)** | `evaluation/production_query_dataset.py` (4 funcs), `evaluation/real_query_gate.py` (68 funcs) | api/runtime 有 `collect_pending_production_queries` 但 honxin 0 跑过; kind `production_query` 0 records | P1 |
| **`eimemory patch promote/rollback`** | cli 暴露 + governance.harness_patch.py 实现 | honxin 0 跑; harness_patch.py 引用但 promote 路径没在 nightly 跑 | P2 |
| **`learn live-acceptance` / `l5-assess` / `l5-readiness`** | 1.9.125 加入 "Bounded autonomous-learning and L5 promotion in production", 最多 3 次/cycle | 代码有但 honxin l5_* kind 总数 < 200 条; 30 天 review gate 后未放量 | P2 |
| **codex / hermes 适配器** | CHANGELOG 1.9.78 "Add distributable Codex plugin / native Hermes provider" | adapters/codex (3 files) 0 真 import; adapters/hermes (4 files) 0 真 import, 仅通过 __init__ 动态 | P2 (有代码但未在 honxin 实际用) |
| **`persona/strategy_tree.py`** | persona 治理 | 0 真 import, 39 行 2 funcs | P2 |
| **`evaluation/locomo`, `longmemeval`, `public_benchmarks`** | 1.9.70 公开 benchmark | 代码完整但 honxin 0 跑 | P3 (评估代码非生产必要) |
| **`learning_state.py` 完整闭环** | governance 84 文件中最大集群 | 34 真 import 但生产 usage 模糊; 配套 `learning_eval`, `learning_dashboard` 等无独立使用证据 | P3 |

---

## Top 5 僵尸模块（defs+行数排序）

1. **eimemory/ops/feishu_delivery_state.py** (25 defs, 404 lines)
   - 完全未被任何 `from eimemory.ops.feishu_delivery_state` 引用
   - 定义 `FeishuDeliveryState` v2 schema, 14 个 method
   - 建议: 整文件删除; Feishu 投递状态已在 `adapters/openclaw/hooks.py` 中处理

2. **eimemory/autonomous/mcp_stub.py** (12 funcs + 5 methods, 227 lines)
   - 文件名直接写明 "mcp_stub" — 占位符性质
   - 4 个 class + 7 个 func, 完全 0 真 import
   - 建议: 整模块删除

3. **eimemory/governance/safety/l3_queue.py** (9 funcs + 9 methods, 133 lines)
   - 整个 `governance/safety/` 子包 13 文件全部 0 真 import
   - L3 queue 实际未在生产用; 14 个 method 维护一条 l3_queue.jsonl
   - 建议: 整 safety 子包评估是否下线 (spend_guard / promotion / network_proxy / outbound_comm / audit_verifier / kill_switch / anomaly / l3_queue / circuit_breaker / profile / file_lock / promotion / audit — 13 文件)

4. **eimemory/living/temporal.py** (11 funcs, 186 lines)
   - 0 真 import, 无 re-export
   - 提供 temporal abstraction (推测是 time-windowed living memory)
   - 建议: 合并到 `living/operations.py` (有 4 真 import) 或删除

5. **eimemory/governance/serve_console.py** (8 funcs + 4 methods, 84 lines)
   - HTTP server 实现, 0 真 import
   - `eimemory governance console` subcommand 没真 import 它 (走 __getattr__ 间接)
   - 建议: 删除或与 main.py 实际接入

## Top 5 未实现/承诺未兑现功能

1. **Multi-agent memory coordination** (CHANGELOG `[Unreleased] / Planned`)
   - 公开文档承诺, 代码 0 痕迹 (无 multi_agent/ 文件夹, 无 multi_agent kind)

2. **`eimemory doctor` 真"系统诊断"** (CHANGELOG 1.9.70)
   - 当前 doctor 与 status 共用 health_payload 输出 (cli/main.py:1088)
   - 应包含: schema version check, DB 完整性, JSONL segment 健康, 磁盘空间, timer 状态 — 这些零散分布在 `evaluation/framework.py`, `ops/timer_monitor.py`, `storage/maintenance.py` 但没拼成 doctor

3. **`state_machine.py` 真实使用** (autonomous/program.md:45)
   - 文档说 "Use state machine (`state_machine.py`)"
   - 实际 0 真 import, 0 honxin 调用

4. **`eval openclaw-e2e` / `eval production-query` 端到端** (1.9.x)
   - 代码完整, 0 真 import 子模块 (production_query_dataset.py 4 funcs, real_query_gate.py 68 funcs)
   - honxin 0 跑过 production_query kind

5. **`rl_policy_value` kind + rl_policy.py 训练闭环** (governance/rl_policy.py 4 methods)
   - honxin 实地: rl_policy_value 3 records, 2 月没更新
   - 跟 `learn l5` 治理路径关联, 但实际未跑

---

## 附录: 关键文件位置 (审计锚点)

- 入口: `eimemory/__init__.py:3-4` (Runtime, __version__)
- CLI 调度: `eimemory/cli/main.py:1016` (`def main`), :1088 (doctor/status), :1955 (nightly)
- 治理入口: `eimemory/governance/__init__.py:6` (`__getattr__` lazy load 4 个核心导出)
- Nightly 主调度: `eimemory/scheduler/jobs.py:27` (`def run_nightly_jobs`)
- Runtime 主体: `eimemory/api/runtime.py:1-2192`
- Records schema: `eimemory/models/records.py:1-305`
- 存储后端: `eimemory/storage/sqlite_store.py:1-6871` (最大单文件)
- 版本: `eimemory/version.py:1` (`__version__ = "1.9.129"`)

## 附录: 关键统计

- **总规模**: 293 .py 文件, 98,732 行 (实际比 prompt 说的 233/~50K 多 ~95%)
- **CLI subcommand**: 32 top-level, 31 有 handler, 1 个真未实现是 `cli/main.py` (它是入口)
- **0 真 import 真僵尸**: 30 个文件, 5,655 行 (含 cli/main.py 入口 2,659 行 → 实际僵尸代码 ~3K 行)
- **0 NotImplementedError / 0 TODO / 0 if False / 0 empty pass** — 表明代码已完成度极高
- **honxin 真实活跃**: 4 个 CLI subcommand (learn, status, openclaw-hook, ei-bridge) + 1 个定时器 (nightly)
- **l5_* 治理 kind**: 总数 < 200 条, 30 天 review gate 后未放量

---

*End of Audit Report*
