# L5 Monotonic Real Replay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make L5 maturity version-neutral and monotonic, allow current-code replay of verified real production evidence to close the real-business gate, and permit downgrade only through a confirmed fatal-regression incident.

**Architecture:** Separate current-release validation from accumulated maturity. Keep release-bound health diagnostics strict, persist a scope-bound maturity checkpoint whose identity contains no version or commit, and combine live-task evidence with a provenance-verified real-replay alternative. Preserve `openclaw-loop-watch`; keep `openclaw-stuck-watchdog` and the Feishu reply watchdog retired.

**Tech Stack:** Python 3, SQLite record store, pytest, Bash/systemd deployment scripts, GitHub Actions.

## Global Execution Order

- Write every new or changed regression test before implementation.
- Run the complete focused regression command once to capture the expected red state.
- Implement all source changes without interleaved test runs.
- Run the focused regression command once after implementation.
- Run the full project verification once after the focused suite passes.
- Do not restore the four deleted historical audit artifacts or the untracked `CODE_AUDIT_REPORT.md`.
- Do not remove, rename, disable, or version-bind `openclaw-loop-watch`.

---

### Task 1: Lock the verified-real-replay contract with tests

**Files:**
- Create: `tests/test_real_replay_gate.py`
- Modify: `tests/test_public_benchmarks_and_replay.py`
- Modify: `tests/test_replay_dataset.py`

**Interfaces:**
- `run_real_task_replay(runtime, dataset, *, seed=True, persist_report=False) -> dict`
- `build_verified_real_replay_summary(runtime, *, scope, limit=500) -> dict`

- [ ] Add fixtures that create same-scope `outcome_trace.v1` records through the production outcome API, with trusted terminal sources, `rehearsal=False`, successful status, and normalized task types.
- [ ] Add replay cases that reference those records through `source_record_id`.
- [ ] Assert persisted samples contain only `source_record_id`, `source_evidence_digest`, normalized `source_task_type`, replay verdict, latency, execution ID, and current package digest.
- [ ] Assert raw message text, platform identifiers, credentials, and source payloads are absent from persisted replay evidence.
- [ ] Reject missing, duplicate, synthetic, rehearsal, bootstrap, probe, malformed, inactive, cross-scope, unsuccessful, and non-terminal sources.
- [ ] Keep legacy manually supplied replay datasets executable, but prove they cannot satisfy the verified-real-replay gate.
- [ ] Cover the exact thresholds: 10 unique sources, 5 task types, pass rate at least `0.8`; cover 9 sources, 4 types, and pass rate below `0.8`.
- [ ] Prove older-release source evidence is accepted only when the replay report contains the current `runtime_package_tree_digest()`.

Expected qualifying report fragment:

```python
{
    "schema_version": "real_task_replay.v1",
    "real_provenance_contract": "verified_real_replay.v1",
    "package_tree_digest": runtime_package_tree_digest(),
    "verified_real_sample_count": 10,
    "verified_real_task_types": 5,
}
```

---

### Task 2: Lock the combined business gate and release closure with tests

**Files:**
- Modify: `tests/test_l5_readiness.py`
- Modify: `tests/test_release_closure.py`
- Modify: `tests/test_l5_closure_rehearsal.py`
- Modify: `tests/test_production_real_query_gate.py`

**Interfaces:**
- `build_l5_readiness_report(...) -> dict`
- `readiness_gate_status(report) -> tuple[bool, list[str]]`

- [ ] Add `real_business_gate` assertions with `accepted_path` equal to `live_tasks`, `real_replay`, or an empty string.
- [ ] Prove live-task and verified-real-replay paths independently close the gate.
- [ ] Prove the existing `live_task_gate` remains present and diagnostic.
- [ ] Prove release closure and closure rehearsal use the combined gate instead of requiring only current-release live-task accumulation.
- [ ] Prove current-release safety failures remain visible under `release_validation` and can block operation without lowering accumulated maturity.
- [ ] Prove `openclaw-loop-watch` deployment/runtime checks remain in place and no stuck-watchdog check is reintroduced.

