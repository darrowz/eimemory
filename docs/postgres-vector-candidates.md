# Optional Postgres vector candidates

Postgres is an optional retrieval accelerator, never an authority. The
supported composition is `SQLiteCandidateSource` plus
`PostgresVectorCandidateSource`; every Postgres hit is only an exact
`CandidateRef` and is rehydrated from SQLite by `GovernedRecallEngine` before
status, scope, source, quality, RRF, page-pool, and create-safety gates run.
Any Postgres, embedding, timeout, lag, dimension, or circuit failure is an
observable bypass while SQLite recall continues.

The default install remains dependency-free and Postgres is disabled. Install
the optional driver only where the projection is wanted:

```text
pip install 'eimemory[postgres]'
```

Configuration is explicit and secret-bearing values are never included in
health or CLI output:

```text
EIMEMORY_POSTGRES_VECTOR_ENABLED=1
EIMEMORY_POSTGRES_VECTOR_DSN=postgresql://...
EIMEMORY_POSTGRES_VECTOR_DIMENSION=1536
EIMEMORY_POSTGRES_VECTOR_SCHEMA=eimemory_recall
EIMEMORY_POSTGRES_VECTOR_TABLE=vector_candidates
EIMEMORY_POSTGRES_CONNECT_TIMEOUT_SECONDS=2
EIMEMORY_POSTGRES_STATEMENT_TIMEOUT_MS=1500
EIMEMORY_POSTGRES_POOL_SIZE=2
EIMEMORY_POSTGRES_QUEUE_BOUND=16
EIMEMORY_POSTGRES_MAX_INDEX_LAG_SECONDS=300
EIMEMORY_EMBEDDINGS_BASE_URL=https://provider.example/v1
EIMEMORY_EMBEDDINGS_API_KEY=...
EIMEMORY_EMBEDDINGS_MODEL=provider-embedding-model
```

`EIMEMORY_EMBEDDINGS_MODEL` must name a real embeddings model. A chat model,
including MiniMax-M3, is not assumed to support `/embeddings`; chat or LLM
output is never written into the candidate projection or authoritative memory.

The lifecycle is deliberately operator-driven:

```text
eimemory vector-index status
eimemory vector-index migrate
eimemory vector-index sync --batch-size 32 --max-pages 10
```

Sync keyset-pages SQLite by `(updated_at, storage_key)`, embeds bounded batches,
and transactionally upserts only title/alias/keyword projection data, the exact
scope/source/status identity, digest, vector, and run watermark. It does not
store the authoritative payload. A failed provider or Postgres transaction
does not advance the cursor or committed watermark. The final page removes
rows not observed in the completed run. Re-running `sync` resumes the same run
and is idempotent.

To inject the optional source programmatically, use
`build_postgres_vector_candidate_source(store, config)`. This factory always
installs SQLite as the participating authority. Production remains SQLite-only
until an operator explicitly supplies and injects the enabled configuration;
there is no autonomous background sync.
