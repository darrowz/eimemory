# eimemory 0.9.1 Self-Evolution Closed Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade eimemory/OpenClaw from report-only autonomous evolution to a production-safe behavior-strategy self-evolution loop, plus a code-evolution sandbox that can propose and verify patches without unattended production deployment.

**Architecture:** Keep strategy evolution and code evolution separate. Strategy evolution may automatically apply only when trust, replay, rollout, budget, and rollback gates pass. Code evolution runs in sandbox/worktree mode and produces patch candidates, verification reports, and release-candidate metadata, but 0.9.1 does not auto-merge or auto-deploy code.

**Tech Stack:** Python 3.11+, SQLite-backed RuntimeStore, existing eimemory governance/runtime/CLI/scheduler APIs, OpenClaw hooks, pytest, git worktrees/archive deployment.

---

## Non-Negotiable Boundaries

- Web learning never directly promotes live policy.
- Agent-inferred outcomes never directly promote live policy.
- Nightly persists the gated autonomous-evolution report in preview mode until staged rollout (`candidate -> shadow -> active`) is fully implemented; explicit CLI `--apply` can still promote gate-passed strategy patches.
- Code evolution may generate branches/patch candidates/reports, but cannot push, merge, or deploy production in 0.9.1.
- Every promotion must have a ledger entry, replay evidence, source trust evidence, and rollback path.
- Every promoted pattern must be attributable in later outcomes so rollback is precise.

## Workstreams

### Task 1: Strategy Trust And Replay Gates

**Files:**
- Create: `eimemory/governance/policy_trust.py`
- Create: `eimemory/governance/policy_replay.py`
- Modify: `eimemory/governance/autonomous_evolution.py`
- Test: `tests/test_policy_trust_replay.py`

**Requirements:**
- Classify outcomes into `user_explicit`, `system_verified`, `trusted_hook`, `agent_inferred`, `web_external`, `unknown`.
- Only `user_explicit`, `system_verified`, and strong `trusted_hook` may pass automatic apply.
- Reject high-risk action text with structured action categories, not only substring checks.
- Build replay cases from positive correction, negative similar expressions, and regression seed intent patterns.
- Report `trusted_gate`, `replay_gate`, and `safe_action_gate` per patch.
- Existing `--apply` still works, but applied patches require all gates.

**TDD anchors:**
- Agent-inferred bad outcome produces opportunity but blocked by `trusted_gate`.
- User explicit correction with verification can pass trust gate.
- Web hypothesis is always replay-only and blocked from apply.
- Candidate with positive replay but negative misfire is blocked.
- Candidate with regression against seeded "最高星项目" pattern is blocked.

### Task 2: Policy Lifecycle, Ledger, Budget, And Rollback

**Files:**
- Create: `eimemory/governance/policy_rollout.py`
- Modify: `eimemory/events.py`
- Modify: `eimemory/storage/sqlite_store.py`
- Modify: `eimemory/storage/runtime_store.py`
- Modify: `eimemory/api/runtime.py`
- Test: `tests/test_policy_rollout.py`

**Requirements:**
- Intent pattern payloads support `status`: `candidate`, `shadow`, `active`, `rolled_back`, `quarantined`.
- `search_policy()` returns only `active` by default; optional context can include `include_shadow=True`.
- Promotion ledger records `promotion_id`, source opportunity, trust report, replay report, applied pattern id, budget decision, and rollback policy.
- Daily budget defaults: max 3 automatic promotions, max 5 automatic rollbacks.
- Circuit breaker stops auto-apply if the same run has any safety gate error or too many blocked risky candidates.
- `record_outcome()` can trigger rollback when a bad outcome references promoted policy ids.
- Rollback changes pattern status to `rolled_back`, stores `last_rollback_reason`, writes reflection report, and creates a follow-up opportunity payload.

**TDD anchors:**
- Shadow/rolled_back patterns are not returned by normal `search_policy`.
- Gated promotion writes ledger and active pattern.
- Two bad outcomes attributed to a promoted pattern roll it back.
- User explicit "不是这个意思/别这样" rolls back immediately.
- Budget exhaustion blocks further promotion and reports `budget_exhausted`.

### Task 3: OpenClaw Policy Attribution And Trusted Outcomes

**Files:**
- Modify: `eimemory/adapters/openclaw/hooks.py`
- Test: `tests/test_openclaw_policy_attribution.py`
- Add targeted assertions in `tests/test_adapters.py` if needed.

