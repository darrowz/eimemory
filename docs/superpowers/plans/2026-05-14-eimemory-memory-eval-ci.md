# EIMemory Memory Evaluation CI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a benchmark-inspired memory evaluation CI layer for eimemory that measures extraction, update, usage, hallucination, consistency, time reasoning, and efficiency, then feeds failures back into incidents, replay datasets, and autonomous rule evolution.

**Architecture:** Extend the existing `eimemory.evaluation.framework` instead of replacing it. Keep deterministic local evaluation as the default, add a small typed contract for benchmark suites, emit structured reports, and wire failing samples into existing `incident` and rule-evolution paths. The first release should not depend on external benchmark downloads or LLM judges.

**Tech Stack:** Python 3.11, existing `Runtime`, `MemoryAPI`, `EvolutionAPI`, JSON datasets, pytest, existing CLI parser in `eimemory/cli/main.py`.

---

## Version Decision

Bump the package version from `0.1.0` to `0.2.0`.

Reason: this is a new product capability, not a patch. It adds public CLI behavior, report schema fields, benchmark-style metrics, optional incident emission, and nightly/governance integration. Because the package is still pre-1.0, a minor bump is the right signal.

Files to update in the final task:
- `pyproject.toml`: `version = "0.2.0"`
- `eimemory/version.py`: `__version__ = "0.2.0"`

Do not bump to `0.1.1`; that would understate the API and workflow change.

## External Evaluation Concepts To Map

Use benchmark ideas as design input, not as hard dependencies:

- LoCoMo: long-conversation consistency and temporal reasoning.
- LongMemEval: conflict handling, cross-session recall, time reasoning.
- RealMem: project lifecycle state updates, R@k, NDCG@k, QA-style score, latency, token/cost proxy.
- HaluMem: operation-level hallucination split into extraction, update, usage.
- ImplicitMemBench: procedural preference and habit internalization.

Reference used by the planning session:
- `https://segmentfault.com/a/1190000047400355`

## File Structure

Create:
- `eimemory/evaluation/contracts.py`
  - Typed helpers and normalization for suite/case/report dictionaries.
- `eimemory/evaluation/metrics.py`
  - Deterministic metrics: recall@k, precision@k, MRR, NDCG@k, memory recall, hallucination rate, conflict accuracy, latency summary.
- `eimemory/evaluation/benchmarks.py`
  - Phase runners for `extraction`, `update`, `usage`, `consistency`, `temporal`, `implicit`.
- `examples/evaluation/memory_ci.json`
  - Built-in smoke dataset that exercises the new benchmark contract.
- `tests/test_memory_eval_ci.py`
  - End-to-end tests for suite execution, metrics, incident emission, and CLI.

Modify:
- `eimemory/evaluation/framework.py`
  - Preserve `run_evaluation(...)`; add suite-mode branching and richer report fields.
- `eimemory/evaluation/__init__.py`
  - Export new evaluation helpers.
- `eimemory/api/runtime.py`
  - Add `run_memory_eval_ci(...)`, preserving existing `run_evaluation(...)`.
- `eimemory/cli/main.py`
  - Add `eimemory eval ci DATASET --threshold --emit-incidents --output`.
- `eimemory/scheduler/jobs.py`
  - Add optional nightly memory evaluation summary using the built-in smoke suite when configured or requested.
- `eimemory/governance/snapshot.py`
  - Surface latest memory eval report and incident counts.
- `eimemory/governance/console.py`
  - Add compact memory eval card with pass rate, hallucination rate, and failed phase counts.
- `eimemory/governance/rule_evolution.py`
  - Let `incident` records with `meta.eval_failure == True` generate replay datasets and candidate rules when repair hints exist.
- `pyproject.toml`
  - Bump version to `0.2.0`.
- `eimemory/version.py`
  - Bump version to `0.2.0`.

## Report Contract

New suite report shape:

```json
{
  "ok": true,
  "schema_version": 2,
  "report_type": "memory_eval_ci",
  "name": "memory-ci-smoke",
  "scope": {"tenant_id": "default", "agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"},
  "sample_count": 6,
  "pass_count": 5,
  "fail_count": 1,
  "pass_rate": 0.833,
  "threshold": 0.8,
  "passed_threshold": true,
  "phase_scores": {
    "extraction": {"sample_count": 1, "pass_rate": 1.0, "hallucination_rate": 0.0},
    "update": {"sample_count": 1, "pass_rate": 1.0, "conflict_accuracy": 1.0},
    "usage": {"sample_count": 2, "pass_rate": 0.5, "recall_at_k": 0.5, "mrr": 0.5},
    "temporal": {"sample_count": 1, "pass_rate": 1.0, "temporal_accuracy": 1.0},
    "implicit": {"sample_count": 1, "pass_rate": 1.0, "habit_accuracy": 1.0}
  },
  "efficiency": {"latency_ms_avg": 0.0, "latency_ms_p95": 0.0, "case_count": 6},
  "failures": [],
  "incident_record_ids": [],
  "samples": []
}
```

Keep old `schema_version = 1` recall reports working for current callers.

## Task 1: Add Metric Primitives

**Files:**
- Create: `eimemory/evaluation/metrics.py`
- Test: `tests/test_memory_eval_ci.py`

