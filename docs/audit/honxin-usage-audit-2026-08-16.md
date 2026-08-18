# eimemory 真实使用情况审计（honxin 实地 + 本地代码）

> 审计日期: 2026-08-16
> 工作空间: `E:\eimemory` v1.9.129 (commit 4044ecb)
> honxin 生产部署: v1.9.132 (commit 20af6e54) — 1.9.133 装了 6 天没切 current
> 审计员: Mavis + 2 个并行 subagent（本地代码 + honxin 实地 SSH）
> 方法: 静态代码扫描 + SSH 实地数据库/服务/进程查询

---

## 一、子系统全景

**honxin 上 eimemory 真实部署形态**：

```
                            ┌──────────────────────────┐
                            │  eimemory 1.9.132 @ /opt │
                            │  current -> 20af6e54     │
                            └────────────┬─────────────┘
                                         │ 100.105.189.120:8091
                                         │ (Tailscale, 0 traffic)
        ┌────────────────────────────────────┼────────────────────────────────┐
        │                                    │                                │
   ┌────▼─────┐   ┌──────────────┐   ┌───────▼────────┐   ┌────────────────┐
   │ eimemory │   │ openclaw-    │   │ hermes-        │   │ honxin-feishu- │
   │  -rpc    │   │ gateway      │   │ gateway        │   │ fast           │
   │ :8091    │   │ :18789       │   │ (Tailscale)    │   │ :18790         │
   │ ✅ 1-12h │   │ ✅ 3 天前    │   │ ✅ 8 天前      │   │ ✅ 18 天前     │
   └────┬─────┘   └──────┬───────┘   └────────┬───────┘   └────────┬───────┘
        │                │                    │                    │
        │                └──────┬─────────────┘                    │
        │                       │                                  │
        │       EIMEMORY_RPC_URL=http://100.105.189.120:8091/   │
        │                       │                                  │
        │              ┌────────▼─────────┐                ┌──────▼──────┐
        │              │  飞书 webhook    │                │  飞书 API   │
        │              │  (via openclaw)  │                │  (honxin-   │
        │              └──────────────────┘                │   feishu)   │
        │                                                  └─────────────┘
        │
   ┌────▼────────────┐    ┌─────────────────┐
   │ eimemory-       │    │ 其它 honxin 进程 │
   │  nightly.timer  │    │  headroom-proxy │
   │  03:30 daily    │    │  vnc, ssh-agent  │
   │ ✅ 在跑         │    │  uumit-agent     │
   └─────────────────┘    └─────────────────┘
```

**Dead 状态**（曾经启用，2026-08-10 16:15 后集体停摆 6 天）：
- `eimemory-l5-effect-review.service` 
- `eimemory-learn-dashboard.service`
- `eimemory-learn-think.service`
- `eimemory-learn-watch.service`
- `eimemory-release-closure.service`
- `openclaw-loop-compact.service`
- `openclaw-loop-watch.service`

**用户活跃进程**（8-16 22:00 数据）：
- `eimemory-rpc.service` — PID 1383768，etime 1-12:15:28 ✅
- `headroom-proxy.service` — 18 天前启动 ✅
- `hermes-gateway.service` — 8 天前 ✅
- `honxin-feishu-fast.service` — 18 天前，但 **0 traffic**
- `openclaw-gateway` — 3 天前 ✅
- `openclaw-loopback-proxy` — 3 天前 ✅
- `uumit-runtime` — 18 天前（tmux 死循环）

---

## 二、子系统统计

| 维度 | 数量 | 备注 |
|------|------:|------|
| **本地 Python 文件** | 293 | 比 audit 报告提到的 233 多 60（新增 adapters/codex, hermes, runtime） |
| **总代码行** | 98,732 | 比全量审计时的 ~50K 多 95% |
| **子模块目录** | 27 | 含 `llm/`, `retrieval/`, `adapters/runtime`, `adapters/codex`, `adapters/hermes` |
| **sqlite 表** | 25 | 加 7 张 proactive / receipt / vector 表 |
| **record kind** | 42 | 加 6 种新 kind（rl_transition, l5_*, etc） |
| **CLI subcommand** | 32 | honxin 真用 = 4 |
| **systemd services** | 7 eimemory + 4 honxin | 真在跑 = 4 eimemory |
| **systemd timers** | 8 | 真在跑 = 2 (nightly + launchpadlib) |
| **data source** | 45 | written>0 = 6 (24%) |
| **git log commits** | 315 | 1.8.22 → 1.9.129 |

