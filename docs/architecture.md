# eimemory Architecture

`eimemory` is split into four memory-system layers:

1. Core models and IDs
2. Local storage and query surfaces
3. Runtime APIs for memory and evolution
4. Integration adapters for OpenClaw and eibrain

The runtime is local-first and persists records into JSONL plus SQLite.

## Quality Layer

Memory records can include `meta.quality` with:

- `importance`
- `confidence`
- `freshness`
- `reuse_potential`
- `salience_score`
- `quality_tier`
- `capture_decision`

The tier model is intentionally small:

- `rejected`: unsafe, too thin, or not useful enough for long-term recall
- `candidate`: possible memory that should be kept low-impact
- `confirmed`: reusable memory with normal recall weight
- `core`: durable high-value memory that should be favored

The ingest path can reject low-quality memories before persistence, while legacy
or migrated rejected records are filtered out by search and graph expansion.
Within a scoped tenant/agent/workspace, `user_id=""` is treated as shared global
memory: a user can recall their own records plus global records, but not another
user's records.

## Quality-Aware Recall

Hybrid recall combines lexical matching, semantic/vector matching, graph
expansion, and quality weighting. Quality does not replace relevance; it adjusts
ranking so high-salience confirmed/core memories are more likely to survive
truncation, while rejected records are excluded.

Recall bundles expose a `quality_summary` and per-item `scoring` data for
operator auditability.

## Evolution Reports

`EvolutionAPI.memory_quality_report()` summarizes memory health without mutating
records. The CLI exposes it as:

```bash
eimemory quality stats
```

Nightly reports include a `memory_quality` section with quality tier
distribution, average salience, source counts, and memory type counts.

## OpenClaw Hygiene

The OpenClaw adapter treats inbound chat and lifecycle events as untrusted memory
candidates. It filters low-value chatter, wrapper-only content, prompt-injection
shapes, malformed hook output, empty cleaned responses, and model thinking traces
before long-term persistence or prompt injection. Explicit
`capture_memory=true` or `captureMemory=true` remains available for deliberate
operator capture.
