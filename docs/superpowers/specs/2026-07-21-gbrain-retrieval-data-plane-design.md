# GBrain-Inspired Retrieval Data Plane Design

## Goal

Keep eimemory as the only authoritative memory and governance control plane,
while adopting the useful retrieval-data-plane patterns identified in GBrain:
pluggable candidate retrieval, explainable rank fusion, proactive context,
realistic retrieval evaluation, and an explicit source axis inside each
channel.

This release preserves the current per-channel authority model and the L5
evidence, replay, promotion, rollback, and deployment contracts. It does not
introduce GBrain or Postgres as a second truth source.

## Authority and Boundaries

- OpenClaw remains the compatibility authority for the current production
  integration. Codex and Hermes retain independent authoritative channel
  scopes.
- `channel` is the hard authority and leakage boundary. `source_id` is a
  partition inside that boundary and never grants cross-channel access.
- SQLite remains the zero-dependency default and authoritative record store.
  Postgres is an optional recall candidate index only; it cannot create,
  promote, mutate, or delete authoritative records.
- A failed, slow, unconfigured, stale, or inconsistent optional backend is
  bypassed with bounded diagnostics. SQLite continues without weakening
  source, channel, safety, or policy filters.
- LLMs are not required for correctness. This design does not put an LLM on
  the authoritative path.

## Unified Recall Contracts

Use two non-overlapping contracts so a physical backend cannot bypass final
governance or create a second ranking path.

`CandidateSource` accepts a normalized query, already-derived channel scope,
optional source allowlist, and a bounded budget. It returns candidate record
IDs, source-local ranks/scores, and evidence hints. It never returns
authoritative content and cannot apply final graph, policy, create-safety, or
mutation semantics. SQLite FTS/local similarity and optional Postgres vector
search implement this contract.

`RecallEngine` is the only final orchestrator used by `MemoryAPI.recall`. The
default `GovernedRecallEngine` owns policy/query-scope derivation, candidate
collection, authoritative SQLite rehydration, channel/source/status recheck,
graph expansion, view and pollution gates, usage feedback, RRF, page
max-pooling, and final bundle construction. `MemoryAPI` remains a facade;
neither `RuntimeStore` nor a candidate source performs a second final sort.

The request separates `target_source_id` from the optional search
`source_ids`. The target is required when create-safety should make an identity
claim; an omitted target can return retrieval evidence but only
`create_safety=unknown`.

The final result and diagnostics are stable contracts:

```text
RecallResult
  record
  score
  rank
  evidence[]
  create_safety
  page_key
  component_scores

RecallDiagnostics
  engine
  candidate_sources[]
  fallback_used
  fallback_reason
  candidate_count
  elapsed_ms
  policy_version
```

`SQLiteCandidateSource` wraps the current SQLite/FTS/local-similarity path and
is always available. `PostgresVectorCandidateSource` uses bounded SQL,
statement timeouts, circuit breaking, and identical channel/source predicates.
It returns IDs only. The final engine drops missing, inactive, stale,
cross-scope, cross-source, and policy-blocked IDs. A Postgres error bypasses
only that source; SQLite candidates and the final engine remain active.

## Explainable Recall Fusion

Use deterministic reciprocal rank fusion (RRF) across bounded lists:

- exact normalized title
- normalized alias hit
- exact keyword / FTS
- local or optional Postgres vector similarity
- graph path expansion
- existing recency, living-quality, and policy signals

Each component contributes `1 / (k + rank)` with a versioned configurable `k`
and bounded weight. Stable record ID is the final tie-breaker. Existing quality
and safety filters remain hard gates, not rank bonuses.

Aliases come only from the top-level versioned `aliases` field on supported
records. They use the same NFKC/case-fold/exact normalization as titles. Legacy
model-specific aliases are projected through an explicit per-kind mapping;
conflicting exact aliases make identity ambiguous and cannot yield `exists`.

Results expose a non-empty subset of:

```text
alias_hit | exact_title | keyword_exact | vector_match | graph_path
```

`create_safety` means:

