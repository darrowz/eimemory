# eimemory Full Autonomous Learning System Implementation Plan

> **For Codex tomorrow:** Use `superpowers-open:executing-plans`. Execute task-by-task, keep tests green after each phase, and do not promote L3 actions without explicit human confirmation.

**Goal:** Build the full autonomous learning system for eimemory/OpenClaw: world watchers, autonomous research, sandbox experiments, capability evaluation, PR/diff promotion, and long-term capability curves. This is the unrestricted architecture in autonomy scope, but still uses explicit authority gates for external side effects.

**Core Principle:** LLM is a tool, not the evolution system. The system must own observation, goal generation, research records, experiments, evaluation, promotion, and regression monitoring as durable artifacts.

**Relationship to 0.10.1:** The 0.10.1 bounded plan implements the local kernel. This full plan extends that kernel into an always-on learning and improvement system. Implement 0.10.1 first if the codebase does not yet have the autonomous learning kernel.

**Non-Negotiable Safety Boundary:** "Unrestricted" does not mean arbitrary execution. It means the agent can independently observe, research, experiment, generate diffs, open reviewable changes, and track capability improvement. It still cannot send externally, spend money, change account auth, deploy production, operate devices, alter system prompts, or delete data without explicit approval.

---

## Authority Model

### L0: Fully Autonomous

Allowed without asking:

- Watch configured public/local sources.
- Create learning goals.
- Run research tasks.
- Store research notes.
- Create sandbox experiments.
- Generate eval cases.
- Run replay/eval locally.
- Write local learning reports.
- Create candidate records.
- Update capability scores.

### L1: Autonomous Local Promotion

Allowed without asking after tests pass:

- Add or update eval fixtures.
- Add non-core skill drafts.
- Add SOP/checklist drafts.
- Add low-risk local tool wrappers that do not call external services by default.
- Add source watcher configs in disabled/dry-run mode.

### L2: Autonomous PR/Diff Promotion

Allowed without asking, but must stop at review:

- Core eimemory/OpenClaw code changes.
- Important skill updates.
- Tool routing strategy changes.
- Source policy changes.
- Scheduler job changes.
- Any change that affects default agent behavior.

Output must be a branch, patch, or PR with evidence, eval results, rollback plan, and risk notes.

### L3: Human Confirmation Required

Never execute automatically:

- External messages/posts/emails/comments.
- Spending money or using paid APIs beyond already approved budgets.
- Account auth, OAuth scopes, tokens, credentials, or app permissions.
- Production deployment or service restarts that affect live users.
- Device action: audio output, robotics, screen/GUI control, office hardware.
- System prompt or high-level policy mutation.
- Irreversible deletion or private data export.

---

## Architecture

```text
World Watchers
  -> Signal Intake
  -> Self-Model
  -> Curiosity Engine
  -> Research Planner
  -> Evidence Collector
  -> Sandbox Lab
  -> Evaluation Harness
  -> Capability Distiller
  -> Promotion Manager
  -> Capability Ledger
  -> Governance Console
```

The system runs as scheduled jobs plus CLI commands. Every step writes durable records. Every promotion has evidence and rollback.

---

## New Record Kinds

Add these if not already present:

- `world_signal`
- `source_watch`
- `capability_model`
- `weakness`
- `learning_goal`
- `research_task`
- `research_note`
- `learning_experiment`
- `learning_eval`
- `capability_candidate`
- `promotion_request`
- `capability_score`
- `regression_watch`

Every record must include:

```json
{
  "loop_id": "learn_2026_06_02_001",
  "authority_tier": "L0|L1|L2|L3",
  "source_record_ids": [],
  "evidence": [],
  "expected_gain": "what capability should improve",
  "risk": "what can go wrong",
  "rollback": "how to undo or disable",
  "status": "candidate|running|passed|rejected|promoted|blocked"
}
```

---

## Files to Add

- `eimemory/eimemory/governance/world_watchers.py`
- `eimemory/eimemory/governance/signal_intake.py`
- `eimemory/eimemory/governance/self_model.py`
- `eimemory/eimemory/governance/curiosity.py`
- `eimemory/eimemory/governance/research_planner.py`
- `eimemory/eimemory/governance/evidence_collector.py`
- `eimemory/eimemory/governance/sandbox_lab.py`
- `eimemory/eimemory/governance/learning_eval.py`
- `eimemory/eimemory/governance/capability_distiller.py`
- `eimemory/eimemory/governance/promotion_manager.py`
- `eimemory/eimemory/governance/capability_ledger.py`
- `eimemory/eimemory/governance/autonomous_learning_full.py`
- `eimemory/tests/test_world_watchers.py`
- `eimemory/tests/test_full_autonomous_learning_loop.py`
- `eimemory/tests/test_promotion_manager.py`
- `eimemory/tests/test_capability_ledger.py`

