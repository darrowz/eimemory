# L5 Capability Lineage And Watchdog Self-Heal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make watchdog findings self-heal and preserve verified L5 capability evidence across compatible autonomous patch releases while keeping the current deployment identity exact.

**Architecture:** Add a bounded repair transaction to the OpenClaw loop watchdog. Add a verified release-lineage record that separates exact deployment identity from per-domain capability continuity, then make readiness select current or inherited evidence by domain. Harden deployment and health checks so stale systemd runtime metadata cannot produce a valid receipt.

**Tech Stack:** Python 3.11+ standard library, SQLite, Bash/systemd, Git immutable releases, pytest.

## Global Constraints

- The current release still requires an exact current deployment receipt.
- Historical evidence remains immutable.
- Lineage is exact-scope and cannot cross channel, tenant, agent, workspace, user, or source boundaries.
- Unknown production file changes invalidate all capability domains.
- LLM availability cannot affect identity, lineage, watchdog, or readiness.
- The release version is `1.9.89`.
- Production deployment source is `/dev-project/eimemory`; runtime is `/opt/eimemory/current`.

---

### Task 1: Watchdog Repair Transaction

**Files:**
- Modify: `eimemory/ops/openclaw_loop.py`
- Modify: `tests/test_openclaw_loop_io.py`
- Modify: `tests/test_openclaw_watchdog.py`

**Interfaces:**
- Consumes: `find_stale_tasks()`, `create_task()`, `record_action()`, `record_verification()`, and `finish_task()`.
- Produces: `run_watch(..., auto_reconcile=True, reconcile_grace_seconds=900)` and bounded repair fields in the watch result.

- [ ] **Step 1: Write failing repair tests**

```python
def test_watch_creates_repair_task_reconciles_old_stale_work_and_rechecks(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCLAW_LOOP_HOME", str(tmp_path))
    stale = create_task(title="old", objective="old", source="test")
    update_task(stale["task_id"], lease_expires_at=1, updated_at="2026-01-01T00:00:00Z")

    result = run_watch(
        run_live_checks=False,
        auto_reconcile=True,
        reconcile_grace_seconds=0,
    )

    assert result["ok"] is True
    assert result["repair"]["created"] is True
    assert result["repair"]["reconciled_count"] == 1
    assert result["repair"]["remaining_stale_count"] == 0
    assert get_task(stale["task_id"])["status"] == "failed"
```

Add separate tests proving a task inside the grace period is not reconciled,
the repair task remains bounded/idempotent, and a failed second check keeps
the watchdog non-zero.

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
python -m pytest -q tests/test_openclaw_loop_io.py tests/test_openclaw_watchdog.py
```

Expected: new assertions fail because `run_watch` has no repair transaction.

- [ ] **Step 3: Implement bounded self-heal**

Add:

```python
WATCH_AUTO_RECONCILE_GRACE_SECONDS = 900

def _stale_age_seconds(item: dict[str, Any], *, now: float) -> float:
    lease = float(item.get("lease_expires_at") or 0)
    if lease:
        return max(0.0, now - lease)
    return max(0.0, now - _updated_epoch(item))

def _run_watch_repair(
    stale: list[dict[str, Any]],
    *,
    grace_seconds: int,
) -> dict[str, Any]:
    """Create an auditable repair task, reconcile eligible work, and recheck."""
```

The repair task uses source `openclaw.loop_watch.repair`, records only counts
and reason classes, reconciles only eligible task IDs, verifies a fresh
`find_stale_tasks()` result, and finishes `done` only when remaining count is
zero. `run_watch` persists the final post-repair state.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the Step 2 command. Expected: all focused tests pass.

- [ ] **Step 5: Commit**

```powershell
git add eimemory/ops/openclaw_loop.py tests/test_openclaw_loop_io.py tests/test_openclaw_watchdog.py
git commit -m "fix: close watchdog stale-task repair loop"
```

### Task 2: Exact Runtime Identity Enforcement

**Files:**
- Modify: `eimemory/adapters/eibrain/rpc_server.py`
- Modify: `deploy/verify_release_health.py`
- Modify: `deploy/install_immutable_release.sh`
- Modify: `tests/test_runtime_adapter_rpc.py`
- Modify: `tests/test_deployment_tools.py`

**Interfaces:**
- Consumes: package import-root commit and `EIMEMORY_RUNTIME_COMMIT`.
- Produces: `/health.checks.runtime_identity`, `configured_runtime_commit`, and deploy-time effective-environment verification.

- [ ] **Step 1: Write failing identity tests**

```python
def test_health_fails_closed_when_configured_runtime_commit_differs_from_release(monkeypatch):
    monkeypatch.setenv("EIMEMORY_RUNTIME_COMMIT", "b" * 40)
    monkeypatch.setattr(rpc_server, "_current_commit", lambda: "a" * 40)
    payload = rpc_server._compact_health_payload(runtime, ready=True, listen_host="127.0.0.1", listen_port=8091)
    assert payload["ok"] is False
    assert payload["checks"]["runtime_identity"] is False