---

## 三、模块使用率矩阵（本地 + 实地交叉）

### A. 活跃模块（top 6）

| 模块 | 文件数 | defs/funcs | 包内引用 | honxin 实地 | 评级 |
|------|------:|------:|------:|------|---|
| **storage** | 7 | 80 | 37 | ✅ sqlite 69257 rows | active |
| **api** | 3 | 32 | 24 | ✅ learn/recall/ingest API | active |
| **models** | 12 | 25 | 180 | ✅ 所有 records kind | active |
| **core** | 3 | 3 | 61 | ✅ 时钟/ID 体系 | active |
| **governance** | 84 | 1286 | 282 | ✅ 535 policy_rollout_ledger | active |
| **intake** | 15 | 223 | 50 | ✅ 45 source nightly 跑 | active |
| **evaluation** | 17 | 240 | 44 | ⚠️ 7-28 跑过一次 production_recall | active |
| **knowledge** | 14 | 159 | 26 | ✅ 2277 knowledge_page | active |
| **persona** | 11 | 44 | 32 | ⚠️ 8-11 后停写 | active |

### B. 真僵尸模块（0 真 import × 0 真实使用）

**29 个真僵尸文件，~3K 行**：

| 模块/文件 | defs | 行数 | 状态 | 优先级 |
|----------|------:|-----:|------|------:|
| `ops/feishu_delivery_state.py` | 25 | 404 | 完全 0 import | **P0** |
| `autonomous/mcp_stub.py` | 16 | 227 | 名字就是 stub | **P0** |
| `governance/safety/` (整子包 13 文件) | 30 | ~900 | 0 真 import | **P0** |
| `governance/skills/` (整子包 3 文件) | 12 | ~270 | 0 真 import | **P0** |
| `governance/serve_console.py` | 9 | 84 | 0 真 import | **P0** |
| `living/temporal.py` | 11 | 186 | 0 真 import | **P0** |
| `adapters/eibrain/sdk.py` | 4 | 46 | 0 真 import | P1 |
| `governance/state_machine.py` | 8 | 185 | 0 真 import（program.md 提到）| P1 |
| `governance/evidence_first.py` | 8 | 120 | 0 真 import | P1 |
| `governance/prompt_safety_openclaw.py` | 5 | 163 | 0 真 import | P1 |
| `autonomous/runner.py` | 4 | 88 | 0 真 import | P2 |
| `autonomous/capability_discovery.py` | 3 | 61 | 0 真 import | P2 |
| `autonomous/seven_day_review.py` | 3 | 168 | 0 真 import | P2 |
| `autonomous/business_feedback.py` | 2 | 98 | 0 真 import | P2 |
| `llm/openclaw_adapter.py` | 2 | 74 | 0 真 import | P2 |
| `persona/strategy_tree.py` | 2 | 39 | 0 真 import | P2 |
| `persona/awareness.py` | 1 | 32 | 0 真 import | P2 |
| `models/reports.py` | 2 | 16 | 0 真 import | P2 |
| `core/errors.py` | 1 | 2 | 0 真 import | P2 |
| `intake/papers/pdf_parse.py` | 1 | 11 | 0 真 import | P2 |
| `adapters/hermes/host_context.py` | 1 | 23 | 0 真 import | P2 |
| `governance/held_out_split.py` | 2 | 85 | 0 真 import | P2 |
| `governance/safety/network_proxy.py` | 2 | 73 | 0 真 import | P2 |
| `governance/safety/spend_guard.py` | 5 | 88 | 0 真 import | P2 |
| `governance/safety/promotion.py` | 4 | 85 | 0 真 import | P2 |
| `governance/safety/anomaly.py` | 6 | 101 | 0 真 import | P2 |
| `governance/safety/outbound_comm.py` | 6 | 78 | 0 真 import | P2 |
| `governance/safety/audit_verifier.py` | 5 | 67 | 0 真 import | P2 |

