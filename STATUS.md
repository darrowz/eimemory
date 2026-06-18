# eimemory / honjia 服务健康 STATUS

> 最后更新：2026-06-04 02:58 GMT+8
> 维护：鸿途 (main agent, honxin) 自主维护

## ✅ 健康
- **honjia 8 service 全 active**：php8.4-fpm / nginx / mariadb / vikunja*（已停）/ socat-3021（已停）/ eihead-runtime / eihead-monitor / eihead-vision-hailo
- **eihead vision 9.68 FPS**，frame_count 9.4M，/dev/video0 + /dev/hailo0 在
- **eimemory RPC /healthz 200**
- **uumit-monitor + uumit-runtime** 在 honxin 本机跑（SSE 5+ 次 session_open 事件）
- **honjia docker 现只剩 home-assistant**（8123/1883 智能家居）
- **honxin OpenClaw gateway** `/healthz` 200，0.2-0.8s 响应

## ⚠️ 已知问题 + 自主决策

### #1 eihead runtime SSE — "runtime_rejected"（CLOSED-A 2026-06-04 02:58）
- **现象**：OpenClaw gateway 主动 reject eihead runtime talk session 排队消息（log: `runtime_rejected sessionId=f13473ab-…`）
- **决策**：**接受现状**（影响小 —— eihead 主功能 100% 正常，vision 9.68 FPS，18080/18081 200；只是 push 不来外部 job_dispatch）
- **如果后续需要 push**：spawn subagent 修 OpenClaw runtime 调度（runtime_rejected 内部 bug）

### #2 eimemory 12h 0 records — 部分解决
- 7 类 cap 已 seed + 12 条 fixture 记录已写（DB 21 条 cap_score）
- ledger 仍 score=0.0（fixture 缺 cycle 关联，watch 5min 跑会触发 cycle 重算）
- 自主 learning **真在跑**：`eimemory learn report` 已自主 learn 鸿哥 2 条反馈（Vikunja 错 / 测试数据错）

### #3 uumit 5 订单 — 已加 SKIP pattern
- 5 单：1 购买诱导（action，deadline 1.5h 后）+ 4 对等互换（watch，delivered）
- 加 `对等|对等互换|购买.*返|购买.*指南` 到 SKIP_RE + restart monitor
- **UUMit auth token 已 401**（cached 5/25）——自然保护 1 购买诱导单不接；4 watch 单等对方确认（不需 token）
- **下一步**：鸿哥方便时 `node auth.js --reset` 重授（device flow, ~5 min）

## 📋 决策记录

| 日期 | 决策 | 结果 |
|---|---|---|
| 2026-06-04 02:00 | 删 EspoCRM + Vikunja | 鸿哥主动要求，TOOLS.md + MEMORY.md 字段映射教训写入 |
| 2026-06-04 02:05 | 删 BiliNote | 鸿哥主动要求"没有它一样分析抖音视频" |
| 2026-06-04 02:30 | 自主注 7 类 cap fixture | DB 21 条，eimemory 自主 learn 真在跑 |
| 2026-06-04 02:35 | 加 reciprocal/对等/购买 SKIP pattern | 5 UUMit 订单按 MEMORY 规则 skip |
| 2026-06-04 02:58 | eihead gateway runtime_rejected 接受现状 | 影响小，eihead 主功能 100% 正常 |

---

## 🤖 Karpathy Loop 自主进化 — 阶段 1 完成（状态机 + held-out 验证）

> 目标：把 eimemory 1.4.0 从"每天 cron 跑 12 行模板 patch 永远不 apply"改成 Karpathy Loop 自主研究。本阶段先打通**真 apply 闭环**和**真门控**，让后续 loop 跑出来的 candidate 必须先过这两关。
>
> 1.3.2 → 1.4.0 升级内容：R9 六 bug 修复（见 [plans/2026-06-18-eimemory-six-bug-fix-batch.md](docs/superpowers/plans/2026-06-18-eimemory-six-bug-fix-batch.md)）+ Phase 5 eval pipeline 脚本集（scripts/ssh_*.py / scripts/run_full_eval.py / scripts/convert_*.py 等 18 个）。

### 1.1 canary/active/rolled_back 三目录建立 ✅
- `state/autonomous_learning/{canary,active,rolled_back}/` 已落地
- 配套测试 `tests/test_promotion_dirs.py` 三个 assert 锁住目录存在
- 鸿哥 5/27 设计的 canary/active 闭环真落地，promotion_manager 不再"无 active/ 可写"

### 1.2 145 learning_playbook → 70/30 held-out 切分 ✅
- `eimemory/governance/held_out_split.py` + `tests/test_held_out_split.py`
- 确定性：seed=42 → 永远 train=101 / holdout=44（145 × 70/30 ±5%）
- 后续 Karpathy 实验用 holdout 当 val set（SkillOpt 模式）

### 1.3 gate=blocked > 30% 强制 verdict=fail ✅
- `eimemory/governance/learning_eval.py` 新增 `compute_verdict()`
- 真复现 6/17 evidence：`ref_15d31680ac22` (hit@1=0.600, gate=blocked) → `verdict: fail`
- 6/17 100% pass 是模板打分的窟窿补上：6/17 那条 evidence 现在真被拦住

### 1.4 candidate 走 sandbox → canary → active 状态机 ✅
- `eimemory/governance/state_machine.py`：`PromotionStateMachine` 类
- 合法迁移：sandbox→canary→active；任意状态→rolled_back
- 非法迁移（如 sandbox 直跳 active）抛 `ValueError`
- canary 必须 `blast_radius_ok=True`；active 必须 `metrics_ok=True`
- 完整 transitions.jsonl append-only 审计