- [ ] **Step 1: Write failing metric tests**

Add this to `tests/test_memory_eval_ci.py`:

```python
from __future__ import annotations

from eimemory.evaluation.metrics import (
    binary_pass_rate,
    mean_reciprocal_rank,
    ndcg_at_k,
    percentile,
    precision_at_k,
    recall_at_k,
)


def test_memory_eval_metric_primitives_are_deterministic() -> None:
    returned = ["a", "b", "c", "d"]
    expected = {"b", "x"}

    assert recall_at_k(returned, expected, k=3) == 0.5
    assert precision_at_k(returned, expected, k=3) == 0.333
    assert mean_reciprocal_rank([0, 2, 0, 1]) == 0.375
    assert binary_pass_rate([True, False, True]) == 0.667
    assert ndcg_at_k(returned, expected, k=3) == 0.387
    assert percentile([10, 20, 30, 40], 95) == 40.0
```

- [ ] **Step 2: Run test and confirm failure**

Run:

```bash
python -m pytest -q tests/test_memory_eval_ci.py::test_memory_eval_metric_primitives_are_deterministic
```

Expected:

```text
ModuleNotFoundError: No module named 'eimemory.evaluation.metrics'
```

- [ ] **Step 3: Implement `eimemory/evaluation/metrics.py`**

Create the file with this content:

```python
from __future__ import annotations

import math
from statistics import mean


def _round(value: float) -> float:
    return round(float(value), 3)


def recall_at_k(returned_ids: list[str], expected_ids: set[str], *, k: int) -> float:
    if not expected_ids:
        return 1.0 if returned_ids else 0.0
    top = returned_ids[: max(0, int(k))]
    hits = len({item for item in top if item in expected_ids})
    return _round(hits / len(expected_ids))


def precision_at_k(returned_ids: list[str], expected_ids: set[str], *, k: int) -> float:
    top = returned_ids[: max(0, int(k))]
    if not top:
        return 0.0
    hits = len([item for item in top if item in expected_ids])
    return _round(hits / len(top))


def mean_reciprocal_rank(ranks: list[int]) -> float:
    if not ranks:
        return 0.0
    values = [(1.0 / rank) if rank > 0 else 0.0 for rank in ranks]
    return _round(mean(values))


def binary_pass_rate(values: list[bool]) -> float:
    if not values:
        return 0.0
    return _round(sum(1 for item in values if item) / len(values))


def ndcg_at_k(returned_ids: list[str], expected_ids: set[str], *, k: int) -> float:
    top = returned_ids[: max(0, int(k))]
    if not top or not expected_ids:
        return 0.0
    dcg = 0.0
    for index, item in enumerate(top, start=1):
        if item in expected_ids:
            dcg += 1.0 / math.log2(index + 1)
    ideal_hits = min(len(expected_ids), len(top))
    idcg = sum(1.0 / math.log2(index + 1) for index in range(1, ideal_hits + 1))
    return _round(dcg / idcg if idcg else 0.0)


def percentile(values: list[float], pct: int) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(item) for item in values)
    index = math.ceil((max(0, min(100, int(pct))) / 100.0) * len(ordered)) - 1
    return _round(ordered[max(0, min(index, len(ordered) - 1))])
```

- [ ] **Step 4: Verify metric tests pass**

Run:

```bash
python -m pytest -q tests/test_memory_eval_ci.py::test_memory_eval_metric_primitives_are_deterministic
```

Expected:

```text
1 passed
```

- [ ] **Step 5: Commit**

```bash
git add eimemory/evaluation/metrics.py tests/test_memory_eval_ci.py
git commit -m "feat(eval): add memory evaluation metrics"
```

## Task 2: Add Suite Contracts

**Files:**
- Create: `eimemory/evaluation/contracts.py`
- Modify: `tests/test_memory_eval_ci.py`

- [ ] **Step 1: Add contract normalization test**

Append:

```python
from eimemory.evaluation.contracts import normalize_memory_eval_suite


def test_memory_eval_suite_normalization_sets_defaults() -> None:
    suite = normalize_memory_eval_suite(
        {
            "name": "memory-ci",
            "scope": {"agent_id": "hongtu", "workspace_id": "embodied"},
            "threshold": 0.75,
            "cases": [
                {
                    "id": "usage-case",
                    "phase": "usage",
                    "query": "official channel",
                    "expect_any_text": ["Feishu"],
                }
            ],
        }
    )

    assert suite["schema_version"] == 2
    assert suite["name"] == "memory-ci"
    assert suite["threshold"] == 0.75
    assert suite["cases"][0]["phase"] == "usage"
    assert suite["cases"][0]["limit"] == 5
    assert suite["cases"][0]["case_id"] == "usage-case"
```

- [ ] **Step 2: Run test and confirm failure**

Run:

```bash
python -m pytest -q tests/test_memory_eval_ci.py::test_memory_eval_suite_normalization_sets_defaults
```

Expected:

```text
ModuleNotFoundError: No module named 'eimemory.evaluation.contracts'
```

- [ ] **Step 3: Implement `contracts.py`**

Create:

```python
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from eimemory.models.records import ScopeRef

SUPPORTED_PHASES = {"extraction", "update", "usage", "consistency", "temporal", "implicit"}


def normalize_memory_eval_suite(dataset: dict | list) -> dict[str, Any]:
    if isinstance(dataset, list):
        raw = {"name": "memory_eval_suite", "cases": dataset}
    elif isinstance(dataset, dict):
        raw = dict(dataset)
    else:
        raise ValueError("memory eval suite must be a JSON object or list")

    scope = asdict(ScopeRef.from_dict(raw.get("scope") or {}))
    cases = [
        _normalize_case(item, index=index, default_scope=scope)
        for index, item in enumerate(list(raw.get("cases") or raw.get("samples") or []))
    ]
    threshold = _clamp_float(raw.get("threshold"), default=0.8)
    return {
        "schema_version": 2,
        "report_type": "memory_eval_ci",
        "name": str(raw.get("name") or "memory_eval_suite"),
        "scope": scope,
        "threshold": threshold,
        "profile": str(raw.get("profile") or "balanced"),
        "seed": list(raw.get("seed") or raw.get("seed_records") or []),
        "cases": cases,
        "emit_incidents": bool(raw.get("emit_incidents", False)),
    }


def _normalize_case(item: Any, *, index: int, default_scope: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {
            "case_id": str(index),
            "phase": "usage",
            "scope": dict(default_scope),
            "query": "",
            "limit": 5,
            "invalid_case": "invalid_case",
        }
    phase = str(item.get("phase") or "usage").strip().lower()
    if phase not in SUPPORTED_PHASES:
        phase = "usage"
    return {
        **dict(item),
        "case_id": str(item.get("case_id") or item.get("id") or index),
        "phase": phase,
        "scope": dict(item.get("scope") or default_scope),
        "limit": max(1, min(100, _int_value(item.get("limit"), default=5))),
    }


def _int_value(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clamp_float(value: Any, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return round(max(0.0, min(1.0, parsed)), 3)
```

- [ ] **Step 4: Verify contract tests pass**

Run:

```bash
python -m pytest -q tests/test_memory_eval_ci.py::test_memory_eval_suite_normalization_sets_defaults
```

Expected:

```text
1 passed
```

- [ ] **Step 5: Commit**

```bash
git add eimemory/evaluation/contracts.py tests/test_memory_eval_ci.py
git commit -m "feat(eval): add memory evaluation suite contract"
```

## Task 3: Implement Benchmark Phase Runner

**Files:**
- Create: `eimemory/evaluation/benchmarks.py`
- Modify: `eimemory/evaluation/framework.py`
- Modify: `eimemory/evaluation/__init__.py`
- Modify: `tests/test_memory_eval_ci.py`

- [ ] **Step 1: Add end-to-end suite test**

Append:

```python
from eimemory.api.runtime import Runtime


def test_memory_eval_ci_scores_usage_hallucination_and_phases(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    dataset = {
        "name": "memory-ci-smoke",
        "scope": scope,
        "threshold": 0.8,
        "seed": [
            {
                "title": "Official channel",
                "text": "Feishu is the official communication channel for Hongtu operator coordination.",
                "memory_type": "decision",
            },
            {
                "title": "Travel plan",
                "text": "The current travel destination is Chongqing, replacing the older Chengdu plan.",
                "memory_type": "fact",
            },
        ],
        "cases": [
            {
                "id": "extract-channel",
                "phase": "extraction",
                "input_text": "Use Feishu as the official channel.",
                "expect_memory_type": "decision",
                "expect_any_text": ["Feishu"],
            },
            {
                "id": "usage-channel",
                "phase": "usage",
                "query": "official communication channel",
                "expect_any_title": ["Official channel"],
                "limit": 3,
            },
            {
                "id": "no-fake-destination",
                "phase": "usage",
                "query": "current travel destination",
                "expect_any_text": ["Chongqing"],
                "forbid_any_text": ["Changsha"],
                "limit": 3,
            },
        ],
    }

    report = runtime.run_memory_eval_ci(dataset)

    assert report["ok"] is True
    assert report["schema_version"] == 2
    assert report["report_type"] == "memory_eval_ci"
    assert report["sample_count"] == 3
    assert report["pass_rate"] == 1.0
    assert report["passed_threshold"] is True
    assert report["phase_scores"]["extraction"]["pass_rate"] == 1.0
    assert report["phase_scores"]["usage"]["recall_at_k"] == 1.0
    assert report["phase_scores"]["usage"]["hallucination_rate"] == 0.0
```

- [ ] **Step 2: Run test and confirm failure**

Run:

```bash
python -m pytest -q tests/test_memory_eval_ci.py::test_memory_eval_ci_scores_usage_hallucination_and_phases
```

Expected:

```text
AttributeError: 'Runtime' object has no attribute 'run_memory_eval_ci'
```

- [ ] **Step 3: Implement `benchmarks.py`**

Create a deterministic runner with this public function:

```python
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import asdict
from typing import Any

from eimemory.api.memory import MemoryAPI
from eimemory.evaluation.contracts import normalize_memory_eval_suite
from eimemory.evaluation.metrics import binary_pass_rate, mean_reciprocal_rank, ndcg_at_k, percentile, precision_at_k, recall_at_k
from eimemory.models.records import ScopeRef


def run_memory_eval_ci(runtime: Any, dataset: dict | list, *, emit_incidents: bool = False) -> dict[str, Any]:
    suite = normalize_memory_eval_suite(dataset)
    if emit_incidents:
        suite["emit_incidents"] = True
    scope_ref = ScopeRef.from_dict(suite["scope"])
    seed_ids = _seed_records(runtime, suite)
    memory_api = MemoryAPI(runtime.store)
    samples: list[dict[str, Any]] = []
    incident_ids: list[str] = []

    for index, case in enumerate(suite["cases"]):
        started = time.perf_counter()
        sample = _run_case(runtime, memory_api, case, index=index, default_scope=scope_ref)
        sample["latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
        samples.append(sample)
        if not sample["passed"] and suite["emit_incidents"]:
            incident_ids.append(_emit_eval_incident(runtime, sample, suite).record_id)

    phase_scores = _phase_scores(samples)
    pass_values = [bool(sample["passed"]) for sample in samples]
    pass_rate = binary_pass_rate(pass_values)
    threshold = float(suite["threshold"])
    latencies = [float(sample["latency_ms"]) for sample in samples]
    failures = [sample for sample in samples if not sample["passed"]]
    return {
        "ok": True,
        "schema_version": 2,
        "report_type": "memory_eval_ci",
        "name": suite["name"],
        "scope": asdict(scope_ref),
        "seeded_record_ids": seed_ids,
        "sample_count": len(samples),
        "pass_count": sum(1 for item in samples if item["passed"]),
        "fail_count": len(failures),
        "pass_rate": pass_rate,
        "threshold": round(threshold, 3),
        "passed_threshold": pass_rate >= threshold,
        "phase_scores": phase_scores,
        "efficiency": {
            "latency_ms_avg": round(sum(latencies) / len(latencies), 3) if latencies else 0.0,
            "latency_ms_p95": percentile(latencies, 95),
            "case_count": len(samples),
        },
        "failures": failures,
        "incident_record_ids": incident_ids,
        "samples": samples,
    }
```

Then add helper functions in the same file:
- `_seed_records(runtime, suite)`
- `_run_case(runtime, memory_api, case, index, default_scope)`
- `_run_extraction_case(runtime, case, index, default_scope)`
- `_run_usage_case(memory_api, case, index, default_scope)`
- `_text_contains_any(values, terms)`
- `_phase_scores(samples)`
- `_emit_eval_incident(runtime, sample, suite)`

Use this behavior:
- `extraction`: ingest `case["input_text"]`, compare returned memory status, type, and text against expectations.
- `usage`, `consistency`, `temporal`, `implicit`: run recall and score returned titles/ids/text.
- `update`: for this version, treat it as a usage-style case with extra `expect_current_text` and `forbid_any_text` checks.
- `forbid_any_text`: if any forbidden term appears in returned title/summary/detail, mark sample failed and `hallucinated=True`.
- `expected_rank`: compute first matching rank; use it for MRR.

- [ ] **Step 4: Wire framework and runtime**

Modify `eimemory/evaluation/framework.py`:

```python
def run_memory_eval_ci(runtime, dataset: dict | list, *, emit_incidents: bool = False) -> dict:
    from eimemory.evaluation.benchmarks import run_memory_eval_ci as _run_memory_eval_ci

    return _run_memory_eval_ci(runtime, dataset, emit_incidents=emit_incidents)
```

Modify `eimemory/evaluation/__init__.py`:

```python
from eimemory.evaluation.framework import run_evaluation, run_memory_eval_ci

__all__ = ["run_evaluation", "run_memory_eval_ci"]
```

Modify `eimemory/api/runtime.py` after `run_evaluation(...)`:

```python
    def run_memory_eval_ci(
        self,
        dataset: dict | list,
        *,
        emit_incidents: bool = False,
    ) -> dict:
        from eimemory.evaluation import run_memory_eval_ci

        return run_memory_eval_ci(self, dataset, emit_incidents=emit_incidents)
```

- [ ] **Step 5: Verify suite runner test passes**

Run:

```bash
python -m pytest -q tests/test_memory_eval_ci.py::test_memory_eval_ci_scores_usage_hallucination_and_phases
```

Expected:

```text
1 passed
```

- [ ] **Step 6: Commit**

```bash
git add eimemory/evaluation/benchmarks.py eimemory/evaluation/framework.py eimemory/evaluation/__init__.py eimemory/api/runtime.py tests/test_memory_eval_ci.py
git commit -m "feat(eval): add benchmark-style memory eval runner"
```

## Task 4: Feed Evaluation Failures Into Incidents

**Files:**
- Modify: `eimemory/evaluation/benchmarks.py`
- Modify: `tests/test_memory_eval_ci.py`

- [ ] **Step 1: Add incident emission test**

Append:

```python
def test_memory_eval_ci_can_emit_incidents_for_failed_samples(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    dataset = {
        "name": "memory-ci-failure",
        "scope": scope,
        "threshold": 1.0,
        "seed": [],
        "cases": [
            {
                "id": "missing-channel",
                "phase": "usage",
                "query": "official communication channel",
                "expect_any_text": ["Feishu"],
                "repair_hint": "Prefer Feishu as the official coordination channel when this preference is present.",
            }
        ],
    }

    report = runtime.run_memory_eval_ci(dataset, emit_incidents=True)
    incidents = runtime.store.list_records(kinds=["incident"], scope=scope, limit=10)

    assert report["passed_threshold"] is False
    assert report["incident_record_ids"] == [incidents[0].record_id]
    assert incidents[0].meta["eval_failure"] is True
    assert incidents[0].meta["eval_phase"] == "usage"
    assert incidents[0].meta["repair_hint"] == "Prefer Feishu as the official coordination channel when this preference is present."
```

- [ ] **Step 2: Run test and confirm failure**

Run:

```bash
python -m pytest -q tests/test_memory_eval_ci.py::test_memory_eval_ci_can_emit_incidents_for_failed_samples
```

Expected:

```text
AssertionError
```

- [ ] **Step 3: Implement incident emission**

In `eimemory/evaluation/benchmarks.py`, implement `_emit_eval_incident`:

```python
def _emit_eval_incident(runtime: Any, sample: dict[str, Any], suite: dict[str, Any]):
    return runtime.evolution.observe(
        signal_type="incident",
        payload={
            "title": f"Memory eval failure: {sample['case_id']}",
            "summary": sample.get("failure_reason") or "Memory evaluation sample failed.",
            "incident_type": "memory_eval_failure",
            "severity": "medium",
            "eval_failure": True,
            "eval_suite": suite["name"],
            "eval_case_id": sample["case_id"],
            "eval_phase": sample["phase"],
            "query": sample.get("query", ""),
            "expected": sample.get("expected", {}),
            "returned_record_ids": sample.get("returned_record_ids", []),
            "repair_hint": sample.get("repair_hint", ""),
            "suggested_replay_dataset": [
                {
                    "id": sample["case_id"],
                    "query": sample.get("query", ""),
                    "scope": sample.get("scope") or suite["scope"],
                    "task_context": sample.get("task_context", {}),
                    "expect_any_title": sample.get("expected_titles", []),
                    "expect_any_record_id": sample.get("expected_record_ids", []),
                    "expect_any_text": sample.get("expected_text", []),
                    "limit": sample.get("limit", 5),
                }
            ],
        },
        scope=suite["scope"],
    )
```

Ensure `_run_case` copies `repair_hint` from the case into failed sample reports.

- [ ] **Step 4: Verify incident test passes**

Run:

```bash
python -m pytest -q tests/test_memory_eval_ci.py::test_memory_eval_ci_can_emit_incidents_for_failed_samples
```

Expected:

```text
1 passed
```

- [ ] **Step 5: Commit**

```bash
git add eimemory/evaluation/benchmarks.py tests/test_memory_eval_ci.py
git commit -m "feat(eval): emit incidents from memory eval failures"
```

## Task 5: Let Rule Evolution Consume Eval Incidents

**Files:**
- Modify: `eimemory/governance/rule_evolution.py`
- Modify: `tests/test_rule_evolution_loop.py`

- [ ] **Step 1: Add failing rule evolution test**

Append to `tests/test_rule_evolution_loop.py`:

```python
def test_rule_evolution_creates_candidate_from_eval_incident_repair_hint(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    incident = runtime.evolution.observe(
        signal_type="incident",
        payload={
            "title": "Memory eval failure: official-channel",
            "summary": "Memory evaluation sample failed.",
            "incident_type": "memory_eval_failure",
            "severity": "medium",
            "eval_failure": True,
            "eval_phase": "usage",
            "repair_hint": "Prefer Feishu as the official coordination channel.",
            "suggested_replay_dataset": [
                {
                    "query": "official coordination channel",
                    "scope": scope,
                    "expect_any_text": ["Feishu"],
                    "limit": 3,
                }
            ],
        },
        scope=scope,
    )

    report = run_rule_evolution_loop(runtime, scope, apply=True)
    rules = runtime.store.list_records(kinds=["rule"], scope=scope, limit=10)

    assert report["source_counts"]["incident_repair"] == 1
    assert report["record_ids"]["source_incidents"] == [incident.record_id]
    assert rules[0].summary == "Prefer Feishu as the official coordination channel."
    assert rules[0].meta["evolution_source_type"] == "incident_repair"
    assert rules[0].meta["suggested_replay_dataset"][0]["query"] == "official coordination channel"
```

- [ ] **Step 2: Run test and confirm failure**

Run:

```bash
python -m pytest -q tests/test_rule_evolution_loop.py::test_rule_evolution_creates_candidate_from_eval_incident_repair_hint
```

Expected:

```text
AssertionError
```

- [ ] **Step 3: Modify incident candidate logic**

In `eimemory/governance/rule_evolution.py`, update `_candidate_from_incident_repair` so it first checks eval incidents:

```python
def _candidate_from_incident_repair(incident: RecordEnvelope, reflections: list[RecordEnvelope]) -> dict | None:
    if bool(incident.meta.get("eval_failure")):
        repair_hint = _clean_text(incident.meta.get("repair_hint") or incident.content.get("payload", {}).get("repair_hint") or "")
        if repair_hint:
            return _candidate_from_eval_incident(incident, repair_hint)
    reflection = _matching_repair_reflection(incident, reflections)
    ...
```

