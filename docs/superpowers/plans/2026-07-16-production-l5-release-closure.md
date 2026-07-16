# Production L5 Release Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make L5 closure reproducible for the currently deployed immutable release without weakening activity, replay-integrity, or commit-binding gates.

**Architecture:** A shared classifier derives explicit autonomous-learning activity from the full cycle before the scheduler creates its reusable summary. A new release-closure orchestrator composes the existing deployment receipt, live acceptance, closure rehearsal, and readiness APIs and stops at the first failed gate.

**Tech Stack:** Python 3.12+, pytest, SQLite record store, argparse CLI, Git, systemd user services, immutable release installer.

## Global Constraints

- Missing or unknown activity status is active, never idle.
- Only explicit successful no-change cycles may preserve prior global L5 readiness.
- Historical replay manifests are immutable; digest mismatch remains a hard failure.
- Live acceptance and deployment evidence never transfer across commits.
- Autonomous code application remains disabled while producing closure evidence.
- The release version is exactly `1.9.50`.
- Final code must be fast-forwarded into `master`, pushed, deployed from `/dev-project/eimemory`, and verified against `http://127.0.0.1:8091/health`.
- Run the full suite once before release; use focused and adjacent suites during implementation.

---

### Task 1: Produce and preserve explicit autonomous activity

**Files:**
- Modify: `eimemory/governance/autonomous_learning.py`
- Modify: `eimemory/scheduler/jobs.py`
- Modify: `tests/test_full_autonomous_learning_loop.py`
- Modify: `tests/test_autonomous_learning_integration.py`

**Interfaces:**
- Produces: `classify_autonomous_learning_activity(report: dict[str, Any], *, timeout_exceeded: bool = False, error_reason: str = "") -> dict[str, Any]`.
- Produces report fields: `activity_status`, `activity_reason`, and `attempted_candidate_count`.
- Consumes existing report fields: `ok`, `candidate_specs`, `eval_record_ids`, `candidate_ids`, `promotions`, `eval_verdict`, `replay_gate_passed`, `safety_gate_passed`, and `isolation_gate_passed`.

- [ ] **Step 1: Write failing classifier tests**

Add tests that import `classify_autonomous_learning_activity` and assert these exact results:

```python
def test_activity_classifier_marks_successful_no_change_idle() -> None:
    result = classify_autonomous_learning_activity(
        {
            "ok": True,
            "candidate_specs": [],
            "eval_record_ids": [],
            "candidate_ids": [],
            "promotions": [],
            "replay_gate_passed": True,
            "safety_gate_passed": True,
            "isolation_gate_passed": True,
        }
    )
    assert result == {
        "activity_status": "idle",
        "activity_reason": "no_candidate_change",
        "attempted_candidate_count": 0,
    }


def test_activity_classifier_keeps_failed_evaluation_active() -> None:
    result = classify_autonomous_learning_activity(
        {
            "ok": True,
            "candidate_specs": [{"promotion_target": "knowledge_patch"}],
            "eval_record_ids": ["eval-1"],
            "candidate_ids": [],
            "promotions": [],
            "eval_verdict": "fail",
            "replay_gate_passed": True,
            "safety_gate_passed": True,
            "isolation_gate_passed": True,
        }
    )
    assert result["activity_status"] == "active"
    assert result["activity_reason"] == "candidate_evaluation_attempted"
    assert result["attempted_candidate_count"] == 1


def test_activity_classifier_marks_timeout_failed() -> None:
    result = classify_autonomous_learning_activity(
        {"ok": True, "candidate_specs": []}, timeout_exceeded=True
    )
    assert result["activity_status"] == "failed"
    assert result["activity_reason"] == "timeout_exceeded"
```

- [ ] **Step 2: Run the classifier tests and verify RED**

Run:

```powershell
python -m pytest -q tests/test_full_autonomous_learning_loop.py -k "activity_classifier"
```

Expected: collection or import failure because `classify_autonomous_learning_activity` does not exist.

- [ ] **Step 3: Implement the minimal shared classifier**

Add a function that:

```python
def classify_autonomous_learning_activity(
    report: dict[str, Any],
    *,
    timeout_exceeded: bool = False,
    error_reason: str = "",
) -> dict[str, Any]:
    candidate_specs = [item for item in report.get("candidate_specs") or [] if isinstance(item, dict)]
    eval_record_ids = [str(item) for item in report.get("eval_record_ids") or [] if str(item or "").strip()]
    candidate_ids = [str(item) for item in report.get("candidate_ids") or [] if str(item or "").strip()]
    promotions = [item for item in report.get("promotions") or [] if isinstance(item, dict)]
    attempted_count = max(len(candidate_specs), len(eval_record_ids))
    if timeout_exceeded:
        status, reason = "failed", "timeout_exceeded"
    elif report.get("ok") is not True:
        status, reason = "failed", str(error_reason or report.get("error") or "cycle_failed")
    elif attempted_count or candidate_ids or promotions:
        status, reason = "active", "candidate_evaluation_attempted"
    elif str(report.get("eval_verdict") or "").strip().lower() in {"fail", "failed", "blocked", "reject", "rejected"}:
        status, reason = "active", "evaluation_gate_failed"
    elif any(key in report and report.get(key) is False for key in ("replay_gate_passed", "safety_gate_passed", "isolation_gate_passed")):
        status, reason = "active", "evidence_gate_failed"
    else:
        status, reason = "idle", "no_candidate_change"
    return {
        "activity_status": status,
        "activity_reason": reason,
        "attempted_candidate_count": attempted_count,
    }
```

Build the autonomous-cycle response into a local `result` dictionary, update it with the classifier output, and then return it.

- [ ] **Step 4: Run the classifier tests and verify GREEN**

Run the command from Step 2. Expected: all selected tests pass.

- [ ] **Step 5: Write failing scheduler contract tests**

Extend the nightly integration tests so a fake active failure retains `activity_status="active"`, evaluation identifiers, replay reason, safety/isolation results, and replay manifest identity; add timeout and exception assertions for `activity_status="failed"`.

```python
assert summary["activity_status"] == "active"
assert summary["activity_reason"] == "candidate_evaluation_attempted"
assert summary["attempted_candidate_count"] == 1
assert summary["eval_record_ids"] == ["eval-1"]
assert summary["replay_gate_reason"] == "passed"
assert summary["safety_gate_passed"] is True
assert summary["isolation_gate_passed"] is False
assert summary["capability_replay_manifest_id"] == "manifest-1"
```

- [ ] **Step 6: Run the scheduler tests and verify RED**

Run:

```powershell
python -m pytest -q tests/test_autonomous_learning_integration.py -k "activity or timeout or reuses_same"
```

Expected: assertions fail because the scheduler summary drops these fields.

- [ ] **Step 7: Preserve the activity contract in `_run_autonomous_learning`**

Classify the full report before building `status`. Copy the classifier result and these normalized fields into the summary:

```python
"eval_record_ids": [str(item) for item in report.get("eval_record_ids") or [] if str(item or "").strip()],
"replay_gate_reason": str((report.get("replay_gate") or {}).get("reason") or ""),
"safety_gate_passed": report.get("safety_gate_passed") is True,
"isolation_gate_passed": report.get("isolation_gate_passed") is True,
"isolation_blocked_reasons": list((report.get("isolated_evaluator") or {}).get("blocked_reasons") or []),
"capability_replay_execution_id": str((report.get("capability_replay") or {}).get("execution_id") or ""),
"capability_replay_manifest_id": str((report.get("capability_replay") or {}).get("manifest_record_id") or ""),
```

Timeout and exception responses must set `activity_status="failed"` and a stable reason. Disabled/skipped reports may use `activity_status="idle"`, but they remain non-reusable because `learning_skipped_reason` is populated.

- [ ] **Step 8: Run focused Task 1 suites**

Run:

```powershell
python -m pytest -q tests/test_full_autonomous_learning_loop.py tests/test_autonomous_learning_integration.py tests/test_l5_consciousness_loop.py
```

Expected: all tests pass.

- [ ] **Step 9: Commit Task 1**

```powershell
git add eimemory/governance/autonomous_learning.py eimemory/scheduler/jobs.py tests/test_full_autonomous_learning_loop.py tests/test_autonomous_learning_integration.py tests/test_l5_consciousness_loop.py
git commit -m "fix(l5): preserve autonomous activity evidence"
```

---

### Task 2: Add a fail-closed release-closure orchestrator

**Files:**
- Create: `eimemory/governance/release_closure.py`
- Create: `tests/test_release_closure.py`

**Interfaces:**
- Produces: `run_release_closure(runtime, *, scope, repo_root, current_link, health_url, prior_commit) -> dict[str, Any]`.
- Calls existing runtime APIs in exact order: `verify_and_record_deployment`, `run_live_task_acceptance`, `run_l5_closure_rehearsal`, `build_l5_readiness_report`.

- [ ] **Step 1: Write orchestration tests with a deterministic fake runtime**

Create a fake runtime that records method calls and returns injected stage responses. Test:

