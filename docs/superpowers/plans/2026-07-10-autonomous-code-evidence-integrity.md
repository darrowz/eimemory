# Autonomous Code Evidence Integrity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace synthetic autonomous-code promotion evidence with persisted isolated execution evidence and release the repair as 1.9.10.

**Architecture:** `promotion_manager` owns the reusable isolated preflight and canonical code-evidence gate. `autonomous_evolution` consumes that preflight in the isolated evaluator and passes the same evidence into promotion. The real repository still executes verification a second time before commit or deployment.

**Tech Stack:** Python 3.11+, pytest, pathlib/tempfile/shutil, Git worktrees, JSONL/SQLite record store, systemd immutable deployment.

## Global Constraints

- Preserve fail-closed behavior for missing, malformed, skipped, and unavailable evidence.
- Never mutate files outside declared `allowed_files`.
- Keep `docs/audit/` untracked and untouched.
- Deploy only from honxin `/dev-project/eimemory` and verify `/health` version and commit.

---

### Task 1: Lock the evidence contract with failing tests

**Files:**
- Create: `tests/test_policy_replay_evidence.py`
- Modify: `tests/test_promotion_manager.py`
- Modify: `tests/test_autonomous_evolution_controller.py`

**Interfaces:**
- Consumes: `evaluate_replay_gate`, `_run_patch_commands`, `promote_candidate`, `run_autonomous_evolution`
- Produces: regression expectations for `executed`, canonical preflight evidence, and pre-mutation blocking

- [ ] **Step 1: Add a structural replay test**

Assert a valid replay-case definition returns `case_valid=true` and
`executed=false`, proving it is not execution evidence.

- [ ] **Step 2: Add a required-command test**

Call `_run_patch_commands([], phase="verify")` and assert `ok=false`,
`skipped=true`, and `error_type=missing_required_commands`.

- [ ] **Step 3: Add preflight reachability tests**

Create real temporary repositories and assert failed verification or a forged
gate bundle cannot mutate the target file.

- [ ] **Step 4: Run the focused tests and confirm RED**

Run:

```powershell
python -m pytest -q tests/test_policy_replay_evidence.py tests/test_promotion_manager.py tests/test_autonomous_evolution_controller.py
```

Expected: failures caused by missing execution metadata, unsupported `required`,
and synthetic promotion evidence.

### Task 2: Implement isolated code preflight

**Files:**
- Modify: `eimemory/governance/promotion_manager.py`

**Interfaces:**
- Produces: `run_code_patch_preflight(runtime, patch, scope, loop_id) -> dict`
- Produces: a persisted `replay_result` whose report type is `code_patch_preflight`
- Consumes: existing path allowlist, file update, command execution, and commit helpers

- [ ] **Step 1: Add deterministic patch identity**

Hash repository root, base commit, normalized allowed files, file updates, and
verification commands into `patch_digest`.

- [ ] **Step 2: Prepare the isolated repository**

Use `git worktree add --detach <sandbox> HEAD` for Git repositories; otherwise
copy the fixture repository while excluding caches and virtual environments.

- [ ] **Step 3: Apply and verify in isolation**

Reuse `_apply_file_updates` and `_run_patch_commands(..., phase="verify")`. Treat
missing commands, patch failures, timeouts, non-zero exits, and skipped reports
as failures.

- [ ] **Step 4: Persist the report**

Write a scoped `replay_result` containing `executed`, `verdict`, base commit,
patch digest, sandbox mode, verification reports, and cleanup status.

- [ ] **Step 5: Canonicalize code gates before rollout**

Reuse a matching persisted preflight or run a fresh one. Replace caller-supplied
code replay/canary/doctor/smoke evidence with fields derived from that report.

### Task 3: Wire autonomous evolution to real evidence

**Files:**
- Modify: `eimemory/governance/policy_replay.py`
- Modify: `eimemory/governance/autonomous_evolution.py`

**Interfaces:**
- Consumes: `run_code_patch_preflight`
- Produces: isolated evaluator packets with actual verification reports
- Produces: promotion gate bundles referencing one persisted preflight record

- [ ] **Step 1: Mark replay definitions honestly**

Return `case_valid`, `executed=false`, and `evidence_kind=case_definition` from
`evaluate_replay_gate` without treating it as real execution.

- [ ] **Step 2: Replace synthetic isolated evidence**

Run code preflight before constructing the evaluation packet. Pass its command
reports as `verification_results` and its executable replay summary as
`real_task_replay`.

- [ ] **Step 3: Replace the synthetic gate bundle**

Build evidence, canary, doctor, smoke, health, and replay fields from the
persisted preflight and isolated evaluator result. Do not emit prompt gates for
code targets.

- [ ] **Step 4: Run focused and neighboring suites**

Run:

```powershell
python -m pytest -q tests/test_policy_replay_evidence.py tests/test_autonomous_evolution_controller.py tests/test_promotion_manager.py tests/test_isolated_evaluator_harness.py
```

Expected: all pass.

### Task 4: Release and deploy 1.9.10

**Files:**
- Modify: `pyproject.toml`
- Modify: `eimemory/version.py`

**Interfaces:**
- Produces: package/runtime version `1.9.10`

- [ ] **Step 1: Run the full local verification gate**

Run full pytest, compileall, and `git diff --check`.

- [ ] **Step 2: Bump both version declarations**

Set `pyproject.toml` and `eimemory/version.py` to `1.9.10`, then run
`tests/test_version.py` and the full suite again.

- [ ] **Step 3: Commit and push**

Stage only the planned source, test, documentation, and version files. Commit
with `fix(governance): require executed code patch evidence`, then push master.

- [ ] **Step 4: Verify and deploy from honxin**

In `/dev-project/eimemory`, use the repo venv for focused tests and compileall,
confirm `git diff --check`, push/synchronize `origin/master`, run
`deploy/install_immutable_release.sh <commit>`, and restart the user services.

- [ ] **Step 5: Verify production**

Require `eimemory-rpc.service=active`, `openclaw-gateway.service=active`, and
`/health` reporting `version=1.9.10` and the deployed commit.
