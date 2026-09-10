# L1 capture extraction idempotency

Date: 2026-09-10. Local offline repair only. No production writes, production backfill, credential access, commit, push, or deployment. No other models or workers were invoked. Changes are confined to `eimemory/knowledge/l1_pipeline.py`, `tests/test_l1_capture_idempotency.py`, and this audit. Pre-existing project/event-time work in the pipeline was preserved.

## Cause and repair

`RuntimeStore.append` returns the existing row for authoritative adapter captures whose source ends in `.memory` and whose idempotency key starts with `adapter.`. Appending a modified envelope therefore did not persist `l1_extracted_at`. Separately, atom ingestion omitted `record_id`, so retrying after a partial extraction created another record for a successful atom.

Extraction now reads the authoritative parent through `get_by_exact_ref`, requiring exact scope/source ownership and matching source. Stale caller envelopes cannot bypass the persisted completion marker or replace source text. Completion rereads the parent inside `mutate_records_atomically`, checks that source material and event time still match, updates only completion metadata and update time, and calls `upsert(commit=False)`. This commits record state and durable export outbox together. Existing completion markers are never reset.

Atom identities hash a versioned tuple of full scope, source partition, episode/session/turn identity, and the original atom fields. They go through the existing `MemoryAPI.ingest(record_id=...)` request-digest contract: matching requests reuse the stored record; conflicting requests fail rather than overwrite it. Different episodes and corrections retain different identities; existing semantic supersession behavior remains intact. Event time is set through a separate exact-reference atomic mutation, so a retry repairs an interruption between ingest and the event-time write. Parent completion happens only after these writes succeed.

The raw parent content, summary, source, source ID, scope, evidence, links, and occurred-at time are preserved. Atom evidence, source-message IDs, and derived-from links retain the original source record. Extraction uses the parent source partition rather than allowing runtime-channel metadata to redirect ownership.

## Red evidence

Command (run before implementation):

```sh
rtk proxy env PYTHONPATH=. /dev-project/eimemory/.venv/bin/python -m pytest -q tests/test_l1_capture_idempotency.py
```

Result: **6 failed, 1 passed**. The deterministic-capture test wrote an atom but reread a parent without `l1_extracted_at`; partial failure/restart returned a different ID for the successful atom. Forged scope/source and source-partition mismatch tests also failed. An earlier fixture run exposed missing `User:`/`Assistant:` transcript framing; the synthetic fixture was corrected before recording the causal red result. No private data was copied into tests.

## Green evidence

Initial seven regression cases: **7 passed**. Final focused command, including transaction interruption coverage:

```sh
rtk proxy env PYTHONPATH=. /dev-project/eimemory/.venv/bin/python -m pytest -q tests/test_l1_capture_idempotency.py tests/test_l1_extract.py tests/test_memory_core_v1_repair.py::test_device_fact_extraction_preserves_source_without_named_user tests/test_memory_core_v1_repair.py::test_captured_image_device_report_extracts_attributed_episode tests/test_tencent_alignment.py::test_l1_edit_and_backfill
```

Result: **20 passed in 3.24s**. Covers persisted and stale retries, completion no-op, two-atom partial ingestion/reopen, failures after SQL writes to event time and completion with rollback/reopen, separate episodes/correction, unauthorized scope/source, shared read visibility versus mutation authority, source links and event time, and existing related L1 behavior. Multi-atom failure testing injects two deterministic synthetic extracted atoms; persistence and restart use the real MemoryAPI and RuntimeStore.

Exact private offline acceptance command:

```sh
PYTHONPATH=/home/darrow/tmp/eimemory-core-v1-worktree /dev-project/eimemory/.venv/bin/python /home/darrow/tmp/eimemory-core-v1/backfill-core.py --report /tmp/core-exact-replay.json
```

Run with stdout/stderr redirected to `/tmp/core-exact-replay.stdout` and `/tmp/core-exact-replay.stderr` to avoid emitting private record payloads. **Exit 0**, `production: false`; `phone_positive`, `project_positive`, `raw_content_unchanged`, and `idempotency` all **true**. One phone atom and one project-support result. The disposable store was cleaned up by the unchanged acceptance script. No `--production` flag was passed. The project association implementation was already present and was not modified.

## Limits

- Atom writes, event-time repair, and parent completion are separate recoverable transactions, not one extraction-wide transaction. An interrupted run can temporarily expose an atom before its event time is repaired; retry finishes it without another ID.
- Stable retry identity assumes the same extracted atom payload. Changed/nondeterministic LLM output can produce a different identity; changed adjudication for an existing identity is rejected by the ingest digest. This work does not freeze LLM output or add extraction state tables.
- The low-level `persist_l1_atoms` retains its existing trusted-caller support for absent parents (and unbound calls without episode/session identity). It checks ownership when a parent is visible; the captured-parent extraction entry point always requires an exact persisted parent. Unbound calls do not gain retry identity.
- No migration/deduplication of historical random-ID atoms, global marker reset, concurrency stress test, full suite, or production verification was performed. Main owns integration and production.
