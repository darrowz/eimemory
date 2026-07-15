# L5 Idle Readiness Semantics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent explicit idle L5 assessments from replacing the last verified global readiness state while preserving fail-closed behavior for real failures.

**Architecture:** Assessment creation records normalized activity state. Readiness selection scans snapshots in SQLite insertion order and skips only explicit idle/no-change snapshots. Audit snapshots remain immutable and real failures remain authoritative.

**Tech Stack:** Python 3.12, pytest, SQLite-backed record store, systemd user service.

## Global Constraints

- Preserve all L5 assessment snapshots.
- Skip only explicit `idle` or `no_change` activity.
- Do not reinterpret historical records without explicit activity evidence.
- Bump the package version and complete commit, push, deploy, and health verification.

---

### Task 1: Separate activity from global readiness

**Files:**
- Modify: `eimemory/governance/l5_loop.py`
- Modify: `eimemory/governance/l5_readiness.py`
- Test: `tests/test_l5_consciousness_loop.py`

**Interfaces:**
- Consumes: `autonomous_learning.activity_status` and persisted `l5_assessment` records.
- Produces: `assessment.activity_status`, `assessment.global_readiness`, and idle-aware `_latest_l5_assessment` selection.

- [x] **Step 1: Verify the existing idle tests fail with missing `activity_status`.**
- [x] **Step 2: Normalize `idle` and `no_change` to `idle` during assessment.**
- [x] **Step 3: Persist activity status and expose global readiness.**
- [x] **Step 4: Select the latest non-idle snapshot only when the newest snapshot is explicitly idle.**
- [x] **Step 5: Add a regression test proving active failures still downgrade readiness.**
- [x] **Step 6: Run `python -m pytest -q tests/test_l5_consciousness_loop.py`; expect all tests to pass.**

### Task 2: Release and verify

**Files:**
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: verified source tree.
- Produces: immutable release directory and healthy `eimemory-rpc.service` on the new version.

- [ ] **Step 1: Bump version from `1.9.48` to `1.9.49`.**
- [ ] **Step 2: Run the full pytest suite and confirm zero failures.**
- [ ] **Step 3: Commit and push `master` to `origin/master`.**
- [ ] **Step 4: Deploy the commit to `/opt/eimemory/releases/<commit>` and repoint `/opt/eimemory/current`.**
- [ ] **Step 5: Restart `eimemory-rpc.service` and verify `/health` reports version `1.9.49` and the deployed commit.**
- [ ] **Step 6: Run a production-safe L5 semantic probe against an isolated temporary store.**
