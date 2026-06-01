# eimemory Outcome Evolution 0.10.0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a complete outcome-evolution loop: OpenClaw/eimemory records task outcomes, diagnoses execution failures, preserves world/visual/operator evidence, generates replayable candidates, evaluates/shadows/promotes low-risk improvements, and rolls back bad attributed policies.

**Architecture:** Keep eimemory as the durable store. Persist `outcome_trace.v1` as `reflection` records, add deterministic diagnosis and sanitizer modules under `eimemory.experience`, expose outcome recording through Runtime/RPC/CLI/OpenClaw hooks, extend rule evolution with outcome-derived replay/candidate sources, and use existing rollout/replay governance for shadow, promotion, and rollback gates.

**Tech Stack:** Python 3.13, eimemory `Runtime`, `RecordEnvelope`, existing RPC server on port 8091, pytest, existing OpenClaw hook bridge.

---

## Non-Negotiable Boundaries

- Do not store raw screenshots, base64 images, cookies, tokens, authorization headers, camera URLs with credentials, or raw long transcripts.
- `primary_label` uses the stable taxonomy only: `success`, `missing_tool_call`, `argument_mismatch`, `stale_context`, `state_tracking_error`, `recovery_failure`, `user_correction`, `unsafe_or_high_risk`, `unknown_failure`.
- Extra signals such as `missing_visual_evidence`, `operator_gap`, `world_state_mismatch`, and `verifier_missing` live in `diagnosis.signals`, not `primary_label`.
- L0/L1 software-only improvements may be auto-shadowed/promoted after replay gates. L2 needs stronger replay evidence. L3/L4 and HA/device/account/privacy actions only create candidates and reports.
- OpenClaw task execution must degrade gracefully if eimemory RPC/outcome recording fails.

## Task Ownership

### Task A: Outcome Core

**Files:**
- Create: `eimemory/experience/sanitize.py`
- Create: `eimemory/experience/diagnosis.py`
- Create: `eimemory/experience/outcome.py`
- Modify: `eimemory/experience/__init__.py`
- Test: `tests/test_experience_outcome.py`

**Acceptance:**
- `record_outcome_trace(runtime, payload, scope=...)` validates, sanitizes, diagnoses, and persists an `outcome_trace.v1` reflection.
- Idempotency prevents duplicate `trace_id` / `idempotency_key` writes in the same scope.
- Optional `world_state`, `visual_evidence`, `operator_gap`, and `policy_attribution` are persisted after sanitization.
- Invalid sensitive/raw media payloads are rejected.

### Task B: Runtime, RPC, CLI

**Files:**
- Modify: `eimemory/api/runtime.py`
- Modify: `eimemory/adapters/eibrain/rpc.py`
- Modify: `eimemory/ei_bridge/protocol.py`
- Modify: `eimemory/cli/main.py`
- Create: `scripts/record_outcome_trace.py`
- Test: `tests/test_ei_bridge_outcome_rpc.py`
- Test: `tests/test_experience_bridge.py`

**Acceptance:**
- Runtime exposes `record_outcome_trace`.
- RPC method `experience.record_outcome_trace` records valid traces and rejects invalid payloads.
- CLI/helper can post a JSON trace to the local RPC endpoint.

### Task C: Replay, AIRA Candidate Search, Evolution

**Files:**
- Create: `eimemory/governance/outcome_replay.py`
- Create: `eimemory/governance/candidate_search.py`
- Modify: `eimemory/governance/rule_evolution.py`
- Test: `tests/test_outcome_replay.py`
- Test: `tests/test_rule_evolution_diagnosis_candidates.py`

**Acceptance:**
- Bad outcome traces become replay cases with positive/negative expectations and risk metadata.
- Repeated diagnosis/operator/visual/world-state patterns generate candidate rules with AIRA metadata.
- Candidates include `candidate_source`, `search_stage`, `proxy_eval`, `promotion_gate`, `risk_level`, source trace ids, and replay dataset hints.
- Repeated candidates are deduped by stable source key.

### Task D: OpenClaw Outcome Hooks

**Files:**
- Modify: `eimemory/adapters/openclaw/hooks.py`
- Test: `tests/test_openclaw_outcome_hooks.py`
- Test or extend: `tests/test_adapters.py`

**Acceptance:**
- `before_prompt_build` adds trace context and policy attribution.
- `on_agent_end`, `on_task_end`, and `on_session_end` best-effort record outcome traces.
- User corrections and failed outcomes become bad outcome traces.
- Hook failures do not fail the OpenClaw primary path.

### Task E: Nightly, Docs, Version, Deployment Surface

**Files:**
- Modify: `eimemory/scheduler/jobs.py`
- Modify: `pyproject.toml`
- Modify: `eimemory/version.py`
- Modify: `tests/test_version.py`
- Create: `docs/superpowers/specs/2026-06-01-outcome-evolution-design.md`
- Test: `tests/test_active_intake_platform.py` or a new targeted scheduler test.

**Acceptance:**
- Version is `0.10.0`.
- Nightly summary includes outcome trace counts, bad outcome rate, repeated diagnosis counts, generated/shadow/promoted/rolled-back counts, and top visual/operator/world-state gaps.
- Docs explain rollout gates and sensitive-data boundaries.

## Final Verification

- `python -m pytest tests/test_experience_outcome.py tests/test_ei_bridge_outcome_rpc.py tests/test_outcome_replay.py tests/test_rule_evolution_diagnosis_candidates.py tests/test_openclaw_outcome_hooks.py tests/test_version.py -q`
- `python -m pytest -q -k "not js_bridge and not export_records_includes_more_than_ten_thousand_rows"`
- `python -m compileall -q eimemory`
- RPC smoke against local or honxin `experience.record_outcome_trace`
- `eimemory recall` and `memory.search_policy` smoke still work
- Code Review Graph incremental index
- Commit, push `origin/master`, deploy immutable release to honxin, restart services, verify `/healthz`, gateway `/health`, and outcome RPC smoke.