Combined gate shape:

```python
{
    "ok": live_task_gate["ok"] or verified_real_replay["ok"],
    "accepted_path": (
        "live_tasks" if live_task_gate["ok"]
        else "real_replay" if verified_real_replay["ok"]
        else ""
    ),
    "live_tasks": live_task_gate,
    "real_replay": verified_real_replay,
}
```

---

### Task 3: Lock monotonic maturity and fatal downgrade with tests

**Files:**
- Create: `tests/test_l5_maturity.py`
- Modify: `tests/test_l5_readiness.py`

**Interfaces:**
- `apply_monotonic_maturity(runtime, *, scope, observed_stage, observed_score, persist, loop_id) -> dict`
- `resolve_maturity_checkpoint(runtime, *, scope) -> dict`

- [ ] Prove stage ordering is exactly `L3.5 < L4 < L4.5 < L5`.
- [ ] Prove version, commit, release session, and deployment receipt changes cannot lower or select a different checkpoint.
- [ ] Prove a lower observation returns the prior effective stage plus a regression warning.
- [ ] Prove a higher observation advances the checkpoint and equal observation holds it.
- [ ] Bootstrap from the highest valid historical same-scope L5 readiness record.
- [ ] Accept a downgrade only for a newer same-scope active `incident` with `incident_type=l5_fatal_regression`, `severity=critical`, `fatal=True`, `status=confirmed`, a recognized lower `target_stage`, non-empty `evidence_record_ids`, and non-empty `confirmed_by`.
- [ ] Reject non-critical, unconfirmed, cross-scope, malformed, evidence-free, inactive, older, and non-lowering incidents.
- [ ] Prove a fatal downgrade persists a replacement checkpoint with the incident reference and is not undone by an ordinary later release report.

Checkpoint identity input:

```python
{
    "schema_version": "l5_maturity_checkpoint.v1",
    "scope": scope.to_dict(),
}
```

It must not contain a version, commit, deployment receipt, or release session.

---

### Task 4: Capture the consolidated red state

**Files:** no source changes.

- [ ] Run once:

```powershell
python -m pytest -q tests/test_real_replay_gate.py tests/test_public_benchmarks_and_replay.py tests/test_replay_dataset.py tests/test_l5_maturity.py tests/test_l5_readiness.py tests/test_release_closure.py tests/test_l5_closure_rehearsal.py tests/test_production_real_query_gate.py
```

- [ ] Record failures as missing-contract evidence; do not modify implementation until every planned regression test is present.

---

### Task 5: Add source-bound provenance to real replay

**Files:**
- Modify: `eimemory/evaluation/task_replay.py`

- [ ] Add a private validator that reloads `source_record_id` with the exact replay scope and accepts only active trusted `outcome_trace.v1` production records.
- [ ] Canonicalize and hash a safe provenance projection instead of copying source content.
- [ ] Deduplicate source record IDs within one replay execution.
- [ ] Mark legacy/manual and rejected cases with explicit non-qualifying reasons while preserving their existing execution behavior.
- [ ] Bind every persisted replay report to `runtime_package_tree_digest()` and `verified_real_replay.v1`.
- [ ] Add summary counts and normalized task-type cardinality derived only from accepted executed cases.

Validator result:

```python
{
    "ok": True,
    "source_record_id": source.id,
    "source_evidence_digest": stable_digest(safe_projection),
    "task_type": normalized_task_type,
    "reason": "",
}
```

---

### Task 6: Build the current-code verified-real-replay gate

**Files:**
- Create: `eimemory/governance/real_replay_gate.py`

- [ ] Select the newest same-scope persisted `eimemory.real_task_replay` report using the verified provenance contract and current package digest.
- [ ] Independently reload and revalidate every source record rather than trusting replay labels or stored counts.
- [ ] Count distinct source IDs once, include all accepted executed verdicts in the pass-rate denominator, and reject `not_run` or malformed verdicts.
- [ ] Return stable deficits and rejection reasons without exposing raw source payloads.

