# OpenClaw Python Environment Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate OpenClaw gateway Python import-surface drift so the strict release-bound `memory.recall` replay remains reproducibly valid in the real gateway environment.

**Architecture:** The managed systemd drop-in removes ambient Python import overrides and retains only absolute immutable-release commands. Static deployment tests prevent the hard-coded interpreter path from returning, while production verification checks the effective process environment before rebuilding release-bound replay evidence.

**Tech Stack:** systemd user units, Bash immutable-release installer, Python 3.11+, pytest, eimemory governance replay and L5 readiness CLI.

## Global Constraints

- Keep `recall_version_truth` and replay evidence comparison fail-closed.
- Do not hard-code a Python minor-version site-packages path.
- Do not run another full repository test suite; run the affected deployment and replay layers.
- Advance to 1.9.77 only after the targeted regression tests pass.
- Production acceptance must execute with the effective OpenClaw gateway environment.

---

### Task 1: Guard the managed gateway environment

**Files:**
- Modify: `tests/test_deployment_tools.py`
- Modify: `deploy/systemd/openclaw-gateway-eimemory.conf`

**Interfaces:**
- Consumes: systemd `UnsetEnvironment=` service directive.
- Produces: a gateway environment that cannot inherit `PYTHONPATH`, `PYTHONHOME`, or `VIRTUAL_ENV`.

- [ ] **Step 1: Write the failing regression test**

Add to `test_openclaw_gateway_override_uses_production_eimemory_runtime`:

```python
assert "UnsetEnvironment=PYTHONPATH PYTHONHOME VIRTUAL_ENV" in override_text
assert "Environment=PYTHONPATH=" not in override_text
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```powershell
python -m pytest tests/test_deployment_tools.py::test_openclaw_gateway_override_uses_production_eimemory_runtime -q
```

Expected: FAIL because the drop-in still contains the Python 3.13 `PYTHONPATH` and has no `UnsetEnvironment` directive.

- [ ] **Step 3: Implement the minimal drop-in change**

Replace the hard-coded `Environment=PYTHONPATH=...python3.13...` line with:

```ini
# The absolute venv commands below own their Python import surface. Prevent the
# Node gateway and its children from inheriting stale operator Python settings.
UnsetEnvironment=PYTHONPATH PYTHONHOME VIRTUAL_ENV
```

- [ ] **Step 4: Run the regression and deployment layer**

Run:

```powershell
python -m pytest tests/test_deployment_tools.py -q
```

Expected: all deployment-tool tests pass.

### Task 2: Prepare patch release identity

**Files:**
- Modify: `pyproject.toml`
- Modify: `eimemory/version.py`

**Interfaces:**
- Consumes: passing Task 1 tests.
- Produces: matching package and runtime version `1.9.77`.

- [ ] **Step 1: Update both version declarations**

Set:

```toml
version = "1.9.77"
```

and:

```python
__version__ = "1.9.77"
```

- [ ] **Step 2: Run version and affected governance tests**

Run:

```powershell
python -m pytest tests/test_version.py tests/test_capability_acceptance.py tests/test_capability_replay_packs.py tests/test_l5_readiness.py -q
```

Expected: all selected tests pass.

- [ ] **Step 3: Review the exact release diff**

Run:

```powershell
git diff --check
git diff -- deploy/systemd/openclaw-gateway-eimemory.conf tests/test_deployment_tools.py pyproject.toml eimemory/version.py docs/superpowers
```

Expected: no whitespace errors and no unrelated changes.

### Task 3: Commit, push, and deploy immutable release

**Files:**
- Deployment source: `/dev-project/eimemory`
- Immutable target: `/opt/eimemory/releases/<commit>`
- Current link: `/opt/eimemory/current`

**Interfaces:**
- Consumes: committed and pushed `master` at version 1.9.77.
- Produces: deployment receipt and live services bound to the same commit.

- [ ] **Step 1: Commit and push**

Run:

```powershell
git add deploy/systemd/openclaw-gateway-eimemory.conf tests/test_deployment_tools.py pyproject.toml eimemory/version.py docs/superpowers/specs/2026-07-20-openclaw-python-environment-isolation-design.md docs/superpowers/plans/2026-07-20-openclaw-python-environment-isolation.md
git commit -m "fix: isolate OpenClaw Python runtime"
git push origin master
```

Expected: `origin/master` resolves to the new commit.

- [ ] **Step 2: Sync the authoritative honxin repository and deploy**

Use the established immutable-release installer from `/dev-project/eimemory` for the pushed commit and version 1.9.77.

Expected: `/opt/eimemory/current`, the RPC health endpoint, and the deployment receipt all identify the new commit and version.

- [ ] **Step 3: Verify the effective gateway process environment**

Read `/proc/<openclaw-gateway-main-pid>/environ` and assert these keys are absent:

```text
PYTHONPATH
PYTHONHOME
VIRTUAL_ENV
```

Expected: all three are absent after the gateway restart.

### Task 4: Rebuild and prove L5 closure in the real gateway context

**Files:**
- Production store: `/var/lib/eimemory/state/eimemory.sqlite`
- Production release: `/opt/eimemory/current`

**Interfaces:**
- Consumes: clean effective gateway environment and current deployment receipt.
- Produces: release-bound replay manifest, L5 readiness evidence, and completion notification.

- [ ] **Step 1: Regenerate release-bound core and weak replay evidence**

Run the existing production replay bootstrap/closure workflow for release 1.9.77 and retain its manifest identifiers.

Expected: five core capabilities and four weak capabilities each have three distinct passing replay cases.

- [ ] **Step 2: Execute readiness with the gateway's effective environment**

Run the release CLI from the OpenClaw workspace with the gateway process environment and production root/config.

Expected:

```text
current_stage=L5
readiness_score=1.0
memory.recall=3/3
core replay=15/15
weak replay=12/12
```

- [ ] **Step 3: Verify release closure and service health**

Confirm Git master, `/opt/eimemory/current`, `/health`, deployment receipt, replay manifest, supervisor, and L5 readiness all bind to the same release.

Expected: no missing evidence, no replay rejection reasons, and closure complete.

- [ ] **Step 4: Send Feishu completion notification**

Send a concise notification containing version, commit prefix, deployment health, L5 score, core/weak replay counts, and the root-cause/fix summary.

Expected: the Feishu send operation returns a platform receipt.
