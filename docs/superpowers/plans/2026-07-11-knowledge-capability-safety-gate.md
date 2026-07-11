# Knowledge Capability Safety Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the external-knowledge-to-capability safety loop with deterministic gates at ingest, recall, and skill validation.

**Architecture:** Add a shared `eimemory.knowledge.safety` gate and wire it into the existing `knowledge.ingest`, `api.memory`, and `skill_validation` boundaries. Keep behavior conservative: unsafe or low-trust external knowledge can persist only as quarantined/review material and cannot become active recall or canary capability evidence.

**Tech Stack:** Python 3.14, pytest, existing `RecordEnvelope`/`RuntimeStore` APIs, existing intake prompt-injection and secret detectors.

## Global Constraints

- Use TDD: write failing tests before production edits.
- Do not add network calls or new dependencies.
- Preserve high-trust official/API/docs flows.
- Quarantined content must not echo secrets or injection text.
- Deploy only after local and honxin verification.

---

### Task 1: Regression Tests

**Files:**
- Modify: `tests/test_knowledge_ingest.py`
- Modify: `tests/test_runtime.py`
- Modify: `tests/test_skill_validation.py`

**Interfaces:**
- Consumes: existing `Runtime.ingest_knowledge_source`, `Runtime.memory.recall`, `Runtime.validate_skill_candidate`.
- Produces: failing evidence for the missing safety gate.

- [ ] Add tests proving prompt injection is quarantined/redacted during knowledge ingest.
- [ ] Add tests proving low-trust external knowledge is blocked from default recall.
- [ ] Add tests proving low-trust external skill candidates cannot validate to canary.
- [ ] Run each test and confirm it fails for the expected missing gate.

### Task 2: Shared Knowledge Safety Gate

**Files:**
- Create: `eimemory/knowledge/safety.py`

**Interfaces:**
- Produces: `evaluate_knowledge_safety(payload_or_record, task="ingest|recall|capability") -> dict[str, object]`.

- [ ] Implement deterministic source-trust extraction, prompt-injection detection, secret detection, source/provenance checks, and status decisions.
- [ ] Keep the return shape JSON-safe and caller-neutral.

### Task 3: Wire Gate Into Ingest, Recall, and Skill Validation

**Files:**
- Modify: `eimemory/knowledge/ingest.py`
- Modify: `eimemory/api/memory.py`
- Modify: `eimemory/governance/skill_validation.py`

**Interfaces:**
- Consumes: `evaluate_knowledge_safety`.
- Produces: quarantined knowledge units, default recall blocking, and failed sandbox checks for unsafe knowledge-derived candidates.

- [ ] Persist unsafe knowledge units as `status="quarantined"` with redacted text.
- [ ] Add online recall block reasons for `external_knowledge_untrusted` and `external_knowledge_quarantined`.
- [ ] Add a `knowledge_safety` sandbox check to skill validation.

### Task 4: Version, Verification, Commit, Deploy

**Files:**
- Modify: `pyproject.toml`
- Modify: `eimemory/version.py`

**Interfaces:**
- Produces: version `1.9.11`, pushed commit, honxin deployment.

- [ ] Run focused tests for knowledge ingest, runtime recall, skill candidate/validation, evidence gate, and version.
- [ ] Run `python -m compileall -q eimemory scripts`.
- [ ] Run `git diff --check`.
- [ ] Commit, push, sync honxin `/dev-project/eimemory`, deploy immutable release, restart services, and verify `/health`.