**Requirements:**
- Before prompt build stores `policy_suggestion_ids`, `policy_sources`, and `matched_event_type` in recall audit metadata/content.
- Terminal memory records which policy ids were visible/used, via `task_context.policy_suggestion_ids` or recall audit lookup by session.
- User correction classification writes `source_trust=user_explicit`.
- System-verified success writes `source_trust=system_verified` when health/test/verification fields exist.
- Plain agent completion writes `source_trust=agent_inferred`.
- Outcomes carry `policy_attribution` so rollback can map bad feedback to promoted patterns.

**TDD anchors:**
- Policy suggestions injected before memory items include ids in audit.
- Agent end forwards attributed policy ids into event/outcome.
- User correction outcome gets `source_trust=user_explicit`.
- Unverified assistant-only outcome gets `source_trust=agent_inferred` and cannot promote.

### Task 4: Code Evolution Sandbox

**Files:**
- Create: `eimemory/governance/code_evolution.py`
- Modify: `eimemory/api/runtime.py`
- Modify: `eimemory/cli/main.py`
- Test: `tests/test_code_evolution_sandbox.py`

**Requirements:**
- Classify incidents as `policy_fixable`, `config_fixable`, `code_fixable`, `infra_fixable`, or `unknown`.
- `code_fixable` can create a sandbox plan/report with worktree path, branch name, allowed files, verification commands, and rollback notes.
- Default mode is dry-run report. No commit, push, merge, deploy, or production release change.
- Allow injecting a fake runner in tests; production implementation may use git commands only when explicitly requested by CLI flags.
- CLI: `eimemory evolve code-sandbox --incident-json <json-or-path> [--create-worktree]`.
- Report persists as reflection when `--persist-report` is set.

**TDD anchors:**
- A policy issue is not sent to code sandbox.
- A code-fixable incident produces a sandbox candidate with verification harness.
- Dry-run does not create a worktree.
- `--create-worktree` creates isolated path under configured root/tmp and never under production release.

### Task 5: Scheduler, CLI, Version, And End-To-End Gates

**Files:**
- Modify: `eimemory/scheduler/jobs.py`
- Modify: `eimemory/cli/main.py`
- Modify: `pyproject.toml`
- Modify: `eimemory/version.py`
- Modify: `tests/test_version.py`
- Test: `tests/test_autonomous_evolution_platform.py`
- Test: `tests/test_autonomous_evolution_controller.py`

**Requirements:**
- Nightly runs `run_autonomous_evolution(apply=False, persist_report=True)` as a safe report-only loop until staged rollout is implemented.
- CLI `eimemory evolve gates` shows recent promotion/rollback/budget status.
- CLI `eimemory evolve rollback --pattern-id ... --reason ...` calls rollback API.
- Version becomes `0.9.1`.
- Reports include `gate_summary`, `promotion_ledger_ids`, `rolled_back_count`, and `circuit_breaker`.

**TDD anchors:**
- Nightly persists the autonomous evolution report without directly applying patches.
- A trusted/replayed correction applies during autonomous evolution.
- Gate status CLI prints latest ledger/budget state.
- Manual rollback CLI hides the pattern from `search_policy`.

## Verification Matrix

- `python -m pytest tests/test_policy_trust_replay.py tests/test_policy_rollout.py tests/test_openclaw_policy_attribution.py tests/test_code_evolution_sandbox.py -q`
- `python -m pytest tests/test_autonomous_evolution_controller.py tests/test_autonomous_evolution_platform.py tests/test_event_memory.py tests/test_judgment.py tests/test_rule_evolution_loop.py -q`
- `python -m pytest -q -k "not openclaw_js_bridge"` locally if Windows node remains blocked.
- `python -m compileall eimemory`
- `git diff --check`
- `code-review-graph update --repo D:\github\ei-workspace\repos\eimemory --base HEAD~1`
- `rtk code-review-graph update --repo D:\github\ei-workspace\repos\eimemory --base HEAD~1`

## Deployment Acceptance

- eimemory package version is `0.9.1`.
- honxin `/opt/eimemory/current` points to the new immutable release.
- honxin `/dev-project/eimemory` source repo matches the deployed commit and is clean.
- `eimemory-rpc.service`, `openclaw-gateway.service`, and `openclaw-loopback-proxy.service` are active.
- `curl http://127.0.0.1:8091/health` returns `ok: true`.
- OpenClaw gateway health returns live.
- Remote CLI smoke:
  - `eimemory evolve autonomous --apply --max-apply 1 --persist-report` returns gate summary.
  - untrusted outcome does not apply.
  - trusted replayed correction can apply in an isolated temp root.
  - rollback hides an active promoted policy.
  - `eimemory evolve code-sandbox --incident-json ...` returns dry-run candidate and does not deploy.