Add helper:

```python
def _candidate_from_eval_incident(incident: RecordEnvelope, repair_hint: str) -> dict:
    payload = dict(incident.content.get("payload") or {})
    task_type = str(incident.meta.get("task_type") or payload.get("eval_phase") or "memory_eval_failure")
    replay_dataset = payload.get("suggested_replay_dataset")
    if not isinstance(replay_dataset, list):
        replay_dataset = []
    source_key = _source_key("incident_repair", [incident.record_id])
    return {
        "title": f"Rule: {repair_hint}",
        "summary": repair_hint,
        "task_type": task_type,
        "retrieval_policy": {"route_hint": "task_context_first"},
        "response_policy": {"summary": repair_hint},
        "feedback": None,
        "reflection": None,
        "source_type": "incident_repair",
        "source_records": [incident],
        "source_key": source_key,
        "confidence_score": 0.72,
        "suggested_replay_dataset": replay_dataset,
        "audit_meta": {
            "task_type": task_type,
            "retrieval_policy": {"route_hint": "task_context_first"},
            "response_policy": {"summary": repair_hint},
            "evolution_source": "rule_evolution_loop",
            "evolution_source_type": "incident_repair",
            "evolution_source_key": source_key,
            "evolution_source_record_ids": [incident.record_id],
            "confidence_score": 0.72,
            "incident_record_id": incident.record_id,
            "eval_failure": True,
            "eval_phase": str(payload.get("eval_phase") or incident.meta.get("eval_phase") or ""),
        },
    }
```

- [ ] **Step 4: Verify rule evolution test passes**

Run:

```bash
python -m pytest -q tests/test_rule_evolution_loop.py::test_rule_evolution_creates_candidate_from_eval_incident_repair_hint
```

Expected:

```text
1 passed
```

- [ ] **Step 5: Commit**

```bash
git add eimemory/governance/rule_evolution.py tests/test_rule_evolution_loop.py
git commit -m "feat(evolution): synthesize rules from eval failure incidents"
```

## Task 6: Add CLI Command `eval ci`

**Files:**
- Modify: `eimemory/cli/main.py`
- Modify: `tests/test_memory_eval_ci.py`
- Create: `examples/evaluation/memory_ci.json`

- [ ] **Step 1: Add example dataset**

Create `examples/evaluation/memory_ci.json`:

```json
{
  "name": "memory-ci-smoke",
  "scope": {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"},
  "threshold": 0.8,
  "seed": [
    {
      "title": "Official channel",
      "text": "Feishu is the official communication channel for Hongtu operator coordination.",
      "memory_type": "decision"
    },
    {
      "title": "Current destination",
      "text": "The current travel destination is Chongqing, replacing the older Chengdu plan.",
      "memory_type": "fact"
    }
  ],
  "cases": [
    {
      "id": "usage-channel",
      "phase": "usage",
      "query": "official communication channel",
      "expect_any_title": ["Official channel"],
      "limit": 3
    },
    {
      "id": "temporal-current-destination",
      "phase": "temporal",
      "query": "current travel destination",
      "expect_any_text": ["Chongqing"],
      "forbid_any_text": ["Changsha"],
      "limit": 3
    }
  ]
}
```

- [ ] **Step 2: Add CLI test**

Append:

```python
import json

from eimemory.cli.main import main as cli_main


def test_cli_eval_ci_writes_report_and_returns_nonzero_below_threshold(tmp_path, monkeypatch, capsys) -> None:
    root = tmp_path / "runtime"
    monkeypatch.setenv("EIMEMORY_ROOT", str(root))
    dataset_path = tmp_path / "dataset.json"
    output_path = tmp_path / "report.json"
    dataset_path.write_text(
        json.dumps(
            {
                "name": "cli-memory-ci",
                "threshold": 1.0,
                "cases": [
                    {
                        "id": "missing",
                        "phase": "usage",
                        "query": "missing preference",
                        "expect_any_text": ["not present"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    exit_code = cli_main(["eval", "ci", str(dataset_path), "--emit-incidents", "--output", str(output_path)])
    printed = json.loads(capsys.readouterr().out)
    written = json.loads(output_path.read_text(encoding="utf-8"))

    assert exit_code == 1
    assert printed["output"] == str(output_path)
    assert written["report_type"] == "memory_eval_ci"
    assert written["passed_threshold"] is False
    assert len(written["incident_record_ids"]) == 1
```

- [ ] **Step 3: Run CLI test and confirm failure**

Run:

```bash
python -m pytest -q tests/test_memory_eval_ci.py::test_cli_eval_ci_writes_report_and_returns_nonzero_below_threshold
```

Expected:

```text
SystemExit: 2
```

- [ ] **Step 4: Modify CLI parser and handler**

In `_build_parser`, after existing `eval_run` setup, add:

```python
    eval_ci = eval_sub.add_parser("ci")
    eval_ci.add_argument("dataset_json")
    eval_ci.add_argument("--threshold", type=float, default=None)
    eval_ci.add_argument("--emit-incidents", action="store_true")
    eval_ci.add_argument("--output", default="")
```

In `main`, inside `if parsed.command == "eval":`, add before the usage fallback:

