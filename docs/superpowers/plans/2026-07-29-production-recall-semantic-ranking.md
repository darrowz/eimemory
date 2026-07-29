# Production Recall Semantic Ranking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make production recall relevance and stability robust to immutable record-ID drift among semantically identical ground-truth rules without weakening provenance.

**Architecture:** Preserve exact operator-labelled and returned record IDs as audit evidence. Add a digest-only semantic ranking identity for ground-truth behavior rules, use it for ranking metrics and predecessor stability, and keep all other records exact-ID based.

**Tech Stack:** Python 3.14, SQLite-backed Runtime records, pytest, immutable Bash deployment.

## Global Constraints

- Do not delete, retire, merge, or rewrite historical ground-truth records.
- Do not infer or create outcome provenance or missing `source_record_id`.
- Do not expand an exact operator label to records lacking that label evidence.
- Keep scope, source, channel, label-evidence, leakage, latency, memory, release, and baseline gates strict.
- Version the release as `1.9.112`.

---

### Task 1: Lock semantic duplicate behavior with failing tests

**Files:**
- Modify: `tests/test_production_real_query_gate.py`

**Interfaces:**
- Consumes: `run_production_recall_eval(...)`
- Produces: regression coverage for semantic-equivalent and behavior-distinct rules

- [ ] **Step 1: Add the semantic duplicate integration regression**

Create two active `ground_truth_behavior_rule` records with identical behavior
content but distinct `lesson_record_id` and `replay_record_id`. Label the first,
return the second from the rule lane, and make the trusted baseline contain the
first. Assert the report is accepted, exact label/returned IDs differ, ranking
refs match, and MRR/NDCG/top-1/Jaccard are `1.0`.

- [ ] **Step 2: Add the semantic boundary regression**

Change `expected_behavior` on the returned rule and assert it receives a
different ranking ref and the gate remains blocked.

- [ ] **Step 3: Run the regressions and capture RED**

Run:

```bash
umask 0077
env -u EIMEMORY_ROOT -u EIMEMORY_RUNTIME_COMMIT \
  .venv/bin/python -m pytest -q \
  tests/test_production_real_query_gate.py \
  -k 'semantic_duplicate or semantic_behavior_change'
```

Expected: the duplicate case fails because exact record IDs differ.

### Task 2: Implement semantic ranking identity

**Files:**
- Modify: `eimemory/evaluation/real_query_gate.py`

**Interfaces:**
- Produces: `_record_ranking_ref(record: RecordEnvelope) -> str`
- Produces report fields: `ranking_identity_schema`,
  `label_ranking_refs`, `returned_ranking_refs`,
  `baseline_ranking_refs`, `ranking_result_refs`,
  `ranking_result_digest`

- [ ] **Step 1: Add the minimal identity projection**

Return exact `record_id` for ordinary records. For
`ground_truth_behavior_rule`, hash:

```python
{
    "schema": "ground_truth_behavior_semantic.v1",
    "kind": record.kind,
    "source": record.source,
    "source_id": record.source_id,
    "title": record.title,
    "summary": record.summary,
    "detail": record.detail,
    "content": {
        key: value
        for key, value in record.content.items()
        if key not in {"lesson_record_id", "replay_record_id"}
    },
}
```

- [ ] **Step 2: Use ranking refs for metrics**

Resolve label, returned, and predecessor refs through the same helper, stable
deduplicate them, and pass only those refs to
`evaluate_labeled_ranking_at_5`. Keep exact IDs in the existing report fields.

- [ ] **Step 3: Persist ranking evidence**

Add the ranking schema, per-sample ranking refs, report-level ranking result
refs, and stable digest. Extend independent validation to reject malformed or
internally inconsistent ranking evidence while retaining compatibility with
historical exact-ID baseline reports.

- [ ] **Step 4: Run GREEN**

Run the Task 1 command and expect both regressions to pass.

### Task 3: Verify focused and full behavior

**Files:** no new source files.

- [ ] **Step 1: Run focused gate suites**

```bash
umask 0077
env -u EIMEMORY_ROOT -u EIMEMORY_RUNTIME_COMMIT \
  .venv/bin/python -m pytest -q \
  tests/test_production_real_query_gate.py \
  tests/test_production_query_dataset.py \
  tests/test_real_replay_gate.py \
  tests/test_l5_readiness.py \
  tests/test_release_closure.py
```

- [ ] **Step 2: Run the full suite**

```bash
umask 0077
env -u EIMEMORY_ROOT -u EIMEMORY_RUNTIME_COMMIT \
  .venv/bin/python -m pytest -q
```

Expected: zero failures. If runtime exceeds the 20-minute execution threshold,
record the exact completed count and continue only with an explicitly detached
run plus health/log monitoring.

### Task 4: Release metadata and changelog

**Files:**
- Modify: `pyproject.toml`
- Modify: `eimemory/version.py`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Bump version**

Set both version declarations to `1.9.112`.

- [ ] **Step 2: Add changelog entry**

Document semantic ground-truth ranking identity, exact-ID audit preservation,
and the fail-closed verified-real evidence deficit.

- [ ] **Step 3: Verify release metadata**

```bash
.venv/bin/python -m pytest -q tests/test_version.py
```

### Task 5: Commit, review, push, and immutable deploy

**Files:** all files above.

- [ ] **Step 1: Review**

Inspect `git diff --check`, the full diff, and focused test evidence. Confirm
known fixable issues and verification gaps are zero before commit.

- [ ] **Step 2: Commit and push**

Commit the reviewed change, push the branch, fast-forward canonical `master`
only if still based on `origin/master`, then push `master`.

- [ ] **Step 3: Deploy**

Run:

```bash
/dev-project/eimemory/deploy/install_immutable_release.sh <full-commit>
```

Verify the installer receipt and current release identity before live closure.

### Task 6: Live closure and report

**Files:**
- Create outside the repository:
  `/home/darrow/.openclaw/workspace/tmp/l5-full-repair-2026-07-29-report.md`

- [ ] **Step 1: Verify production identity and health**

Check `/opt/eimemory/current`, `eimemory status --json`, and `/health`.

- [ ] **Step 2: Run live gates**

Run `learn l5-assess`, `learn capability-replay --persist`,
`learn release-closure`, and `learn l5-readiness` for scope
`hongtu/embodied/darrow`.

- [ ] **Step 3: Audit verified-real sources**

Call `validate_real_replay_source` on existing immutable outcome traces. Build
and persist a current-code replay only when at least one valid exact source is
available, and never fill missing IDs by inference. Keep the 10/5 gate visible
when the valid-source count is insufficient.

- [ ] **Step 4: Write final report**

Record root cause, code changes, red/green evidence, version/commit, push and
deploy receipts, live metrics, exact verified-real deficits, and
`known_fixable_issues` / `verification_gaps`.