```

Add deployment tests requiring the installer to inspect effective
`EIMEMORY_RUNTIME_COMMIT` for both RPC and gateway before recording the
receipt, and requiring `verify_release_health.py` to reject a false runtime
identity check.

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
python -m pytest -q tests/test_runtime_adapter_rpc.py tests/test_deployment_tools.py
```

Expected: health remains true and the installer lacks the effective metadata
gate.

- [ ] **Step 3: Implement health and installer gates**

Health returns:

```python
"configured_runtime_commit": configured_commit,
"checks": {
    "process": True,
    "store": store_ready,
    "runtime_identity": runtime_identity_ok,
    "ready": bool(ready and store_ready and runtime_identity_ok),
},
```

The installer adds `_verify_effective_runtime_metadata "$COMMIT"` after
`daemon-reload` and service restart. It reads only the named environment
field from `systemctl --user show`, requires both services to match, then
allows health verification and receipt recording.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the Step 2 command. Expected: all tests pass.

- [ ] **Step 5: Commit**

```powershell
git add eimemory/adapters/eibrain/rpc_server.py deploy/verify_release_health.py deploy/install_immutable_release.sh tests/test_runtime_adapter_rpc.py tests/test_deployment_tools.py
git commit -m "fix: enforce exact deployed runtime identity"
```

### Task 3: Release Capability Lineage Records

**Files:**
- Create: `eimemory/governance/release_lineage.py`
- Create: `deploy/record_release_lineage.py`
- Create: `tests/test_release_lineage.py`
- Modify: `eimemory/api/runtime.py`
- Modify: `deploy/install_immutable_release.sh`
- Modify: `tests/test_deployment_tools.py`

**Interfaces:**
- Produces:

```python
def record_release_lineage(
    runtime: Any,
    *,
    scope: ScopeRef | dict | None,
    repo_root: str | Path,
    current_release: ReleaseIdentity,
    gate_evidence: dict[str, list[str]] | None = None,
) -> dict[str, Any]: ...

def current_release_lineage(
    runtime: Any,
    *,
    scope: ScopeRef | dict | None,
    current_release: ReleaseIdentity,
    repo_root: str | Path = "/dev-project/eimemory",
) -> dict[str, Any]: ...

def evidence_release_for_domain(
    runtime: Any,
    *,
    scope: ScopeRef | dict | None,
    repo_root: str | Path,
    domain: str,
    current_release: ReleaseIdentity,
    expected_record_id: str = "",
) -> ReleaseIdentity: ...
```

- [ ] **Step 1: Write failing lineage tests**

Create temporary Git repositories and verified receipt fixtures. Test:

```python
def test_unchanged_domain_inherits_verified_ancestor_release(tmp_path):
    lineage = record_release_lineage(...)
    resolved = current_release_lineage(...)
    assert resolved["ok"] is True
    assert resolved["domains"]["memory.recall"]["mode"] == "inherited"
    assert evidence_release_for_domain(
        runtime,
        scope=scope,
        repo_root=repo,
        domain="memory.recall",
        current_release=current,
        expected_record_id=resolved["record_id"],
    ) == prior
```