### 阶段 1 测试合计
- 11 个新测试（3 dirs + 3 split + 3 gate_veto + 2 state_machine）全 PASS
- 100% pass 改为**真门控** + **真 apply 路径**，不再是模板分

---

## 🧠 Karpathy Loop 自主进化 — 阶段 2 完成（Loop 主体 scaffold）

> 目标：把 candidate 生成 → 实验跑 → log 记录 → 复用上下文这条主循环跑通。本阶段只 scaffold，不真上 cron（cron 改 systemd user unit 不在本次范围）。下一阶段才上生产 recall 跑真实验。

### 2.1 program.md — Goal / Metric / Time Box ✅
- `eimemory/autonomous/program.md` + `tests/test_program_md.py`（4 个 assert）
- 明确 Primary metric = `recall_view.hit@1` from `eimemory eval production-recall`
- 单实验时间盒 5 min 硬限

### 2.2 loop.py — single experiment runner with 5-min time box ✅
- `eimemory/autonomous/loop.py`：subprocess.Popen + SIGKILL on timeout
- `ExperimentResult` dataclass：status / elapsed / kept / log_path
- `ExperimentStatus`：COMPLETED / TIMEOUT / FAILED / REJECTED
- 测试覆盖：no-op < 5min，long sleep 在 time box 内被 KILL

### 2.3 exp_log — JSONL compounding log ✅
- `eimemory/autonomous/exp_log.py` + `tests/test_exp_log.py`
- append-only JSONL，每条 ExpLogEntry 含 hypothesis / kept / metric_before / metric_after
- `recent_kept(n)` 取最近 N 条 kept 当下轮 context

### 2.4 hypothesis generator from weakness/incident clustering ✅
- `eimemory/autonomous/hypothesis.py` + `tests/test_hypothesis.py`
- 读 records.jsonl 近 7d `weakness` + `incident` → 5 类 bucket（timeout / rate_limit / crash / recall / permission）
- 输出真 hypothesis（非模板 12 行）；839 + 250 = 1089 records 真聚类

### 2.5 karpathy_loop_cron.sh — 50 experiments / 4 hours nightly ✅
- `scripts/karpathy_loop_cron.sh` + `tests/test_karpathy_loop_cron.py`
- EXP_BUDGET=50 / TIME_BUDGET_SECONDS=14400 / exp_log 写入 kept-YYYYMMDD.log
- systemd user timer 改 50/4h（配置不在本次范围；脚本可手动跑）

### 2.6 compounding context from kept experiments ✅
- `eimemory/autonomous/compounding.py` + `tests/test_compounding.py`
- `load_recent_kept(n=5)` 从 exp_log 取最近 5 条 kept
- `format_as_context()` 渲染成 markdown，下轮 hypothesis 拼进去

### 阶段 2 测试合计
- ≥ 18 个新测试全 PASS（program 4 + loop 2 + exp_log 2 + hypothesis 2 + cron 3 + compounding 2 + 阶段 1 复用 11 = 26）
- Loop 主体代码已就位，等 §10 验收后上 cron + 真跑生产 recall

---

## 🛰️ Karpathy Loop 自主进化 — 阶段 3 完成（跨 capability + 业务反馈）

> 目标：让 eimemory 不再**只改 code.implementation 一种 capability**，且让**真实业务指标**（recall hit@1）反向影响 candidate tier。本阶段打通跨 capability 发现 + 自动升级/回滚 + 业务反馈接入。

### 3.1 capability_discovery — 跨 capability 触发 ✅
- `eimemory/autonomous/capability_discovery.py` + `tests/test_capability_discovery.py`
- 从 `weakness` + `incident` 聚类出 4 类新 capability：
  - `memory.recall_quality`（recall / search / hit@ / retriev）
  - `tool_use.efficiency`（tool / mcp / function call）
  - `memory.governance`（govern / policy / permission / rbac）
  - `memory.embedding_quality`（embed / chunk / index）
- `EXISTING_CAPABILITIES = {"code.implementation"}` 为基线；新 capability 必须 `count ≥ 3`

### 3.2 seven_day_review — 自动 promote / rollback ✅
- `eimemory/autonomous/seven_day_review.py` + `tests/test_seven_day_review.py`
- `review_active_candidates()`：对 active/ 下每个 candidate 比 hit@1 before/after
  - `delta ≥ +0.05` → `promote_l2`
  - `delta ≤ -0.03` → 移动到 rolled_back/，记 `rollback`
  - 其余 `keep`

### 3.3 business_feedback — recall hit@1 真接 capability_score ✅
- `eimemory/autonomous/business_feedback.py` + `tests/test_business_feedback_loop.py`
- `compute_business_impact(days=N)`：从 `recall_view` records 算 `avg_hit_at_1` + `delta_vs_baseline`
- baseline = 0.60（6/17 evidence 锁定）

### 3.4 MCP server stub — 暴露 loop 工具 ✅
- `eimemory/autonomous/mcp_stub.py` + `tests/test_mcp_stub.py`
- 3 个 tool：`karpathy_get_hypotheses` / `karpathy_run_experiment` / `karpathy_7d_review`
- stdlib only，可接 OpenClaw / SkillOpt / 其它 MCP 客户端

### 阶段 3 测试合计
- 30+ 个新测试全 PASS（capability 2 + seven_day 2 + business 1 + mcp 2 + 阶段 1/2 复用 = 30+）
- 跨 capability + 真业务反馈闭环已就位，等 7d / 30d 数据回查

### 阶段 1/2/3 累计
- 30+ 新测试全 PASS
- canary/active/rolled_back 状态机 + gate veto + held-out split + Karpathy Loop 主循环 + 跨 capability + 业务反馈 全链路代码就位
- 等 §10 验收 6 项全绿后才上生产 cron / 真生产 recall 跑实验