- `exists`: a unique exact-title or alias match inside the requested channel
  and explicit `target_source_id` proves the item already exists.
- `probable`: a strong keyword, vector, graph, or ambiguous identity match
  suggests an existing item but is not proof.
- `unknown`: no identity-strength evidence exists or no target source was
  supplied.

Per-page max-pooling prevents one long document from occupying top K. The page
key includes channel scope and source namespace plus the first stable value
among page ID, parent record ID, source-local document ID, raw
`session_id/source_event_id`, and record ID. Only the best chunk represents the
page; diagnostics retain contributing evidence.

## Explicit Source Axis

Add normalized top-level `source_id` to `RecordEnvelope`, the SQLite `records`
table, `recall_index`, recall requests, mutations, and evaluation fixtures.

- Existing records default to `default`. There is no generic legacy-hint copy.
  Mappings are versioned by kind and field. Initially, a knowledge-page
  `source_ids` value maps only when exactly one normalized value exists;
  bridge/source-registry IDs remain provenance. Ambiguous/conflicting values
  remain `default` and increment bounded migration diagnostics.
- Values are UTF-8 NFKC-normalized, case-folded, restricted to a documented
  slug alphabet, and limited to 128 characters. Invalid input is rejected;
  normalization collisions are reported and never silently merged.
- `records.source_id` and `recall_index.source_id` are added/backfilled in one
  versioned schema migration/transaction. Schema-ready detection includes the
  migration ID and covering channel/source indexes.
- Omitted search filters mean all sources in the already-derived channel.
  Supplied filters use an indexed allowlist predicate.
- Writes, replace/remove, replay, snapshots, provenance, caches, and evaluation
  preserve the field. No mutation may move a record across channel or source.

## Proactive Context Closure

Add one channel-neutral proactive recall service used by OpenClaw, Codex, and
Hermes:

```text
latest 4 bounded turns -> deterministic entity/intent signals -> recall
                       -> confidence >= 0.70 -> max 3 items
                       -> remove already injected session items
                       -> volunteered -> injected -> used/rejected
```

The deterministic path works without an LLM. Confidence combines intent
strength, retrieval evidence, fused rank, and record quality. Context is
bounded, prompt-safe, channel/source local, and advisory.

Session state stores hashes/record IDs rather than transcripts. Cache keys
include channel scope, source filters, query digest, retrieval policy, and
release identity; initialization/scope change invalidates them. Existing
memory-usage feedback is extended so replay can compare proactive delivery with
a no-volunteer control.

## Real-Query Retrieval Gate

Extend the existing production recall evaluator. An eligible case has
`provenance=production_redacted_v1`, a stable case ID, collection window,
channel/source scope, at least one human/accepted relevance label, and no raw
conversation text. Generated records, smoke queries, synthetic probes, and
unlabelled “any result is a hit” cases are diagnostic only and make the release
gate `not_run`. A versioned minimum sample size and per-channel coverage apply.
The immutable dataset digest is computed before both baseline and candidate
runs.

The release report contains:

- Recall@5, Precision@5, MRR, and nDCG@5
- Top-1 stability and Jaccard@K against the immutable baseline
- p50 and p95 latency and peak traced memory
- proactive volunteered, injected, used, and rejected rates
- OpenClaw/Codex/Hermes cross-channel leakage count equal to zero
- source-filter leakage count equal to zero

Recall@5 divides relevant labels returned by all relevant labels. Precision@5
divides relevant returned by five, except a corpus with fewer eligible records
uses that bounded returned count. MRR uses the first relevant rank; nDCG@5 uses
graded accepted relevance. Each metric has versioned absolute and
non-regression thresholds; leakage is an unconditional zero gate.

Query/report persistence stores only case IDs, digests, labels, metrics, and
bounded redacted features, never raw queries or returned text. The result binds
release version/commit, deployment receipt, dataset/baseline/policy/result
digests, and thresholds. `release_closure` and `l5_readiness` independently
resolve and verify this exact current-release record. Synthetic diagnostics
cannot substitute for it.