## Files to Modify

- `eimemory/eimemory/models/records.py`
- `eimemory/eimemory/api/evolution.py`
- `eimemory/eimemory/cli/main.py`
- `eimemory/eimemory/scheduler/jobs.py`
- `eimemory/eimemory/governance/console.py`
- `eimemory/eimemory/governance/snapshot.py`
- `eimemory/README.md`

---

## Phase 0: Verify the 0.10.1 Kernel

**Outcome:** The local autonomous learning kernel exists and passes tests.

- [ ] Run `cd eimemory && python -m pytest tests/test_autonomous_learning_loop.py tests/test_self_model.py tests/test_curiosity_engine.py tests/test_learning_distillation.py -q`
- [ ] If those tests do not exist, implement the 0.10.1 plan first:
  `docs/superpowers/plans/2026-06-02-eimemory-0.10.1-autonomous-learning-loop.md`
- [ ] Confirm `eimemory learn cycle` creates:
  - 3 learning goals
  - 1 research note
  - 1 sandbox experiment
  - 1 learning eval
  - 1 capability candidate

---

## Phase 1: World Watchers

**Outcome:** eimemory can proactively observe configured sources and produce normalized `world_signal` records.

Initial watcher types:

- `local_outcome_trace`: recent eimemory traces, reflections, replay failures.
- `local_repo`: changed files, failing tests, stale TODOs, repeated error logs.
- `github_releases`: configured repositories and agent/tool ecosystem updates.
- `research_feed`: configured paper/blog/search queries.
- `tool_registry`: installed skills, CLI tools, outdated docs, new usable tools.
- `user_goal_memory`: durable goals, current projects, recurring pain points.

Rules:

- Watchers must default to dry-run unless explicitly enabled.
- Watchers must store summaries and references, not raw private dumps.
- Watchers must rate-limit and dedupe signals.
- Watchers must never send messages or mutate external systems.

Tasks:

- [ ] Add `SourceWatch` config model with fields: `name`, `kind`, `query`, `enabled`, `cadence`, `authority_tier`, `last_seen`, `dedupe_key`.
- [ ] Add `collect_world_signals(runtime, scope, watches, dry_run=True)`.
- [ ] Add dedupe by `(watch_name, signal_hash)`.
- [ ] Add tests for dry-run, dedupe, and disabled watchers.

Commands:

```bash
cd eimemory
python -m pytest tests/test_world_watchers.py -q
```

---

## Phase 2: Signal Intake and Prioritization

**Outcome:** World signals become ranked learning opportunities.

Ranking factors:

- Relevance to user goals.
- Repeated failures or corrections.
- Capability gap severity.
- Potential cost reduction.
- Potential revenue/productivity impact.
- Safety risk.
- Freshness.
- Evidence quality.

Tasks:

- [ ] Add `rank_learning_signals(signals, self_model, user_goals, max_items=20)`.
- [ ] Add score fields: `relevance`, `impact`, `urgency`, `learnability`, `risk`, `confidence`.
- [ ] Prefer high-impact, low-risk signals.
- [ ] Add deterministic fallback when no LLM is configured.

Acceptance:

- Given repeated tool-routing failures and a new local source signal, ranking picks the repeated failure first.

---

## Phase 3: Self-Model and Capability Ledger

**Outcome:** The system maintains a measurable capability map over time.

Capability dimensions:

- Search/research.
- Code implementation.
- Code review/CI.
- Tool routing.
- Memory recall.
- UUMit/ops/business.
- Office/device awareness.
- Proactive judgment.
- Safety boundary judgment.
- Communication style.

Tasks:

- [ ] Add `CapabilityLedger`.
- [ ] Store `capability_score` records after every learning cycle.
- [ ] Track score, trend, evidence count, regression count, last improved date.
- [ ] Add weekly summary generation.
- [ ] Add tests that a passing candidate improves the right capability score.

Acceptance:

- `eimemory learn ledger` prints capability scores and recent deltas.

---

## Phase 4: Curiosity Engine Full Mode

**Outcome:** The agent generates learning goals from both internal weaknesses and external signals.

Goal categories:

- `capability_gap`: I repeatedly fail at X.
- `world_change`: New tool/paper/framework affects my work.
- `efficiency_gap`: I spend too many steps/tokens on X.
- `safety_gap`: I am unsure when to act or ask.
- `opportunity`: New workflow may improve user outcome.
- `maintenance`: Existing skill/tool may be stale.

Tasks:

- [ ] Extend `generate_learning_goals` to accept ranked world signals.
- [ ] Add goal dedupe by semantic key.
- [ ] Add daily cap and priority queue.
- [ ] Add `eimemory learn goals --source world --limit 10`.

Acceptance:

- A new watcher signal can create a learning goal without a user task failure.

---

## Phase 5: Autonomous Research Planner

**Outcome:** The system can turn a learning goal into bounded research tasks.

Research task types:

- `docs_read`
- `repo_scan`
- `paper_summary`
- `benchmark_review`
- `tool_comparison`
- `local_history_review`
- `small_spike`

Rules:

- Every research note must have evidence.
- LLM output must be labeled as synthesis, not evidence.
- Claims must point to source URLs, file paths, commits, traces, or eval results.
- Private data must be summarized minimally.

Tasks:

- [ ] Add `plan_research_tasks(goal, source_policy)`.
- [ ] Add `create_research_task` and `complete_research_task`.
- [ ] Add evidence schema: `kind`, `ref`, `summary`, `confidence`.
- [ ] Add CLI: `eimemory learn research --goal <id>`.

Acceptance:

- A learning goal creates at least one research task and one research note with evidence.

---

## Phase 6: Evidence Collector Integrations

**Outcome:** Research can use approved local and public channels.

Initial collectors:

- Local eimemory store.
- Local repo files.
- GitHub public metadata via existing authenticated CLI if configured.
- Web search/fetch through approved tooling.
- Installed skills/tool inventory.

Rules:

- All network collectors must support `--dry-run`.
- Paid APIs require budget config.
- Private sources require explicit source config.

Tasks:

- [ ] Add collector interface: `collect(task) -> list[Evidence]`.
- [ ] Implement local store collector.
- [ ] Implement repo collector.
- [ ] Add placeholder adapter for web/GitHub collectors that can be disabled.
- [ ] Add source policy enforcement.

Acceptance:

- Offline tests pass without network.

---

## Phase 7: Sandbox Lab

**Outcome:** The agent can create and test candidate improvements without changing production behavior.

Candidate types:

- `skill_patch`
- `sop_patch`
- `tool_wrapper`
- `tool_route`
- `eval_case`
- `memory_rule`
- `source_policy`
- `prompt_patch`
- `scheduler_policy`

Tasks:

- [ ] Add candidate bundle schema.
- [ ] Add `create_sandbox_experiment`.
- [ ] Store generated patch text as candidate artifact.
- [ ] Add local workspace sandbox directory under eimemory data root, not repo root by default.
- [ ] Add tests that sandbox artifacts do not alter production files.

Acceptance:

- `eimemory learn experiment --goal <id>` creates a candidate bundle and rollback plan.

---

## Phase 8: Evaluation Harness

**Outcome:** Every candidate is scored before promotion.

Required scores:

- `capability`: Does it improve the target task?
- `safety`: Does it obey authority tiers?
- `cost`: Does it reduce or bound cost?
- `regression`: Does it avoid breaking existing behavior?
- `evidence`: Is the claim supported?
- `maintainability`: Is the change small and understandable?

Eval sources:

- Replay cases from outcome traces.
- Synthetic benchmark cases.
- Unit tests.
- Static checks.
- Policy checks.
- Cost estimates.

Tasks:

- [ ] Add `run_learning_eval(candidate, eval_suite)`.
- [ ] Add policy checks for L3 actions.
- [ ] Add regression threshold: safety and regression must be >= 0.9.
- [ ] Add CLI: `eimemory learn eval --candidate <id>`.

Acceptance:

- Unsafe candidate is rejected even if capability score is high.

---

## Phase 9: Promotion Manager

**Outcome:** Passing candidates move to the right promotion lane.

Promotion lanes:

- L0 record-only: mark learned, update ledger.
- L1 local draft: write draft skill/SOP/eval file.
- L2 review diff: create patch/branch/PR request.
- L3 blocked: create approval request only.

Tasks:

- [ ] Add `PromotionManager`.
- [ ] Add `promotion_request` records.
- [ ] Add `eimemory learn promote --candidate <id> --dry-run`.
- [ ] Add `--apply` only for L0/L1.
- [ ] For L2, generate patch bundle and instructions for Codex/GitHub.
- [ ] For L3, refuse execution and produce approval summary.

Acceptance:

- L2 candidate creates a reviewable diff but does not merge.
- L3 candidate cannot be applied from CLI.

---

## Phase 10: PR/Diff Promotion for Codex

**Outcome:** Codex can pick up approved L2 candidates and implement them cleanly.

Tasks:

- [ ] Add `eimemory learn codex-brief --candidate <id>`.
- [ ] Output:
  - goal
  - evidence
  - files likely affected
  - tests to run
  - safety tier
  - rollback
  - done criteria
- [ ] Add optional `--write-plan` to create a Superpowers plan from a candidate.
- [ ] Add test for generated brief content.

Acceptance:

- Tomorrow Codex can run one command and get a complete implementation brief.

---