**整子包级僵尸**：
- `governance/safety/` 13 文件 0 真 import
- `governance/skills/` 3 文件 0 真 import
- `persona/evals/` 2 文件（`__init__.py` 空文件）
- `intake/papers/pdf_parse.py`

---

## 四、功能点使用率（CLI subcommand）

**32 个 subcommand 中 honxin 真用 = 4 个**：

| subcommand | honxin bash_history | nightly 跑 | 评级 |
|-----------|---:|---------|------|
| `learn` | **14** (l5-readiness 主力) | × | **active** |
| `status` | **3** | × | active（实际只 health check）|
| `openclaw-hook` | **1** | × | active |
| `ei-bridge` | **1** | × | active |
| `nightly` | 0 (systemd 调) | **YES (03:30 每天)** | active (后台) |
| `serve-eibrain-rpc` | 0 (systemd 调) | × | active (后台) |
| `doctor` | 0 | × | **partial**（与 status 一样只 health）|
| `ingest` | 0 | × | dormant |
| `recall` | 0 | × | dormant |
| `paper` | 0 | × | dormant |
| `source` | 0 | × | low-use (nightly 走 runtime) |
| `intake` | 0 | × | low-use |
| `export` / `import` | 0 | × | dormant |
| `backup` | 0 | × | dormant |
| `rebuild-sqlite` | 0 | × | dormant |
| `storage` | 0 | × | low-use (nightly 走 runtime) |
| `migrate` | 0 | × | dormant |
| `brief` | 0 | × | low-use (nightly 走 runtime) |
| `quality` | 0 | × | dormant |
| `identity` | 0 | × | dormant |
| `living` | 0 | × | dormant |
| `reflect` | 0 | × | dormant |
| `experience` | 0 | × | dormant |
| `ops` | 0 | × | dormant |
| `governance` | 0 | × | dormant |
| `evolve` | 0 | × | dormant (走 runtime.evolution) |
| `eval` | 0 | × | low-use (nightly 走 _run_memory_eval_ci) |
| `patch` | 0 | × | dormant |
| `vector-index` | 0 | × | dormant |
| `persona` | 0 | × | dormant |
| `emergency-stop` | 0 | × | dormant (purpose-built) |
| `init` | 0 | × | dormant |

**统计**：4/32 = **12.5% 真实使用率**

---

## 五、Record Kind 真实使用情况

### A. 高活跃（> 1000 records）

| kind | 总数 | 30天 | 用在 |
|------|------:|------:|------|
| `reflection` | 13,262 | 8,997 | 主力：nightly 反思 |
| `recall_view` | 12,920 | 7,689 | 召回视图 |
| `replay_result` | 5,940 | 4,810 | 回放测试结果 |
| `feedback` | 4,935 | 4,935 | 用户反馈 |
| `capability_score` | 4,226 | 1,170 | 能力分数 |
| `memory` | 3,060 | 1,044 | 核心记忆 |
| `knowledge_candidate` | 2,955 | 944 | 知识候选 |
| `learning_eval` | 2,653 | 1,741 | 学习评估 |
| `world_signal` | 2,566 | 1,180 | 世界信号 |
| `weakness` | 2,352 | 1,178 | 弱点 |
| `knowledge_page` | 2,277 | 410 | 知识页 |
| `entity_record` | 1,786 | 324 | 实体 |
| `incident` | 1,557 | 785 | 事件 |
| `claim_card` | 1,009 | 136 | 声明卡 |
| `relation_record` | 1,009 | 136 | 关系记录 |

### B. 中等活跃（100-1000）