```python
def test_release_closure_runs_all_stages_in_order() -> None:
    runtime = FakeRuntime.successful()
    report = run_release_closure(
        runtime,
        scope=SCOPE,
        repo_root="/dev-project/eimemory",
        current_link="/opt/eimemory/current",
        health_url="http://127.0.0.1:8091/health",
        prior_commit="a" * 40,
    )
    assert runtime.calls == ["deployment_receipt", "live_acceptance", "closure_rehearsal", "readiness"]
    assert report["ok"] is True
    assert report["closure_complete"] is True
    assert report["blocked_stage"] == ""
```

Parameterize failures at every stage and assert later calls are absent. Add a readiness failure where `current_stage="L5"` but `readiness_score=0.9`, proving the score must be exactly `1.0`.

- [ ] **Step 2: Run the new tests and verify RED**

```powershell
python -m pytest -q tests/test_release_closure.py
```

Expected: import failure because `eimemory.governance.release_closure` does not exist.

- [ ] **Step 3: Implement the orchestrator**

Use one initial report with `not_run` placeholders. After each call, store the result and stop through a helper:

```python
def _blocked(report: dict[str, Any], stage: str, reason: str) -> dict[str, Any]:
    report["ok"] = False
    report["closure_complete"] = False
    report["blocked_stage"] = stage
    report["blocked_reason"] = reason
    return report
```

Success requires all of:

```python
ready = (
    readiness.get("ok") is True
    and readiness.get("current_stage") == "L5"
    and isinstance(readiness.get("readiness_score"), (int, float))
    and not isinstance(readiness.get("readiness_score"), bool)
    and float(readiness["readiness_score"]) == 1.0
    and (readiness.get("latest_l5_assessment") or {}).get("complete") is True
    and (readiness.get("live_task_gate") or {}).get("ok") is True
    and int((readiness.get("live_task_gate") or {}).get("current_deployment_acceptance") or 0) >= 10
    and not list((readiness.get("verified_replay") or {}).get("weak_capabilities_missing") or [])
)
```

Return deployment identity from the receipt and persisted record identifiers from the stage responses for audit.

- [ ] **Step 4: Run Task 2 tests and verify GREEN**

Run the command from Step 2. Expected: all tests pass.

- [ ] **Step 5: Commit Task 2**

```powershell
git add eimemory/governance/release_closure.py tests/test_release_closure.py
git commit -m "feat(l5): orchestrate release closure evidence"
```

---

### Task 3: Expose release closure through Runtime and CLI

**Files:**
- Modify: `eimemory/api/runtime.py`
- Modify: `eimemory/cli/main.py`
- Modify: `tests/test_release_closure.py`

**Interfaces:**
- Produces: `Runtime.run_release_closure(...) -> dict`.
- Produces CLI: `eimemory learn release-closure --repo-root ... --current-link ... --health-url ... --prior-commit ... --scope-agent ... --scope-workspace ... --scope-user ... --json`.

- [ ] **Step 1: Write failing Runtime and CLI tests**

Test that Runtime forwards every argument. Test CLI parser/dispatch with a monkeypatched runtime and assert exit code `0` for `ok=True`, `1` for a blocked report, and JSON includes `blocked_stage`.

- [ ] **Step 2: Run Task 3 tests and verify RED**

```powershell
python -m pytest -q tests/test_release_closure.py -k "runtime or cli"
```

Expected: missing method/parser failures.

- [ ] **Step 3: Add Runtime and CLI adapters**

Add the runtime method next to the existing L5 methods:

```python
def run_release_closure(
    self,
    *,
    scope: dict | None = None,
    repo_root: str,
    current_link: str,
    health_url: str,
    prior_commit: str,
) -> dict:
    from eimemory.governance.release_closure import run_release_closure
    return run_release_closure(
        self,
        scope=scope,
        repo_root=repo_root,
        current_link=current_link,
        health_url=health_url,
        prior_commit=prior_commit,
    )
```

Add parser arguments matching `live-acceptance`, dispatch through `_cli_scope`, print JSON, and return `0 if report.get("ok") else 1`.

- [ ] **Step 4: Run release-closure and adjacent CLI tests**

```powershell
python -m pytest -q tests/test_release_closure.py tests/test_live_task_acceptance.py tests/test_l5_closure_rehearsal.py tests/test_l5_readiness.py
```

Expected: all tests pass.

- [ ] **Step 5: Commit Task 3**

```powershell
git add eimemory/api/runtime.py eimemory/cli/main.py tests/test_release_closure.py
git commit -m "feat(cli): add production L5 release closure"
```

---

### Task 4: Release version 1.9.50 and verify the repository