## Phase 11: Scheduler and Watcher Runtime

**Outcome:** The system can run daily/weekly without manual prompting.

Jobs:

- `world_watch`: collect signals.
- `learning_cycle`: generate goals/research/experiment/eval/candidates.
- `promotion_review`: summarize pending candidates.
- `capability_weekly`: capability curve report.
- `regression_watch`: check whether promoted candidates caused failures.

Tasks:

- [ ] Add scheduler config flags.
- [ ] Default all jobs off unless explicitly enabled.
- [ ] Add dry-run mode.
- [ ] Add job status to governance snapshot.
- [ ] Add console view.

Acceptance:

- `eimemory scheduler run learning_cycle --dry-run` completes without external side effects.

---

## Phase 12: Governance Console

**Outcome:** Human can see what the system is learning and what it wants to change.

Console sections:

- Active watchers.
- New world signals.
- Top learning goals.
- Research tasks.
- Sandbox experiments.
- Passing/rejected candidates.
- Promotion requests by tier.
- Capability score deltas.
- Blocked L3 actions.

Tasks:

- [ ] Add snapshot fields.
- [ ] Add console rendering.
- [ ] Add JSON output mode for OpenClaw integration.

Acceptance:

- `eimemory governance snapshot --json` includes full autonomous learning state.

---

## Phase 13: Regression Watch

**Outcome:** The system checks whether promoted learning actually helped.

Tasks:

- [ ] Link each promoted candidate to target eval cases.
- [ ] Re-run target evals after promotion.
- [ ] Track before/after score.
- [ ] Create `regression_watch` record if score drops.
- [ ] Automatically demote/disable L1 local drafts on regression.
- [ ] L2 regressions create review alerts, not automatic reverts.

Acceptance:

- A failing post-promotion eval creates a regression record and rollback recommendation.

---

## Phase 14: CLI Surface

Commands:

```bash
eimemory learn watch --dry-run
eimemory learn cycle --full --dry-run
eimemory learn goals --limit 10
eimemory learn research --goal <goal_id>
eimemory learn experiment --goal <goal_id>
eimemory learn eval --candidate <candidate_id>
eimemory learn promote --candidate <candidate_id> --dry-run
eimemory learn codex-brief --candidate <candidate_id>
eimemory learn ledger
```

Acceptance:

- All commands support `--json`.
- All mutating commands support `--dry-run`.
- L3 actions cannot be executed by CLI.

---

## Phase 15: End-to-End Acceptance Scenario

Create an offline test that simulates:

1. A local outcome trace shows repeated tool-routing waste.
2. A watcher creates a `world_signal`.
3. Self-model marks `tool.routing` weak.
4. Curiosity creates a learning goal.
5. Research planner creates a local-history research task.
6. Evidence collector reads local records.
7. Research note summarizes evidence.
8. Sandbox lab creates a `tool_route` candidate.
9. Eval harness runs replay cases.
10. Candidate passes safety/regression.
11. Promotion manager creates an L2 review diff or brief.
12. Capability ledger records expected improvement.

Test command:

```bash
cd eimemory
python -m pytest tests/test_full_autonomous_learning_loop.py -q
```

---

## Done Criteria

- [ ] Full learning cycle works offline without real LLM or network.
- [ ] World watchers can run in dry-run and store deduped signals.
- [ ] Learning goals can be created without direct user prompts.
- [ ] Research notes require evidence.
- [ ] Sandbox experiments never mutate production files.
- [ ] Eval rejects unsafe or regressive candidates.
- [ ] L0/L1/L2/L3 authority tiers are enforced in tests.
- [ ] L2 creates reviewable Codex brief/diff, not automatic merge.
- [ ] L3 cannot execute from CLI.
- [ ] Capability ledger shows before/after scores.
- [ ] Governance snapshot exposes watcher/goal/candidate/promotion status.
- [ ] README explains full autonomous learning boundary.

---

## Tomorrow Codex Handoff

Recommended execution order:

1. Run Phase 0 and make the 0.10.1 kernel pass.
2. Implement record kinds and source watch schema.
3. Implement world watchers in dry-run mode only.
4. Implement signal ranking and full curiosity goals.
5. Implement research planner with local collectors first.
6. Implement sandbox/eval/promotion manager.
7. Add CLI commands.
8. Add scheduler entries disabled by default.
9. Run full test suite.
10. Produce a short implementation report with:
    - files changed
    - commands run
    - L3 actions blocked
    - remaining risks

Suggested first Codex command:

```bash
cd /home/darrow/.openclaw/workspace
codex --cd eimemory "Use superpowers-open:executing-plans and implement docs/superpowers/plans/2026-06-02-eimemory-full-autonomous-learning-system.md phase by phase. Start with Phase 0 and stop after the first green end-to-end offline full learning cycle."
```