```python
        if parsed.eval_command == "ci":
            try:
                with open(parsed.dataset_json, "r", encoding="utf-8") as handle:
                    dataset = json.load(handle)
            except OSError as exc:
                print(json.dumps({"ok": False, "error": "dataset_unreadable", "detail": str(exc)}, ensure_ascii=False))
                return 2
            except json.JSONDecodeError:
                print(json.dumps({"ok": False, "error": "invalid_dataset_json"}, ensure_ascii=False))
                return 2
            if parsed.threshold is not None and isinstance(dataset, dict):
                dataset = {**dataset, "threshold": parsed.threshold}
            try:
                report = runtime.run_memory_eval_ci(dataset, emit_incidents=bool(parsed.emit_incidents))
            except ValueError as exc:
                print(json.dumps({"ok": False, "error": "invalid_eval_dataset", "detail": str(exc)}, ensure_ascii=False))
                return 2
            if parsed.output:
                try:
                    output_path = Path(parsed.output)
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                except OSError as exc:
                    print(json.dumps({"ok": False, "error": "eval_output_failed", "detail": str(exc)}, ensure_ascii=False))
                    return 2
                report = {**report, "output": str(output_path)}
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report.get("passed_threshold") else 1
```

- [ ] **Step 5: Verify CLI test passes**

Run:

```bash
python -m pytest -q tests/test_memory_eval_ci.py::test_cli_eval_ci_writes_report_and_returns_nonzero_below_threshold
```

Expected:

```text
1 passed
```

- [ ] **Step 6: Commit**

```bash
git add eimemory/cli/main.py examples/evaluation/memory_ci.json tests/test_memory_eval_ci.py
git commit -m "feat(cli): add memory evaluation ci command"
```

## Task 7: Add Nightly And Governance Reporting

**Files:**
- Modify: `eimemory/scheduler/jobs.py`
- Modify: `eimemory/governance/snapshot.py`
- Modify: `eimemory/governance/console.py`
- Modify: `tests/test_active_intake_platform.py`
- Modify: `tests/test_governance.py`
- Modify: `tests/test_governance_console.py`

- [ ] **Step 1: Add scheduler test**

In `tests/test_active_intake_platform.py`, extend the nightly test to assert a `memory_eval_ci` section exists:

```python
    assert report["memory_eval_ci"]["ok"] is True
    assert "pass_rate" in report["memory_eval_ci"]
    assert "passed_threshold" in report["memory_eval_ci"]
```

- [ ] **Step 2: Modify scheduler with safe fallback**

In `eimemory/scheduler/jobs.py`, add `_run_memory_eval_ci` similar to `_run_rule_evolution`:

```python
def _run_memory_eval_ci(runtime: Runtime, *, scope: dict) -> dict[str, Any]:
    run_eval = getattr(runtime, "run_memory_eval_ci", None)
    if run_eval is None:
        return {"ok": False, "pass_rate": 0.0, "passed_threshold": False, "eval_skipped_reason": "run_memory_eval_ci_unavailable"}
    dataset = {
        "name": "nightly-memory-ci-smoke",
        "scope": scope,
        "threshold": 0.0,
        "seed": [],
        "cases": [],
    }
    try:
        return _json_safe(run_eval(dataset, emit_incidents=False))
    except Exception as exc:  # pragma: no cover - defensive scheduler boundary
        return {
            "ok": False,
            "pass_rate": 0.0,
            "passed_threshold": False,
            "eval_skipped_reason": "",
            "error": exc.__class__.__name__,
            "detail": str(exc),
        }
```

Add it to `run_nightly_jobs(...)` report as `"memory_eval_ci": memory_eval_ci_report`.

- [ ] **Step 3: Add snapshot summary**

In `eimemory/governance/snapshot.py`, include latest records with `meta.report_type == "memory_eval_ci"` when building report summaries. Add summary fields:

```python
"memory_eval_ci": {
    "count": len(memory_eval_reports),
    "latest": _memory_eval_summary(memory_eval_reports[0]) if memory_eval_reports else None,
}
```

Helper:

```python
def _memory_eval_summary(record: RecordEnvelope) -> dict[str, Any]:
    report = dict(record.content.get("report") or {})
    return {
        "name": str(report.get("name") or record.title),
        "pass_rate": float(report.get("pass_rate") or 0.0),
        "passed_threshold": bool(report.get("passed_threshold")),
        "fail_count": int(report.get("fail_count") or 0),
        "incident_count": len(report.get("incident_record_ids") or []),
    }
```

- [ ] **Step 4: Add console card**

In `eimemory/governance/console.py`, add a compact card near rule evolution:

```python
      <section class="card card-small" draggable="true" data-card-id="memory-eval-ci">
        <h2>Memory Eval CI</h2>
        {_render_memory_eval_ci(snapshot)}
      </section>
```

Add helper:

```python
def _render_memory_eval_ci(snapshot: dict[str, Any]) -> str:
    section = _normalize_mapping(snapshot.get("memory_eval_ci"))
    latest = _normalize_mapping(section.get("latest"))
    if not latest:
        return '<div class="empty">No memory eval CI report available.</div>'
    return (
        f'{_render_mini("pass rate", latest.get("pass_rate"))}'
        f'{_render_mini("failed", latest.get("fail_count"))}'
        f'{_render_mini("incidents", latest.get("incident_count"))}'
    )
```

