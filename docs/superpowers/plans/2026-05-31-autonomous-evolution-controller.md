# Autonomous Evolution Controller Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a B-level autonomous evolution controller that mines local experience and optional web evidence, synthesizes replay cases, evaluates low-risk safe patches, applies passing memory-policy improvements, and reports every decision.

**Architecture:** Implement a new governance module for controller orchestration and a separate web scout module for external evidence hypotheses. Integrate through `Runtime`, CLI, nightly jobs, and active recall policy so applied rules can affect recall profile and source weights without code edits or deployment changes.

**Tech Stack:** Python 3.11+, existing `Runtime`, `RecordEnvelope`, SQLite-backed event/outcome tables, production recall evaluator, pytest.

---

## File Structure

- Create `eimemory/governance/autonomous_evolution.py`: opportunity mining, replay synthesis, experiment gate, safe patch application, persisted report record.
- Create `eimemory/governance/web_learning.py`: configured URL/evidence scout that emits hypotheses only.
- Modify `eimemory/api/runtime.py`: add `run_autonomous_evolution(...)`.
- Modify `eimemory/api/memory.py`: merge active-policy `source_weights` into recall filters.
- Modify `eimemory/scheduler/jobs.py`: run controller in nightly and include summary.
- Modify `eimemory/cli/main.py`: add `eimemory evolve autonomous`.
- Test `tests/test_autonomous_evolution_controller.py`: local event/outcome opportunities and safe patch application.
- Test `tests/test_autonomous_safe_patches.py`: source weights/recall profile behavior and blocked high-risk patches.
- Test `tests/test_web_learning_scout.py`: web hypotheses are evidence only.
- Test `tests/test_autonomous_evolution_platform.py`: Runtime, CLI, nightly integration.

---

### Task 1: Local Opportunity Mining And Replay Synthesis

**Files:**
- Create: `eimemory/governance/autonomous_evolution.py`
- Test: `tests/test_autonomous_evolution_controller.py`

- [ ] **Step 1: Write failing tests**

Add tests that create event/outcome pairs and assert the controller finds an opportunity and replay case.

```python
from eimemory.api.runtime import Runtime
from eimemory.governance.autonomous_evolution import run_autonomous_evolution


def test_autonomous_evolution_mines_bad_outcome_into_opportunity_and_replay(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    event = runtime.record_event(
        {
            "id": "evt_repair_bad",
            "timestamp": "2026-05-31T01:00:00+08:00",
            "source": "manual",
            "user_phrase": "OpenClaw 又没反应",
            "event_type": "repair",
            "interpreted_intent": "恢复 OpenClaw",
            "goal": "服务恢复并验证",
            "verification": "",
            "confidence": 0.84,
        },
        scope=scope,
    )
    runtime.record_outcome(
        event["id"],
        {
            "outcome": "bad",
            "reason": "只做临时重启，没有诊断日志",
            "correction_from_user": "先看日志和状态，别只重启",
            "policy_update": "repair 请求先诊断日志、状态和最近变更，再低风险修复并验证",
        },
        scope=scope,
    )

    report = run_autonomous_evolution(runtime, scope=scope, apply=False)

    assert report["ok"] is True
    assert report["opportunity_count"] == 1
    assert report["opportunities"][0]["opportunity_type"] == "intent_policy"
    assert report["replay_cases"][0]["query"] == "OpenClaw 又没反应"
    assert "先诊断日志" in " ".join(report["replay_cases"][0]["expected_text"])
```

- [ ] **Step 2: Run test to verify RED**

Run:

```bash
python -m pytest tests/test_autonomous_evolution_controller.py::test_autonomous_evolution_mines_bad_outcome_into_opportunity_and_replay -q
```

Expected: FAIL because `eimemory.governance.autonomous_evolution` does not exist.

- [ ] **Step 3: Implement minimal module**

Create `eimemory/governance/autonomous_evolution.py` with:

```python
from __future__ import annotations

import hashlib
from dataclasses import asdict
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.models.records import RecordEnvelope, ScopeRef


DEFAULT_MIN_PASS_RATE = 0.8


def run_autonomous_evolution(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    apply: bool = False,
    web_hypotheses: list[dict[str, Any]] | None = None,
    max_apply: int = 3,
) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    opportunities = _mine_event_opportunities(runtime, scope=scope_ref)
    opportunities.extend(_web_opportunities(web_hypotheses or [], scope=scope_ref))
    replay_cases = [_replay_case_from_opportunity(item, scope=scope_ref) for item in opportunities]
    return {
        "ok": True,
        "report_type": "autonomous_evolution",
        "schema_version": "autonomous_evolution.v1",
        "generated_at": now_iso(),
        "scope": asdict(scope_ref),
        "apply": bool(apply),
        "opportunity_count": len(opportunities),
        "opportunities": opportunities,
        "replay_cases": replay_cases,
        "safe_patches": [],
        "experiments": [],
        "applied_patches": [],
        "blocked_patches": [],
        "max_apply": max(0, int(max_apply)),
    }
```

Then add helper functions `_mine_event_opportunities`, `_web_opportunities`, `_replay_case_from_opportunity`, `_stable_id`, `_load_recent_event_outcome_pairs`.

- [ ] **Step 4: Run test to verify GREEN**

Run:

```bash
python -m pytest tests/test_autonomous_evolution_controller.py::test_autonomous_evolution_mines_bad_outcome_into_opportunity_and_replay -q
```

Expected: PASS.

---

### Task 2: Safe Patch Generation And Application

**Files:**
- Modify: `eimemory/governance/autonomous_evolution.py`
- Test: `tests/test_autonomous_evolution_controller.py`
- Test: `tests/test_autonomous_safe_patches.py`

- [ ] **Step 1: Write failing tests**

Add tests proving passing local evidence can upsert an intent pattern and high-risk patches are blocked.