Public constants and signature:

```python
MIN_VERIFIED_REAL_REPLAY_SAMPLES = 10
MIN_VERIFIED_REAL_REPLAY_TASK_TYPES = 5
MIN_VERIFIED_REAL_REPLAY_PASS_RATE = 0.8

def build_verified_real_replay_summary(
    runtime,
    *,
    scope,
    limit: int = 500,
) -> dict:
    ...
```

---

### Task 7: Implement version-neutral maturity checkpoints

**Files:**
- Create: `eimemory/governance/l5_maturity.py`

- [ ] Resolve the highest valid same-scope maturity checkpoint and bootstrap from historical readiness evidence when no checkpoint exists.
- [ ] Compute effective maturity as the maximum of prior checkpoint and observed stage during normal operation.
- [ ] Persist only advancement or an authorized fatal downgrade; ordinary lower observations write no lower checkpoint.
- [ ] Keep fatal downgrade active across later ordinary releases until a newer valid maturity advancement explicitly supersedes it with fresh observed evidence above the downgraded stage.
- [ ] Expose transition, checkpoint ID, downgrade incident ID, observed/effective stages, and regression warning.

---

### Task 8: Integrate maturity and the combined gate

**Files:**
- Modify: `eimemory/governance/l5_readiness.py`
- Modify: `eimemory/governance/release_closure.py`
- Modify: `eimemory/governance/closure_rehearsal.py`

- [ ] Build the verified replay summary and `real_business_gate`.
- [ ] Use the combined business gate for observed L5 promotion and readiness status.
- [ ] Apply monotonic maturity after computing the complete observed release report.
- [ ] Preserve compatibility fields while adding `observed_stage`, `observed_score`, `release_validation`, `maturity_transition`, `maturity_checkpoint_record_id`, `downgrade_incident_id`, and `regression_warning`.
- [ ] Update closure and rehearsal diagnostics to identify which real-business path was accepted and which deficits remain.
- [ ] Keep release-bound safety checks strict without making their version identity part of maturity checkpoint selection.

---

### Task 9: Consolidated local verification

**Files:** no new changes unless a failure identifies a defect in this implementation.

- [ ] Run the focused command from Task 4 once and require all tests to pass.
- [ ] Run the repository's full supported verification command once.
- [ ] Run repository search checks proving:
  - `openclaw-loop-watch` is still installed and identity-checked;
  - `openclaw-stuck-watchdog` is absent from deployment/systemd/runtime identity;
  - the Feishu reply watchdog is not an active resend path;
  - no maturity checkpoint selection key contains version or commit.
- [ ] Inspect the final diff for secrets, raw identifiers, generated artifacts, accidental audit-report restoration, and unrelated files.

---

### Task 10: Commit, push, deploy, and verify production closure

**Files:**
- Delete as already requested:
  - `docs/audits/2026-07-11-1.9.10-1.9.14-l5-audit.md`
  - `docs/audits/2026-07-18-full-closure-1.9.70.md`
  - `docs/audits/2026-07-18-openclaw-2026.7.1-compatibility.md`
  - `docs/audits/production-l5-autonomous-proof.json`

- [ ] Commit the complete implementation and requested audit cleanup on the repair branch.
- [ ] Push and integrate the reviewed commit to GitHub `master`.
- [ ] Deploy the exact integrated commit from `/dev-project/eimemory`; never deploy from `/home/darrow/dev-project-ready/eimemory`.
- [ ] Confirm GitHub master, `/dev-project/eimemory` HEAD, `/opt/eimemory/current`, runtime import root, `/health`, and release identity all agree.
- [ ] Confirm `openclaw-loop-watch.timer` is enabled/active, stale lease count is zero, and loopback proxy health is green.
- [ ] Execute a fresh verified real replay against current code when at least 10 eligible real sources across 5 task types exist.
- [ ] Rebuild readiness/release closure and report separately:
  - deployed and currently healthy;
  - accumulated L5 maturity;
  - accepted real-business path and exact remaining deficits, if any.
