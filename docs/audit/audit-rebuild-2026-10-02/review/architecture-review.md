# Fresh architecture and storage ownership review

## Executive result

The first independent storage pass confirms a concrete transaction-ownership defect in two mutation boundaries: a nested transaction is rejected by `BEGIN IMMEDIATE`, but the surrounding exception handler then rolls back the caller's already-open transaction. A minimal entry guard is consistent with the existing `append` contract. Batch 001 subsequently passed independent patch review and focused fake-only before/after verification; see `batch-001/README.md` and its sealed verdict. The exact reviewed repair is published as commit d1d571e109c99eab6540fd41f8d167e2502f012a.

This is a fresh review of **1.14.31**, commit **4763001d1c4f3f4af6e6dda17e008e1b4c9b5609**. No previous audit result, fix, or pass count is carried forward. The navigation graph describes all tracked files, but that is not audit coverage. This report reviews **74 function definitions across selected ranges in five storage files**: three small files in full and two large files only in part. It is candidate-informed independent corroboration, not a blind pass, because the source reviewer supplied candidate issues before this inspection.

No project module was imported or executed for this architecture pass. There is no full-suite, production, model, network-service, real SQLite, or PostgreSQL validation claim. Previously excluded security/authentication/evidence/archive probes were not reopened, and the paused command-client startup range was not inspected.

## Dependency map from the fresh graph

The graph contains **551 production Python modules** and **1,807 distinct internal directed import pairs** between them. These are static import relationships, including nested, conditional, and type-checking imports; they are not runtime call edges.

- `api.runtime.Runtime` is the composition root. `create` opens `RuntimeStore`; construction binds memory services and, in the full profile, capability, proactive, evolution, and raw interfaces. The core profile returns before those optional extensions are constructed. This is a source-level observation, not a startup test.
- `api.runtime` imports 100 distinct production modules; `cli.main` imports 48. These are coordination hot spots, so interface changes here have wide static reach.
- `models.records` is imported by 192 production modules. Scope and envelope changes should be reviewed as shared contracts rather than local storage refactors.
- `storage.runtime_store` has 21 direct production importers and 13 outgoing production dependencies. It is the durable-write orchestration boundary shared by API, capabilities, knowledge, intake, retrieval, raw, and maintenance consumers.
- The largest production package groups are governance (247 modules), evaluation (42), adapters (30), retrieval (27), knowledge (22), and storage (22). Counts identify navigation and review scope, not completed review.

The mechanically derived package counts and fan-in/fan-out are in `architecture-graph-summary.json`. The existing graph index remains the source-navigation artifact.

## Transaction and connection ownership

| Boundary | Owner and observed behavior | Review implication |
| --- | --- | --- |
| `RuntimeStore.append` (153–187) | Runtime RLock; rejects an existing transaction before the `try`; owns begin/commit/rollback; projection follows commit | Existing compatibility model for top-level record mutation |
| `mutate_records_atomically` (631–672) | Runtime RLock; callback writes with `commit=False`; record/edge outbox entries commit together; projection follows commit | Nested entry currently rolls back caller state; minimal pre-try rejection is appropriate |
| `mutate_capabilities_atomically` (674–736) | Runtime RLock; transaction-local typed repository; capability audit records and outbox commit with domain changes; flush failure becomes status | Same nested-entry ownership defect; do not move flush before commit |
| `CapabilityStore` (constructor and `_write` spot checks) | Requires an active RuntimeStore transaction; per-write savepoints; typed repository does not own the enclosing commit | Preserve savepoint isolation and transaction-local repository lifetime |
| `read_consistent` (507–528) | Borrows a reader; starts a read transaction only if none exists; rolls back only the transaction it starts | Correct ownership distinction in the inspected source |
| `read_capabilities` (738–754) | Always attempts `BEGIN` before the `try`; ends its own read snapshot | Nested entry fails, but not through the two mutation handlers' destructive rollback path |
| `mutate_code_evolution_atomically` / `read_code_evolution` (756–788) | Participate in an existing transaction; commit/rollback only when they opened it | A different explicit ownership policy; do not normalize blindly to top-level rejection |
| `SqliteRecordStore` guarded wrappers (263–310) | Bound runtime lock is checked by execute/commit/rollback wrappers | Raw connection access and cursor lifetime still need caller discipline; wrapper checks do not cover later cursor fetches |
| Reader pool (414–491) | Lazily constructs SQLite stores, marks them query-only, tracks `in_use`, and holds per-reader locks while borrowed; otherwise falls back to the writer | Reader acquisition, database bootstrap, and lifecycle are part of the effective latency/ownership boundary |
| `flush_exports` (2167–2209) | Appends pending payloads, marks exported without per-row commit, flushes touched logs, then commits export-state changes | Post-commit export is a separate durable phase; failure tests must distinguish committed fact from projection state |