- [ ] **Step 5: Run targeted governance tests**

Run:

```bash
python -m pytest -q tests/test_active_intake_platform.py tests/test_governance.py tests/test_governance_console.py
```

Expected:

```text
passed
```

- [ ] **Step 6: Commit**

```bash
git add eimemory/scheduler/jobs.py eimemory/governance/snapshot.py eimemory/governance/console.py tests/test_active_intake_platform.py tests/test_governance.py tests/test_governance_console.py
git commit -m "feat(governance): surface memory evaluation ci"
```

## Task 8: Version Bump And Documentation

**Files:**
- Modify: `pyproject.toml`
- Modify: `eimemory/version.py`
- Modify: `README.md`
- Modify: `tests/test_runtime.py` or create `tests/test_version.py`

- [ ] **Step 1: Add version consistency test**

Create `tests/test_version.py`:

```python
from __future__ import annotations

import tomllib
from pathlib import Path

from eimemory.version import __version__


def test_package_version_matches_pyproject() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert __version__ == pyproject["project"]["version"]
    assert __version__ == "0.2.0"
```

- [ ] **Step 2: Run version test and confirm failure**

Run:

```bash
python -m pytest -q tests/test_version.py::test_package_version_matches_pyproject
```

Expected:

```text
AssertionError: assert '0.1.0' == '0.2.0'
```

- [ ] **Step 3: Bump version**

Change `pyproject.toml`:

```toml
version = "0.2.0"
```

Change `eimemory/version.py`:

```python
__version__ = "0.2.0"
```

- [ ] **Step 4: Document the feature**

Add a concise README section:

```markdown
## Memory Evaluation CI

`eimemory eval ci` runs a benchmark-style memory quality suite. It reports extraction, update, usage, consistency, temporal, implicit, hallucination, and efficiency signals, then can emit failed samples as incidents for autonomous rule evolution.

```bash
eimemory eval ci examples/evaluation/memory_ci.json --emit-incidents --output .tmp/memory-eval-report.json
```

Use `passed_threshold` as the CI gate. Use `incident_record_ids` to inspect failures that should become repair evidence or replay datasets.
```

- [ ] **Step 5: Verify version and README tests**

Run:

```bash
python -m pytest -q tests/test_version.py tests/test_evaluation_framework.py tests/test_memory_eval_ci.py
```

Expected:

```text
passed
```

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml eimemory/version.py README.md tests/test_version.py
git commit -m "chore: bump eimemory to 0.2.0"
```

## Task 9: Full Verification

**Files:**
- No new files.

- [ ] **Step 1: Run targeted evaluation tests**

```bash
python -m pytest -q tests/test_evaluation_framework.py tests/test_memory_eval_ci.py tests/test_rule_evolution_loop.py
```

Expected:

```text
passed
```

- [ ] **Step 2: Run governance and scheduler tests**

```bash
python -m pytest -q tests/test_active_intake_platform.py tests/test_governance.py tests/test_governance_console.py
```

Expected:

```text
passed
```

- [ ] **Step 3: Run full suite**

```bash
python -m pytest -q
```

Expected:

```text
passed
```

- [ ] **Step 4: Run CLI smoke manually**

```bash
python -m eimemory.cli.main eval ci examples/evaluation/memory_ci.json --output .tmp/memory-ci-report.json
```

Expected:

```text
JSON output includes "report_type": "memory_eval_ci" and "passed_threshold": true
```

- [ ] **Step 5: Rebuild code review graph for workspace**

From the Codex graph tool, run a full rebuild against:

```text
D:\github\ei-workspace
```

with:

```text
full_rebuild=true
postprocess=full
recurse_submodules=true
```

Expected:

```text
status=ok
files_parsed > 600
flows_detected > 0
communities_detected > 0
```

## Parallel Execution Map

Recommended subagent split:

- Agent A: Tasks 1-3, owns `eimemory/evaluation/*` and `tests/test_memory_eval_ci.py`.
- Agent B: Tasks 4-5, owns eval incident emission and `eimemory/governance/rule_evolution.py`.
- Agent C: Tasks 6-7, owns CLI, scheduler, governance snapshot/console.
- Agent D: Task 8 and final verification support, owns version/docs only after A-C merge.

Coordination rules:
- Do not let two agents edit `tests/test_memory_eval_ci.py` simultaneously without rebasing.
- Task 5 depends on Task 4's incident payload shape.
- Task 7 depends on Task 3's `Runtime.run_memory_eval_ci`.
- Task 8 must be last so the version bump represents the completed feature.

## Definition Of Done

- `eimemory eval run` remains backward compatible.
- `eimemory eval ci` exists and returns exit code `0` only when `passed_threshold` is true.
- Evaluation reports include phase-level scores and hallucination indicators.
- Failed samples can become `incident` records.
- Rule evolution can synthesize candidates from eval failure incidents that contain `repair_hint`.
- Nightly/governance can display memory eval status.
- Version is `0.2.0` in both `pyproject.toml` and `eimemory/version.py`.
- Full pytest suite passes.
- Workspace code review graph is rebuilt from `D:\github\ei-workspace` with recursive submodules.