Also prove changed domains require current gate evidence, unknown production
paths change all domains, broken ancestry is rejected, forged receipts are
rejected, and scope mismatch cannot inherit.

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
python -m pytest -q tests/test_release_lineage.py
```

Expected: import failure because the lineage module does not exist.

- [ ] **Step 3: Implement domain digests and attestation validation**

Define explicit domain path rules and use `git ls-tree -r <commit> -- <paths>`
to hash canonical `(mode, object, path)` rows. Compute changed paths with
`git diff --name-only prior..current`. Persist one current-release record with
both verified receipt identities, ancestry result, domain digests, change
classification, and exact gate references. Validation recomputes every field.

- [ ] **Step 4: Integrate deployment recording**

After the current deployment receipt exists, invoke:

```bash
env EIMEMORY_RUNTIME_COMMIT="$COMMIT" \
  "$RELEASE_DIR/.venv/bin/python" -I -B \
  "$RELEASE_DIR/deploy/record_release_lineage.py" \
  --repo-root "$REPO_DIR" --current-commit "$COMMIT" \
  --scope-agent "$EIMEMORY_DEPLOY_SCOPE_AGENT" \
  --scope-workspace "$EIMEMORY_DEPLOY_SCOPE_WORKSPACE" \
  --scope-user "$EIMEMORY_DEPLOY_SCOPE_USER"
```

The recorder selects the newest verified ancestor receipt, not an unverified
intermediate deployment.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run:

```powershell
python -m pytest -q tests/test_release_lineage.py tests/test_deployment_tools.py
```

- [ ] **Step 6: Commit**

```powershell
git add eimemory/governance/release_lineage.py eimemory/api/runtime.py deploy/record_release_lineage.py deploy/install_immutable_release.sh tests/test_release_lineage.py tests/test_deployment_tools.py
git commit -m "feat: preserve capability evidence across release lineage"
```

### Task 4: Lineage-Aware Readiness Without Weakening Receipts

**Files:**
- Modify: `eimemory/governance/capability_dashboard.py`
- Modify: `eimemory/governance/l5_readiness.py`
- Modify: `eimemory/governance/release_closure.py`
- Modify: `tests/test_capability_dashboard_metrics.py`
- Modify: `tests/test_l5_readiness.py`
- Modify: `tests/test_release_closure.py`
- Modify: `tests/test_production_recall_bootstrap_deploy.py`

**Interfaces:**
- Consumes: `current_release_lineage()` and `evidence_release_for_domain()`.
- Produces: `release_lineage` in readiness, lineage-aware real-task, replay,
  recall-gate, strict-state, and assessment selection.

- [ ] **Step 1: Write failing readiness continuity tests**

Add a prior trusted L5 evidence set, a current exact deployment receipt, and a
valid lineage record. Assert:

```python
report = runtime.build_l5_readiness_report(scope=SCOPE, persist=False)
assert report["release_identity"]["release_commit"] == current.commit
assert report["release_lineage"]["ok"] is True
assert report["live_task_gate"]["evidence_mode"] == "lineage_inherited"
assert report["current_stage"] == "L5"
```

Add negative cases for missing current receipt, changed domain without a
current gate, digest mismatch, and cross-scope evidence.

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
python -m pytest -q tests/test_capability_dashboard_metrics.py tests/test_l5_readiness.py tests/test_release_closure.py tests/test_production_recall_bootstrap_deploy.py
```

- [ ] **Step 3: Make evidence selection domain-aware**

Readiness resolves exact `current_release_identity()` first. It then loads the
current lineage and selects:

- `channel.openclaw` for verified real-task outcomes;
- `memory.governance` for weak/core replay and latest L5 assessment;
- `memory.recall` for production recall gate and strict-state;
- exact current release for deployment and storage gates.

Every inherited result includes `evidence_mode=lineage_inherited`,
`evidence_release_commit`, and `current_release_commit`. Existing validators
still validate the original record against its original verified receipt.

- [ ] **Step 4: Finalize changed domains during release closure**

After replay and live acceptance, record a final lineage attestation carrying
their current-release gate references before closure rehearsal/readiness.
Failed or missing changed-domain gates remain non-inheritable.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run the Step 2 command. Expected: all tests pass.

- [ ] **Step 6: Commit**

```powershell
git add eimemory/governance/capability_dashboard.py eimemory/governance/l5_readiness.py eimemory/governance/release_closure.py tests/test_capability_dashboard_metrics.py tests/test_l5_readiness.py tests/test_release_closure.py tests/test_production_recall_bootstrap_deploy.py
git commit -m "fix: retain L5 evidence across compatible releases"
```