### Authority clarification worth documenting

The architecture document describes general-record SQLite as rebuildable projection, while `append` and the two atomic mutation methods acknowledge the SQLite commit before best-effort JSONL projection. In the current write path, recovery must therefore preserve both SQLite and its pending outbox until export completes. This is an operational-contract clarification, not a newly reproduced data-loss defect. The capability domain already explicitly identifies normalized SQLite as transactional authority.

## Findings and open review questions

### A-STO-001: caller transaction rollback on rejected nested mutation

**Source-confirmed; focused fake-only before/after verification approved in batch 001.** In both atomic mutation methods, the failed `BEGIN IMMEDIATE` is inside the handler that unconditionally calls `rollback`. If the same thread already owns a transaction, the callback need not run for caller work to be rolled back. Rejecting entry before the `try` preserves the caller transaction and matches `append`.

The proposed repair must preserve normal callback execution, commit ordering, rollback of transactions this operation actually starts, and post-commit projection behavior. Focused fake-only tests should prove nested rejection leaves caller state untouched, a nontransaction entry still completes, callback failure still rolls back operation-owned state, and BEGIN failure does not invoke the callback.

### Reader acquisition is outside the search deadline scope

**Source-confirmed placement issue; no timing reproduction performed.** `search` and `search_with_diagnostics` bind the deadline, then enter `borrow_reader`, and only afterward enter `recall_read_scope` with `lock_held=True`. Cold pool construction acquires the writer lock; pool exhaustion and patched-writer paths also acquire it without the search budget. Deadline enforcement therefore cannot bound those earlier waits. A free, initialized pooled reader is a different path and must not be conflated with this finding.

A repair needs a deliberate acquisition contract. Simply timing the per-reader lock would leave lazy construction and writer fallback unbounded. Fake locks/clocks can establish control flow, but actual contention timing remains unverified until separately permitted.

### Cursor and close lifetime questions

**Source risks requiring contract/reproduction before acceptance as repaired defects.** `execute_readonly` returns a live cursor after releasing its reader slot (530–533). No other production or test call to this facade was found by direct text search. `_close_readers` clears the pool and closes each reader without taking its slot lock (436–444), while `close` calls it before acquiring the writer lock (1899–1902). Concurrent borrowers could therefore outlive pool ownership. Runtime's top-level close drains its proactive service, but direct concurrent store clients still need a clear lifecycle contract.

Do not silently replace the returned cursor with a list: cursor fetch sequencing, iteration, metadata, and public compatibility need explicit review. Do not fix close by introducing a lock-order inversion between writer, reader, and pool locks.

### Additional ownership hotspots, not repaired in this batch

- `repair_status_projection_mismatches` (1115–1151) has the same `BEGIN`-inside-try/unconditional-rollback shape and needs a separate focused finding/reproduction
- `rewrite` (1092–1113) commits its underlying writes without an explicit own-transaction guard and propagates projection failure after commit; nesting and acknowledgment semantics need separate contract review
- `update_intent_pattern_row` (586–629) exposes `commit=False`, unlike the unconditional top-level mutation boundaries; its callers must own the transaction when disabling commit

These are fresh source observations, not inherited issue counts or claims of verified fixes.

## Exact review scope and reproducibility

`review-coverage.json` records source hashes, reviewed ranges, all 74 counted functions, and the counting rule (all `def`/`async def` fully contained in inspected ranges, including nested functions; classes excluded). Architecture spot checks outside that manifest are not included in its storage review count.

- `runtime_store.py`: 44–86, 125–187, 379–538, 586–788, 1092–1215, 1899–1902, 2167–2210, 2277–2304; 32 functions
- `sqlite_store.py`: 201–340 and 7916–7917; 11 functions
- `recall_deadline.py`: full 1–122; 14 functions
- `readonly_recall.py`: full 1–55; 3 functions
- `store_access.py`: full 1–107; 14 functions

The metadata was generated with Python standard-library AST, JSON, and SHA-256 operations without importing project code. To reproduce the counts, select `def`/`async def` nodes fully contained in the recorded inclusive ranges; to reproduce graph metrics, deduplicate internal directed source/target module pairs whose two endpoints are production Python modules below `eimemory/`. The source and repair trees are unchanged.

Repository contribution instructions request `pytest tests/`. That aggregate suite was **not run**. Relevant targeted patch evidence will be reported separately against exact before/after hashes, without representing it as suite-wide or production validation.
