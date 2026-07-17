# L5 Evidence Quality Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Measure verified real OpenClaw tasks and attribute failures without weakening L5 readiness.

**Architecture:** Extend deterministic outcome diagnosis, persist the derived layer, and aggregate strictly verified business traces separately from deployment acceptance.

**Tech Stack:** Python 3.11+, pytest, SQLite-backed Runtime records.

## Global Constraints

- Do not change L5 acceptance thresholds or evidence rules.
- Do not count unresolved or cross-scope evidence.
- Do not infer a blame layer without a deterministic signal.
- Complete version bump, test, commit, push, deploy, and health verification.

---

### Task 1: Blame-layer attribution

**Files:** `eimemory/experience/diagnosis.py`, `eimemory/experience/outcome.py`, `tests/test_experience_outcome.py`

- [x] Add failing tests for all stable layer mappings and metadata persistence.
- [x] Confirm the tests fail because `blame_layer` is absent.
- [x] Implement deterministic mapping and metadata hoisting.
- [x] Confirm focused tests pass.

### Task 2: Verified real-task dashboard

**Files:** `eimemory/governance/capability_dashboard.py`, `tests/test_capability_dashboard_metrics.py`

- [x] Add a failing test for valid, forged, and rehearsal traces.
- [x] Confirm new dashboard keys are absent before implementation.
- [x] Implement same-scope evidence validation and blame aggregation.
- [x] Confirm focused dashboard tests pass.

### Task 3: Release verification

**Files:** `pyproject.toml`, `eimemory/version.py`, `deploy/systemd/*.service`, `tests/test_version.py`

- [x] Bump `1.9.54` to `1.9.55`.
- [x] Run the full suite with zero failures.
- [ ] Commit and push.
- [ ] Deploy the immutable release and restart the RPC service.
- [ ] Verify health, import root, production metrics, readiness, and git status.
