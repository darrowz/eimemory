# Production Query Authority Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Apply superpowers:test-driven-development for every behavior change and superpowers:verification-before-completion before claiming success.

**Goal:** Restore Codex and Hermes production-recall evidence to their authoritative channel scopes, prevent future nightly flattening, make dataset reads complete under load, and classify the bootstrap path in release lineage.

**Architecture:** Preserve valid Hongtu `embodied::channel::{codex,hermes}` scopes in identity normalization. Add a bounded, idempotent repair service that reconstructs channel scope only from mutually consistent protected evidence and rewrites pending, label, and accepted records in dependency order. Read accepted cases through the indexed report-type query, and run repair before production bootstrap collection.

**Tech Stack:** Python 3.11+, dataclasses/Pydantic record models, SQLite runtime store, pytest, immutable release scripts.

---

## Task 1: Preserve authoritative Hongtu channel scopes

**Files:**
- Modify: `eimemory/identity.py`
- Test: `tests/test_identity_ops.py`

1. Add a failing test that persists a Hongtu Codex evaluation record in `embodied::channel::codex`, runs `repair_hongtu_identity(..., apply=True)`, and asserts the exact outer scope remains unchanged while missing identity metadata is repaired.
2. Add the symmetric Hermes case and an already-canonical idempotence assertion.
3. Run `pytest -q tests/test_identity_ops.py` and confirm failure shows the scope is flattened to `embodied`.
4. Add a strict channel-scope predicate using `runtime_channel_from_scope()` plus `base_scope_from_channel()`. Accept only supported non-OpenClaw channel suffixes whose base is the canonical Hongtu scope.
5. Update `needs_hongtu_identity_repair()` to regard that predicate as canonical scope and update `normalize_hongtu_record()` to preserve the exact valid channel scope.
6. Run the focused test and commit only after it passes.

## Task 2: Add bounded, evidence-validated repair

**Files:**
- Create: `eimemory/evaluation/production_query_repair.py`
- Create: `tests/test_production_query_repair.py`
- Modify: `eimemory/evaluation/production_query_dataset.py`

1. Build a fixture with five accepted cases per channel using the public collection/acceptance APIs, then simulate the observed production flattening by rewriting only pending, label-evidence, and accepted envelopes to the base scope.
2. Assert readiness fails before repair, then specify the repair result contract: schema, `ok`, scanned/repaired/already-correct/conflict counts by record type and channel, bounded record IDs, and no query or memory bodies.
3. Add negative tests for forged embedded scope, source mismatch, missing candidate memory, cross-channel references, and a target-scope ID collision. Each must remain untouched and appear as a conflict.
4. Run the new test and confirm RED because the repair module does not exist.
5. Implement `repair_production_query_channel_scopes(runtime, *, scope, limit=500, persist_receipt=True)` with these rules:
   - Query only the three known report types from the canonical base scope through `list_records_by_meta_value`.
   - Reconstruct target scope from protected schema/channel/scope/source fields; never infer from free text.
   - Validate the exact target from `resolve_channel_scope`, the source ID, referenced candidate record, and pending/label/accepted relationship.
   - Rewrite pending first, label evidence second, accepted last using `RuntimeStore.rewrite(..., previous_scope=...)`.
   - Treat already-correct records as no-ops, block conflicts, cap all scans and returned ID lists, and make reruns converge.
   - Persist one compact receipt containing identifiers, counts, digests, and conflict reasons only.
6. Run the repair tests; fix only the minimal implementation until GREEN.

## Task 3: Make dataset reads indexed and complete

**Files:**
- Modify: `eimemory/evaluation/production_query_dataset.py`
- Modify: `tests/test_production_query_dataset.py`

1. Add a failing test that inserts more than 500 newer unrelated evaluation packets after five accepted cases in each channel and asserts the accepted cases remain visible.
2. Confirm RED under the current generic `list_records(... limit=500)` implementation.
3. Replace the generic scan with `list_records_by_meta_value`, filtering exact scope, active status, `kind=evaluation_packet`, and `report_type=production_recall_accepted_case`.
4. Retain strict source/schema/scope validation as defense in depth.
5. Run `pytest -q tests/test_production_query_dataset.py tests/test_production_query_repair.py`.

## Task 4: Wire repair into bootstrap and release lineage

**Files:**
- Modify: `deploy/bootstrap_production_recall.py`
- Modify: `eimemory/governance/release_lineage.py`
- Modify: `tests/test_release_lineage.py`
- Test: `tests/test_production_recall_bootstrap.py` or the existing bootstrap CLI test module located by `rg`

1. Add a failing bootstrap test proving repair runs before collection/build and the report exposes only the bounded repair summary.
2. Call the repair service immediately before `collect_pending_production_queries()` and include its compact result in the bootstrap report. Abort readiness on repair conflicts; do not delete evidence.
3. Add a failing lineage test classifying `deploy/bootstrap_production_recall.py` as `deployment.runtime` with no unknown production path.
4. Add the path to `DOMAIN_PATHS["deployment.runtime"]` and rerun both focused suites.

## Task 5: Verify the data closure patch

1. Run:
   ```bash
   python -m pytest -q \
     tests/test_identity_ops.py \
     tests/test_production_query_dataset.py \
     tests/test_production_query_repair.py \
     tests/test_release_lineage.py
   ```
2. Run the complete test suite with `python -m pytest -q --strict-markers tests`.
3. Run the bootstrap in an isolated temporary database and assert all three channels report at least five accepted cases and `ready=true`.
4. Inspect `git diff --check`, `git status --short`, and the final diff. Commit the data-closure unit only after fresh outputs are green.