## Existing P0 Integrity Preconditions

These adjacent defects are release blockers because they would invalidate
authority/L5 while the data plane changes:

1. New Codex/Hermes real-task success requires at least one v2 HMAC receipt
   issued before the terminal event by `adapter.attest_tool_result`. This RPC
   uses a separate least-privilege attestation credential mapped server-side to
   exactly one producer/channel; the normal recall/MCP token cannot call it.
   Codex `PostToolUse` and a Hermes `post_tool_call` plugin hook are the only
   producers. Their credential is loaded from a dedicated 0600 file into the
   hook and is not exposed to tool subprocesses or model-callable tools.
2. The producer submits bounded host event fields and the actual bounded tool
   result to the attestation endpoint. The runtime, not the producer, computes
   the result digest and `passed`/`verification_policy_id`. Generic successful
   tool use yields an execution receipt only. A verification receipt requires
   a server policy such as parsed test-command exit zero with positive test
   count, a trusted structured check, or eimemory-owned deployment health;
   `echo`, arbitrary text, and caller-selected `passed=true` cannot qualify.
   The runtime stores the receipt and returns only its ID.
3. Signed fields are receipt/version/attestation/key IDs, source, channel,
   session/run/tool-call/tool names, result digest, verification policy,
   passed, issued-at, release version/commit, deployment receipt ID, and
   retrieval-policy digest. A `tool_call_id` can be registered once. A terminal
   event can reference only an existing same-channel/session/run verification
   receipt ID; terminal submission cannot mint or inline-sign a receipt. The
   evaluator verifies signature, method/source, current release/receipt,
   expiry, policy eligibility, and one-terminal-trace consumption.
4. Receipt key rotation uses signed `key_id`: the secure key file contains one
   active signing key and a bounded set of verification-only previous keys. V1
   remains readable for prior OpenClaw compatibility evidence but cannot verify
   a new Codex/Hermes task.
5. Terminal text is structurally redacted before persistence. Signed receipt
   fields are allowlisted and preserved; arbitrary nested fields are dropped.
6. Adapter memories and outcome traces use deterministic IDs. Terminal event,
   outcome, and trace persistence is one SQLite transaction, or a persisted
   resumable saga with equivalent race/fault-injection evidence.
7. Hermes authoritative add/replace/remove uses a shared mutation RPC carrying
   target record ID, expected revision, source ID, idempotency key, and allowed
   provenance. Replace supersedes and inactivates the old record; remove writes
   a tombstone and inactivates it. Cross-channel/source, stale revision,
   missing target, and duplicate requests fail closed.

## Performance and Failure Safety

- Bound candidates, graph expansion, results, turn window, diagnostics, caches,
  optional queues, and evaluation cases.
- New scans use keyset pagination; no new OFFSET or full-table materialization.
- Keep SQLite WAL, prepared predicates, covering scope/source indexes, and short
  transactions. Preload only bounded indexes/recent metadata, never bodies.
- Optional Postgres config failure, timeout, circuit state, stale/partial IDs,
  and empty-result policy are visible in health/status. Empty is valid only
  after a successful bounded query; stale/partial IDs are dropped and audited.
- Every bypass is observable and never becomes an implicit success signal.

## Verification and Release Contract

Advance the patch version only after:

1. Receipt, redaction, concurrency, atomic retry, and Hermes mutations close.
2. Source migration/CRUD/recall/replay/snapshot and isolation tests pass.
3. The final engine and candidate sources cannot bypass hard gates.
4. RRF/title/alias/graph/page/evidence/create-safety are replayable.
5. Proactive four-turn/threshold/cap/dedupe/usage feedback works across channels.
6. The eligible real-query gate binds all metrics and zero leakage to the
   current receipt and L5.
7. Focused layers, one full suite, performance bounds, refreshed code graph,
   and complete third-party review pass with all P0/P1 triaged.
8. The branch is reviewed, merged to `master`, pushed, installed as an
   immutable honxin release, and health, services, deployment receipt,
   real-query gate, and L5 agree on the same version and commit.