| kind | 总数 | 30天 | 用途 |
|------|------:|------:|------|
| `paper_extract` | 983 | 139 | 论文抽取 |
| `research_task` | 776 | 420 | 研究任务 |
| `evaluation_packet` | 614 | 588 | 评估包 |
| `paper_source` | 559 | 192 | 论文源 |
| `thought` | 388 | 271 | 思考 |
| `capability_model` | 348 | 229 | 能力模型 |
| `learning_playbook` | 335 | 152 | 学习剧本 |
| `rule` | 295 | 202 | 规则 |
| `learning_goal` | 246 | 104 | 学习目标 |
| `promotion_request` | 237 | 142 | 升级请求 |
| `learning_experiment` | 232 | 99 | 学习实验 |
| `news` | 225 | 65 | 新闻 |
| `l5_self_continuity` | 167 | 147 | L5 自连续 |
| `evaluator_verdict` | 158 | 99 | 评估判定 |
| `stop_judgment` | 158 | 99 | 终止判断 |
| `source_candidate` | 132 | 90 | 源候选 |
| `learning_loop` | 104 | 42 | 学习循环 |
| `capability_candidate` | 102 | 23 | 能力候选 |
| `rl_transition` | 101 | 70 | RL 转换 |

### C. 僵尸/极冷（< 100）

| kind | 总数 | 最后更新 | 备注 |
|------|------:|---------|------|
| `l5_world_model` | 99 | 8-16 04:44 | L5 世界模型 |
| `regression_watch` | 99 | 8-16 04:44 | 回归监控 |
| `research_note` | 94 | 8-16 04:45 | 研究笔记 |
| `l5_assessment` | 92 | 8-16 04:44 | L5 评估 |
| `l5_strategic_roadmap` | 80 | 8-16 04:44 | L5 战略路线图 |
| `l5_closed_loop` | 70 | 8-16 04:44 | L5 闭环 |
| `raw_chunk` | 43 | **6-19 04:40** | **2 月没更新**（僵尸）|
| `skill_candidate` | 9 | 8-16 04:44 | 技能候选（极少）|
| `rl_policy_value` | 3 | **6-27 03:57** | **近 2 月没更新**（僵尸）|
| `source_watch` | 1 | 8-16 04:23 | 源监控（极冷）|

---

## 六、未实现 / 承诺未兑现

| 计划项 | 来源 | 代码状态 | 优先级 |
|--------|------|---------|------:|
| **Multi-agent memory coordination** | CHANGELOG `[Unreleased] / Planned` | 完全未实现 | **P0** |
| **`eimemory doctor` 真"系统诊断"** | CHANGELOG 1.9.70 | 实际跟 status 共用 health_payload，没独立诊断（DB schema/version/磁盘/timer） | **P0** |
| **`governance.state_machine` 真实使用** | autonomous/program.md:45 明确写 | 0 真 import，0 调用 | P1 |
| **`eimemory rl_policy` 与 RL 训练闭环** | rl_policy.py 4 methods + `rl_policy_value` kind | honxin 3 records, 2 月没更新 | P1 |
| **`eval openclaw-e2e` 端到端** | subcommand 1.9.x 加 | honxin 0 跑过 | P1 |
| **`eval production-query` production gate** | real_query_gate.py 68 funcs, production_query_dataset.py 4 funcs | honxin 0 跑过，`production_query` kind 0 records | P1 |
| **`eimemory patch promote/rollback`** | governance.harness_patch.py 实现 | honxin 0 跑，promote 路径没在 nightly 跑 | P2 |
| **`learn live-acceptance` / `l5-assess` / `l5-readiness`** | 1.9.125 加入 | 代码有但 l5_* kind 总数 < 200，30 天 review gate 后未放量 | P2 |
| **codex / hermes 适配器** | CHANGELOG 1.9.78 | adapters/codex (3 files) 0 真 import；adapters/hermes (4 files) 0 真 import | P2 |
| **`persona/strategy_tree.py`** | persona 治理 | 0 真 import | P2 |
| **`evaluation/locomo`, `longmemeval`, `public_benchmarks`** | 1.9.70 公开 benchmark | 代码完整但 honxin 0 跑 | P3 |
| **`learning_state.py` 完整闭环** | governance 84 文件最大集群 | 34 真 import 但生产 usage 模糊 | P3 |

---

## 七、8/10 16:15 集体停摆事件

