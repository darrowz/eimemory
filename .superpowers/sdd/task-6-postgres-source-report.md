# Task 6 - Optional PostgreSQL vector candidate source

## Outcome

Implemented an optional PostgreSQL/pgvector candidate accelerator while keeping
SQLite as the only authority and an unconditional participant. PostgreSQL only
returns bounded `CandidateRef` projections; `GovernedRecallEngine` rehydrates
every PostgreSQL-only reference from SQLite before any result can be returned.
The feature remains disabled and dependency-free by default.

## Closed contracts

- Lazy optional `psycopg` dependency; import and SQLite-only operation do not
  require PostgreSQL packages.
- Explicit bounded configuration for connection/statement timeouts, pool and
  queue size, vector dimension, candidate count, cache, projection text,
  embedding payload/response sizes, sync pages, and sync lease.
- OpenAI-compatible `/embeddings` provider only. It makes no chat-model or
  MiniMax-M3 assumption, rejects remote plaintext HTTP and redirects, enforces
  a bounded total response deadline, and exposes a credential-free stable
  fingerprint. Custom providers require an explicit SHA-256 fingerprint.
- PostgreSQL candidate search uses prepared values, exact tenant/agent/workspace/
  user/source/kind/status/watermark predicates, bounded returned text, pgvector
  `>=0.8.0`, filtered HNSW iterative scan, a scope index, GIN, HNSW, and a
  collision-resistant identifier scheme.
- `candidate-projection.v1` is a bounded projection digest, not a payload digest.
  Sync and governed SQLite rehydration call the same canonical digest function,
  including canonical UTC timestamps and the configured text bound.
- A stale PostgreSQL-only projection is dropped and counted. A stale PostgreSQL
  duplicate never vetoes the matching SQLite hit: its vector contribution is
  stripped and the mismatch is counted. Private projection hints never enter
  public scoring evidence or diagnostics.
- Cache keys hash the query and bind scope, sources, kinds, limits, policy,
  release, committed watermark, SQLite authority cursor, and authority revision.
- Versioned v2 DDL validates dimension, composite primary key, exact indexes,
  required columns, pgvector version, and migration marker. The earlier
  unshipped v1 candidate-only schema is conditionally rebuilt without touching
  SQLite or authoritative payload data.
- Sync uses bounded keyset pages, a persistent SQLite mutation revision, a
  PostgreSQL worker lease, cursor CAS, separate staging/committed fingerprints,
  abandoned-watermark garbage collection, and atomic final watermark switch.
  Provider/DB/mutation failures do not advance the committed watermark.
- Health and CLI status are allowlisted, secret-safe, strict-JSON-safe, and bind
  availability to provider circuit, committed fingerprints, authority revision,
  authority cursor, and lag. Every optional failure visibly bypasses to SQLite.

## Focused verification

Fresh results from this worktree:

```text
python -m pytest tests/test_postgres_vector_source.py tests/test_postgres_vector_sync.py tests/test_postgres_vector_cli.py -q
68 passed

python -m pytest tests/test_postgres_vector_source.py tests/test_postgres_vector_sync.py tests/test_postgres_vector_cli.py tests/test_recall_fusion.py tests/test_recall_engine.py tests/test_candidate_search_v2.py tests/test_source_partition.py tests/test_eibrain_rpc_contract.py -q
209 passed

python -m compileall -q eimemory/retrieval eimemory/adapters/eibrain
passed

git diff --check
passed (line-ending conversion warnings only)
```

The liveness probe uses four rows, batch size two, and ten allowed pages while
mutating the SQLite authority on every embedding batch. It fails on the first
revision CAS with `authority_changed_during_sync`, performs zero PostgreSQL page
applies, advances no cursor/watermark, releases its lease exactly once, and does
not loop. A separate two-run regression proves that a same-timestamp earlier key
causes a new run with an empty cursor and is captured first.

No full suite was run, in accordance with the parent task's focused-verification
constraint. No live PostgreSQL DSN was available, so PostgreSQL behavior is
covered by prepared-SQL/transaction fakes and SQL-shape validation rather than a
live server integration test.

## Operational boundary

The persistent global authority revision intentionally favors correctness over
availability: a write during full-projection sync aborts the run. A continuously
busy, very large SQLite authority therefore needs a quiet sync window; until a
complete run exists or while revisions differ, PostgreSQL remains bypassed and
SQLite recall continues normally. An incremental dirty-journal/tombstone tailer
is the appropriate later optimization, not a reason to weaken this fail-closed
first backend.

## Scope exclusions

- No authoritative memory was moved to PostgreSQL.
- No second long-term-memory authority or dual-write path was introduced.
- No version bump, release, deployment, or full-suite run was performed.