**Files:**
- Modify: `pyproject.toml`
- Modify: `eimemory/version.py`
- Modify: all eleven version-pinned files under `deploy/systemd/`
- Modify: version/deployment contract tests as required by existing patterns
- Modify: `docs/superpowers/specs/2026-07-16-production-l5-release-closure-design.md`
- Create: `docs/superpowers/plans/2026-07-16-production-l5-release-closure.md`

**Interfaces:**
- Produces package and service-cache version `1.9.50` consistently.

- [ ] **Step 1: Update the version contract test first**

Change the expected version from `1.9.49` to `1.9.50`, then run the relevant deployment contract test and observe failure.

- [ ] **Step 2: Update all version sources mechanically**

Replace only exact `1.9.49` version literals in `pyproject.toml`, `eimemory/version.py`, and `deploy/systemd/*.service`. Verify no stale service literal remains:

```powershell
rg -n "1\.9\.49|1\.9\.50" pyproject.toml eimemory/version.py deploy/systemd
```

- [ ] **Step 3: Run layered verification**

Focused:

```powershell
python -m pytest -q tests/test_full_autonomous_learning_loop.py tests/test_autonomous_learning_integration.py tests/test_l5_consciousness_loop.py tests/test_release_closure.py
```

Adjacent:

```powershell
python -m pytest -q tests/test_l5_readiness.py tests/test_l5_closure_rehearsal.py tests/test_live_task_acceptance.py tests/test_capability_replay_packs.py tests/test_deploy_loop_integration.py tests/test_deployment_tools.py
```

Static:

```powershell
python -m compileall -q eimemory
git diff --check
```

Full suite, once:

```powershell
python -m pytest -q
```

Expected: zero failures in every command.

- [ ] **Step 4: Commit the release changes**

```powershell
git add pyproject.toml eimemory/version.py deploy/systemd tests docs/superpowers
git commit -m "chore: release 1.9.50"
```

---

### Task 5: Merge, deploy, and prove production L5 closure

**Files:**
- No new source files; operates on Git and the honxin deployment.

**Interfaces:**
- Consumes the verified branch HEAD and prior `origin/master` commit.
- Produces a pushed mainline commit, immutable release, deployment receipt, live acceptance, closure rehearsal, and independent L5 readiness evidence.

- [ ] **Step 1: Re-run the completion gate immediately before integration**

```powershell
python -m pytest -q
python -m compileall -q eimemory
git diff --check
git status --short --branch
```

Expected: zero test failures, clean diff check, and no uncommitted files.

- [ ] **Step 2: Push the feature branch**

```powershell
git push -u origin fix/l5-closure-1.9.50
```

- [ ] **Step 3: Fast-forward authoritative mainline on honxin**

In `/dev-project/eimemory`, fetch, verify clean status, fast-forward `master` to `origin/fix/l5-closure-1.9.50`, and push `master`. Record the prior mainline commit before the merge for rollback evidence.

- [ ] **Step 4: Install the immutable release and restart services**

Run:

```bash
bash deploy/install_immutable_release.sh "$COMMIT"
systemctl --user restart eimemory-rpc.service openclaw-gateway.service
```

Verify `eimemory-rpc.service`, `openclaw-gateway.service`, and `openclaw-loopback-proxy.service` when installed.

- [ ] **Step 5: Verify health identity**

Require `/health` to report `ok=true`, version `1.9.50`, commit equal to mainline HEAD, current link equal to `/opt/eimemory/current`, and release/import roots under `/opt/eimemory/releases/$COMMIT`.

- [ ] **Step 6: Run production release closure**

```bash
EIMEMORY_ROOT=/var/lib/eimemory /opt/eimemory/current/.venv/bin/eimemory learn release-closure \
  --repo-root /dev-project/eimemory \
  --current-link /opt/eimemory/current \
  --health-url http://127.0.0.1:8091/health \
  --prior-commit "$PRIOR_COMMIT" \
  --json
```

Require `ok=true`, `closure_complete=true`, ten successful live cases, fresh verified weak replay, and final readiness `L5/1.0`.

- [ ] **Step 7: Run independent read-only verification in a new process**

```bash
EIMEMORY_ROOT=/var/lib/eimemory /opt/eimemory/current/.venv/bin/eimemory learn l5-readiness --limit 1000 --json
```

Require `current_stage=L5`, `readiness_score=1.0`, complete latest assessment, current deployment acceptance at least ten, no weak replay capability missing, and health/repository/release commit identity agreement.

- [ ] **Step 8: Report exact evidence**

Report branch and mainline commit, version, release path, health identity, test counts, deployment receipt ID, live acceptance pass count/reuse count, closure result, independent readiness result, and any remaining data-accumulation-only observation gates.