**关键分水岭**：
- 8/10 15:45 learn-think 跑了最后一次
- 8/10 16:15:21 learn-watch 跑了最后一次
- 8/10 16:15:34 装 1.9.133（**没切 current**）
- 8/10 16:15 之后 **6 个 eimemory timer 全部停**
- 8/11 17:00 `openclaw.before_prompt_build` 和 `persona.trace` 最后一次写入
- 8/11 后这两个数据流也停了

**8/10 之后 eimemory 实际只剩**：
- `eimemory-rpc.service` 在跑（每 1-12 小时重启）
- `eimemory-nightly.timer` 03:30 每天跑
- `learn` 命令手动跑（user 主动）
- `openclaw-gateway` / `hermes-gateway` / `honxin-feishu-fast` 还在但 **0 traffic**

---

## 八、关键异常（应该有人在看但没看）

1. **1.9.133 装 6 天没切 current** — 可能 release-closure 卡住
2. **6 个 eimemory timer 自 8/10 后同步停** — LastTriggerUSec 没刷
3. **eimemory-rpc MemoryMax=2G 仍被 OOM 杀**（cgroup 失效）
4. **L5 readiness：observed L4 / score 0.8** — accumulated maturity L5 但 maturity_transition=held，4 个 gate 都不过
5. **飞书 message_received 最后一次 6-21**（56 天前）— openclaw.before_prompt_build 12117 records 后 0 写入
6. **45 source 中 39 written=0**（24% 实际采到数据）
7. **L5 next_action 明确要求 `device.control` 和 `operations.uumit` 各 3+ replay**，但 5 天没跑
8. **hermes-gateway 每天 ValueError: embedded null byte**（飞书残留消息含特殊字符）
9. **uumit-agent 死循环**（tmux，18 天前启动，每轮 200 扫但 applied=0 — 配额卡死）
10. **events.jsonl 7073 行全部 `{}`** — event 写入后被空 dict 覆盖？

---

## 九、Top 5 立即行动建议

### 优先级 0（本周）

1. **修 release-closure 卡住问题** — 1.9.133 装 6 天没切 current，2 个 release 旧版本 (`current.bak.1786074812, 1786074936, 1786093640`) 待清理
2. **重启 6 个 eimemory timer** — learn-watch / learn-think / learn-dashboard / l5-effect-review / release-closure / openclaw-loop-watch
3. **修 `eimemory doctor` 真正实现系统诊断** — 现在是名不副实

### 优先级 1（2 周内）

4. **删除 ~3K 行僵尸代码** — 29 个 0 真 import 文件，特别是：
   - `ops/feishu_delivery_state.py` (404 行)
   - `autonomous/mcp_stub.py` (227 行)
   - 整 `governance/safety/` 子包 13 文件（0 真 import）
   - 整 `governance/skills/` 子包 3 文件
5. **修 eimemory-rpc OOM** — MemoryMax=2G 不够或 cgroup 失效，需要 runtime 内存调优

### 优先级 2（持续）

6. **修 L5 transition held** — capability replay 5 天没跑
7. **修 39/45 source written=0**（RSS 僵尸）
8. **补 4/7 飞书 / openclaw-before-prompt 数据流**（8/10 停摆原因）
9. **修 events.jsonl 7073 行全 `{}`**（写入逻辑 bug）

---

## 十、扫描统计

- **本地 Python 文件扫描**: 293 个，98,732 行
- **honxin 实地查询**: 25+ 次 SSH 命令，覆盖：
  - systemd services/timers
  - sqlite 数据库（25 表）
  - 12+ jsonl 日志
  - 实际进程清单
  - bash history
  - /opt/eimemory 部署
  - 评估数据
  - sandbox 产物
  - source registry
  - openclaw 集成
- **覆盖时间**: 2026-08-16 22:00 - 22:25 CST

---

**报告生成时间**: 2026-08-16 22:25
**审计版本**: eimemory v1.9.129 (local) / v1.9.132 (honxin)
**审计员**: Mavis (orchestrator) + 2 个 subagent（本地 + 实地）
**对比基线**: 上次审计 (2026-07-27, v1.9.94)
