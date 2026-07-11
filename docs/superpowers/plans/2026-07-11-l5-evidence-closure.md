# L5 Evidence Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make eimemory L5 readiness and closure rehearsal depend only on executed, verified, reversible evidence.

**Architecture:** Add compact evidence-summary helpers at the decision boundaries instead of changing storage schemas. Keep raw diagnostic counts, but use verified replay, complete assessment, and verified code-patch promotion summaries for L5 decisions and hard metrics.

**Tech Stack:** Python 3.11+, pytest, existing Runtime/RecordEnvelope storage, git/systemd immutable release flow.

## Global Constraints

- Preserve the stricter 1.9.14 measured gates; never restore default-pass behavior.
- Do not generate fake production outcomes or replay evidence.
- Do not run the full test suite; use version-range and L5 layered tests.
- Deploy only from `/dev-project/eimemory` and verify `/opt/eimemory/current` plus health version/commit identity.

---

### Task 1: Verified replay and assessment readiness

**Files:**
- Modify: `eimemory/governance/l5_readiness.py`
- Modify: `tests/test_l5_readiness.py`

**Interfaces:**
- Consumes: stored `replay_result` and `l5_assessment` records.
- Produces: `verified_replay` and `latest_l5_assessment` report fields used by `_stage_for`.

- [ ] Add tests proving `not_run` replay records and incomplete assessments cannot produce L5.
- [ ] Run the two new tests and confirm they fail against 1.9.14.
- [ ] Implement replay and assessment summaries; require count >= 10, pass rate >= 0.8, `complete=true`, and zero missing evidence.
- [ ] Run `tests/test_l5_readiness.py` and confirm it passes.

### Task 2: Fail-closed closure rehearsal

**Files:**
- Modify: `eimemory/governance/closure_rehearsal.py`
- Modify: `tests/test_l5_closure_rehearsal.py`

**Interfaces:**
- Consumes: capability replay pack case results and downstream skill/rollback/readiness reports.
- Produces: `closure_complete`, `blocked_reasons`, and an `ok` value that is true only for a fully executed rehearsal.

- [ ] Add tests proving missing replay executors do not write success outcomes or reusable skill evidence.
- [ ] Run the new test and confirm the existing 1.9.14 behavior fails it.
- [ ] Gate SOP replay status, skill promotion/call, success outcome, and top-level `ok` behind verified replay completion.
- [ ] Run closure rehearsal, replay pack, correction replay, and dashboard tests.

### Task 3: Verified patch hard metrics

**Files:**
- Modify: `eimemory/governance/capability_dashboard.py`
- Modify: `tests/test_capability_dashboard_metrics.py`

**Interfaces:**
- Consumes: explicit code-patch promotion records and their gate/side-effect evidence.
- Produces: `patch_promotion_success_rate` plus the backward-compatible `auto_patch_success_rate` alias and evidence quality counts.

- [ ] Add tests proving generic promotions and status-only code-patch records are not successful patch evidence.
- [ ] Run the new tests and confirm red failures.
- [ ] Implement explicit patch selection and verified-success checks.
- [ ] Run dashboard, promotion manager, autonomous evolution, and L5 readiness tests.

### Task 4: Release, deploy, and live audit

**Files:**
- Modify: `pyproject.toml`
- Modify: `eimemory/version.py`
- Modify: `docs/l5-roadmap-spec.md`

**Interfaces:**
- Consumes: verified local diff and layered test evidence.
- Produces: patch release 1.9.15, Git commit, immutable server release, health evidence, and current L5 report.

- [ ] Update documentation to state the verified replay/assessment/patch rules and bump both version files to 1.9.15.
- [ ] Run the focused 1.9.10-1.9.14 audit suite, `compileall`, and `git diff --check`.
- [ ] Review the complete diff for evidence bypasses and secret leakage.
- [ ] Commit, fast-forward local `master`, push `origin/master`, and deploy the exact commit from `/dev-project/eimemory`.
- [ ] Verify remote layered tests, both services, health endpoints, current release symlink, version/commit identity, and live `l5-readiness`.