```python
def test_autonomous_evolution_applies_low_risk_intent_pattern_after_replay(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    event = runtime.record_event(
        {
            "id": "evt_media_bad",
            "timestamp": "2026-05-31T01:10:00+08:00",
            "source": "manual",
            "user_phrase": "给我唱首歌",
            "event_type": "media_playback",
            "interpreted_intent": "播放音乐给用户听",
            "goal": "用户能听见或打开播放",
            "verification": "用户能听见或打开播放",
            "confidence": 0.91,
        },
        scope=scope,
    )
    runtime.record_outcome(
        event["id"],
        {
            "outcome": "bad",
            "reason": "把播放请求误判成创作歌词",
            "correction_from_user": "其实就是播放一首歌，要考虑怎么让我听见",
            "policy_update": "media_playback 请求先确认歌曲和播放出口，不要默认创作歌词",
        },
        scope=scope,
    )

    report = run_autonomous_evolution(runtime, scope=scope, apply=True)
    policy = runtime.search_policy("给我唱首歌", scope=scope)

    assert report["applied_count"] == 1
    assert policy["policy_suggestions"][0]["event_type"] == "media_playback"
    assert "播放出口" in " ".join(policy["policy_suggestions"][0]["execution_policy"])
```

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
python -m pytest tests/test_autonomous_evolution_controller.py::test_autonomous_evolution_applies_low_risk_intent_pattern_after_replay -q
```

Expected: FAIL because safe patches are not generated/applied.

- [ ] **Step 3: Implement safe patch planner**

In `autonomous_evolution.py`, add:

- `_safe_patch_from_opportunity(opportunity, replay_case, scope)`
- `_evaluate_patch(runtime, patch, replay_case, scope)`
- `_apply_safe_patch(runtime, patch, scope)`

Only `patch_type="intent_pattern"` should be applied in this task.

- [ ] **Step 4: Run tests to verify GREEN**

Run:

```bash
python -m pytest tests/test_autonomous_evolution_controller.py tests/test_autonomous_safe_patches.py -q
```

Expected: PASS.

---

### Task 3: Active Recall Policy Source Weights

**Files:**
- Modify: `eimemory/api/memory.py`
- Modify: `eimemory/governance/autonomous_evolution.py`
- Test: `tests/test_autonomous_safe_patches.py`

- [ ] **Step 1: Write failing tests**

Add a test proving active rule `retrieval_policy.source_weights` affects recall without task_context override.

```python
def test_active_policy_source_weights_affect_recall_ranking(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    runtime.memory.ingest(
        text="UUMit delivery quality means external orders must pass demand-list acceptance.",
        memory_type="fact",
        title="UUMit delivery acceptance",
        scope=scope,
        source="trusted.delivery",
        force_capture=True,
    )
    runtime.memory.ingest(
        text="UUMit generic agent outcome log with unrelated notes.",
        memory_type="conversation",
        title="OpenClaw agent outcome",
        scope=scope,
        source="openclaw.agent_end",
        force_capture=True,
    )
    rule = runtime.evolution.store_rule(
        title="Prefer trusted delivery source",
        summary="Prefer trusted delivery source for delivery task recall",
        task_type="delivery.review",
        retrieval_policy={"source_weights": {"trusted.delivery": 2.5, "openclaw.agent_end": 0.1}},
        response_policy={},
        scope=scope,
        status="active",
    )
    runtime.store.append(rule)

    bundle = runtime.memory.recall(
        query="UUMit delivery quality",
        scope=scope,
        task_context={"task_type": "delivery.review"},
        limit=2,
    )

    assert bundle.items[0].source == "trusted.delivery"
```

- [ ] **Step 2: Run test to verify RED**

Run:

```bash
python -m pytest tests/test_autonomous_safe_patches.py::test_active_policy_source_weights_affect_recall_ranking -q
```

Expected: FAIL because active-policy source weights are not merged into recall filters.

- [ ] **Step 3: Implement active-policy merge**

In `MemoryAPI.recall`, after `recall_filters = self._recall_filters_from_task_context(task_context)`, merge `retrieval_policy.get("source_weights")` before recall intent weights:

```python
policy_weights = self._source_weights(retrieval_policy.get("source_weights"))
if policy_weights:
    recall_filters["source_weights"] = {**policy_weights, **dict(recall_filters.get("source_weights") or {})}
```

- [ ] **Step 4: Add safe patch type**

Add `patch_type="active_rule"` support in `_apply_safe_patch`, storing an active rule with retrieval policy such as:

```python
{
  "recall_profile": "precision",
  "source_weights": {"openclaw.agent_end": 0.2}
}
```

- [ ] **Step 5: Run tests**

Run:

```bash
python -m pytest tests/test_autonomous_safe_patches.py tests/test_runtime.py::test_runtime_recall_uses_active_policy_for_task_context -q
```

Expected: PASS.

---

### Task 4: Web Learning Scout

**Files:**
- Create: `eimemory/governance/web_learning.py`
- Modify: `eimemory/governance/autonomous_evolution.py`
- Test: `tests/test_web_learning_scout.py`

- [ ] **Step 1: Write failing tests**

Add tests for evidence-only web hypotheses.

```python
from eimemory.governance.web_learning import scout_web_learning


def test_web_learning_scout_emits_hypotheses_without_applying_policy(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    report = scout_web_learning(
        runtime,
        scope=scope,
        evidence=[
            {
                "url": "https://example.com/rag-rerank",
                "title": "Hybrid retrieval and reranking",
                "text": "Production RAG systems often reduce noisy retrieval by using hybrid retrieval and reranking.",
            }
        ],
    )

    assert report["ok"] is True
    assert report["hypothesis_count"] == 1
    assert report["hypotheses"][0]["source"] == "web_scout"
    assert report["hypotheses"][0]["risk_level"] == "medium"
    assert runtime.search_policy("hybrid retrieval", scope=scope)["policy_suggestions"] == []
```

- [ ] **Step 2: Run test to verify RED**

Run:

```bash
python -m pytest tests/test_web_learning_scout.py::test_web_learning_scout_emits_hypotheses_without_applying_policy -q
```

Expected: FAIL because module does not exist.

- [ ] **Step 3: Implement scout**

Implement `scout_web_learning(runtime, scope, urls=None, evidence=None, timeout_seconds=8)`:

- Accept explicit evidence list for deterministic tests.
- Optionally fetch URLs with `urllib.request` when URLs are provided.
- Store a reflection record with `report_type="web_learning_scout"`.
- Return hypotheses with `risk_level="medium"` and replay-case hints.
- Do not call `upsert_intent_pattern`, `store_rule`, or any mutating safe patch API.

- [ ] **Step 4: Integrate with controller**

Let `run_autonomous_evolution(..., web_hypotheses=...)` include these as opportunities but keep them blocked unless local replay passes.

- [ ] **Step 5: Run tests**

Run:

```bash
python -m pytest tests/test_web_learning_scout.py -q
```

Expected: PASS.

---

### Task 5: Runtime, CLI, And Nightly Integration

**Files:**
- Modify: `eimemory/api/runtime.py`
- Modify: `eimemory/scheduler/jobs.py`
- Modify: `eimemory/cli/main.py`
- Test: `tests/test_autonomous_evolution_platform.py`

- [ ] **Step 1: Write failing tests**

Add tests for `Runtime.run_autonomous_evolution`, CLI, and nightly summary.

```python
import json

from eimemory.api.runtime import Runtime
from eimemory.cli.main import main as cli_main
from eimemory.scheduler.jobs import run_nightly_jobs


def test_runtime_and_nightly_include_autonomous_evolution(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    report = runtime.run_autonomous_evolution(scope=scope, apply=False)
    nightly = run_nightly_jobs(runtime, scope=scope)

    assert report["report_type"] == "autonomous_evolution"
    assert nightly["autonomous_evolution"]["ok"] is True


def test_cli_evolve_autonomous_prints_report(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path))
    exit_code = cli_main(["evolve", "autonomous", "--scope-agent", "hongtu", "--scope-workspace", "embodied"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["report_type"] == "autonomous_evolution"
```

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
python -m pytest tests/test_autonomous_evolution_platform.py -q
```

Expected: FAIL because Runtime/CLI/nightly integration is missing.

- [ ] **Step 3: Add Runtime method**

Add to `Runtime`:

```python
def run_autonomous_evolution(
    self,
    *,
    scope: dict | None = None,
    apply: bool = False,
    max_apply: int = 3,
    web_hypotheses: list[dict] | None = None,
    persist_report: bool = False,
) -> dict:
    from eimemory.governance.autonomous_evolution import run_autonomous_evolution
    return run_autonomous_evolution(
        self,
        scope=scope,
        apply=apply,
        max_apply=max_apply,
        web_hypotheses=web_hypotheses,
        persist_report=persist_report,
    )
```

- [ ] **Step 4: Add nightly call**

In `run_nightly_jobs`, call:

```python
autonomous_evolution_report = _run_autonomous_evolution(runtime, scope=scope)
```

and include `"autonomous_evolution": autonomous_evolution_report`.

- [ ] **Step 5: Add CLI command**

Add `eimemory evolve autonomous` with `--apply`, `--max-apply`, `--persist-report`, `--web-evidence-json`.

- [ ] **Step 6: Run tests**

Run:

```bash
python -m pytest tests/test_autonomous_evolution_platform.py tests/test_platform.py::test_cli_nightly_runs_all_jobs -q
```

Expected: PASS.

---

### Task 6: Final Regression And Release Prep

**Files:**
- Modify: `pyproject.toml`
- Modify: `eimemory/version.py`
- Modify: `tests/test_version.py`

- [ ] **Step 1: Bump version**

Set version to `0.9.0` because this adds a new governance capability and public CLI behavior.

- [ ] **Step 2: Run focused regression**

Run:

```bash
python -m pytest tests/test_autonomous_evolution_controller.py tests/test_autonomous_safe_patches.py tests/test_web_learning_scout.py tests/test_autonomous_evolution_platform.py tests/test_judgment.py tests/test_rule_evolution_loop.py tests/test_production_recall_eval.py tests/test_version.py -q
```

Expected: PASS.

- [ ] **Step 3: Static checks**

Run:

```bash
python -m compileall eimemory
git diff --check
```

Expected: both pass.

- [ ] **Step 4: Commit**

Commit message:

```bash
git add eimemory tests docs pyproject.toml
git commit -m "feat: add autonomous evolution controller"
```

---

## Self-Review

- Spec coverage: opportunity mining, replay synthesis, safe patching, web scout, reporting, nightly, CLI, and versioning are covered.
- Placeholder scan: no task uses TBD/TODO/fill-in language.
- Type consistency: `run_autonomous_evolution`, `scout_web_learning`, `safe_patches`, `opportunities`, `replay_cases`, and report fields are named consistently across tasks.