### Task 5: Observation Pending Semantics

**Files:**
- Modify: `deploy/systemd/eimemory-l5-observation-gate.sh`
- Modify: `deploy/systemd/eimemory-l5-observation-gate.timer`
- Modify: `deploy/systemd/README.md`
- Modify: `tests/test_deployment_tools.py`

**Interfaces:**
- Produces: a successful `observation_pending` execution for valid below-L5
  reports, and a repeated six-hour recheck until exact L5.

- [ ] **Step 1: Write failing deployment contract tests**

Require `OnUnitActiveSec=6h`, require a valid non-L5 stage to emit
`status=observation_pending` and exit zero, and retain non-zero exits for
malformed JSON, command failures, and failed service checks.

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
python -m pytest -q tests/test_deployment_tools.py
```

- [ ] **Step 3: Implement pending semantics**

Parse `ok`, `current_stage`, and `readiness_score`. If `ok=true` and stage is
not L5, emit the pending status and leave apply/deploy disabled. Only exact L5
executes the enabling mutations and disables the timer.

- [ ] **Step 4: Run tests and verify GREEN**

Run the Step 2 command. Expected: all tests pass.

- [ ] **Step 5: Commit**

```powershell
git add deploy/systemd/eimemory-l5-observation-gate.sh deploy/systemd/eimemory-l5-observation-gate.timer deploy/systemd/README.md tests/test_deployment_tools.py
git commit -m "fix: keep L5 observation pending without failed units"
```

### Task 6: Release, Review, Deploy, And Production Closure

**Files:**
- Modify: `pyproject.toml`
- Modify: `eimemory/version.py`
- Modify: `CHANGELOG.md` if present
- Modify: `tests/test_version.py`

**Interfaces:**
- Produces: `v1.9.89` on `master`, immutable production deployment, and a
  Feishu completion notification.

- [ ] **Step 1: Bump version and run focused version tests**

Set both declared versions to `1.9.89`, add the release notes, and run:

```powershell
python -m pytest -q tests/test_version.py
```

- [ ] **Step 2: Run layered verification**

```powershell
python -m pytest -q tests/test_openclaw_loop_io.py tests/test_openclaw_watchdog.py tests/test_runtime_adapter_rpc.py tests/test_deployment_tools.py tests/test_release_lineage.py tests/test_governance_evidence_contract.py tests/test_capability_dashboard_metrics.py tests/test_l5_readiness.py tests/test_release_closure.py tests/test_production_recall_bootstrap_deploy.py
python -m compileall -q eimemory deploy
git diff --check
```

Run one repository-wide suite only after the focused layers pass.

- [ ] **Step 3: Request code review and repair all critical/important findings**

Review the diff against the design and plan. Re-run every affected test after
review fixes.

- [ ] **Step 4: Commit release and merge**

```powershell
git add pyproject.toml eimemory/version.py CHANGELOG.md tests/test_version.py
git commit -m "chore: release v1.9.89"
git tag v1.9.89
git -C E:\eimemory merge --ff-only fix/l5-lineage-watchdog
git -C E:\eimemory push origin master
git -C E:\eimemory push origin v1.9.89
```

- [ ] **Step 5: Deploy from the authoritative honxin checkout**

Fast-forward `/dev-project/eimemory`, run
`deploy/install_immutable_release.sh <full-commit>`, and do not disable storage
migrations, runtime metadata, receipt recording, or lineage recording.

- [ ] **Step 6: Verify production closure**

Require:

- repository, systemd environment, current link, health, receipt, and lineage
  identify the same commit/version;
- migration pending count is zero;
- stale tasks are repaired through the watchdog and the latest watch succeeds;
- RPC, gateway, loopback proxy, timers, and user failed units are healthy;
- replay, live acceptance, release closure, and independent readiness are
  bound to current release or explicitly inherited through a valid domain
  lineage;
- no cross-channel/source leakage.

- [ ] **Step 7: Send Feishu notification**

Send a concise Chinese result through the production Feishu API. Treat only an
accepted platform response as notification success; do not expose credentials
or full message/user identifiers.
