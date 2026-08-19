# ADR-0003: Storage v2 authority and projections

**Status:** accepted

**Date:** 2026-08-20

## Context

Current capability and L5 state relies heavily on generic record JSON and
read-time scans. Dynamic capability/eval relationships need typed query paths,
transactional state, incremental projections, restartable backfill, and audit
evidence without introducing a second uncontrolled store.

## Decision

Keep eimemory local-first and assign one authority per data class:

- immutable source/eval bodies: content-addressed artifact store;
- capability observations: append-only durable record/event ledger, with
  immutable evidence references; SQLite `capability_observations` rows are
  idempotent, rebuildable query indexes of those events, never independently
  mutable facts;
- capability definitions, revisions, relations, profiles, bindings, adapter
  capability advertisements, evaluation specifications, and `EvaluationRun`
  lifecycle rows: normalized SQLite v2 domain-table authority with the existing
  operation journal/outbox pattern;
- immutable ledger entries linked to an `EvaluationRun`: audit/evidence events
  only. They cannot create or independently mutate a second run lifecycle;
- capability-state snapshots and L5 assessments: versioned, reproducible
  projections derived from the authoritative definitions and immutable events;
- PostgreSQL/pgvector: optional rebuildable read projection only.

In particular, no component may write a second mutable observation source or a
second run lifecycle source. Observation events are first recorded in the
ledger and indexed by event identity. Evaluation runs are first created and
transitioned transactionally in SQLite; linked ledger events preserve their
immutable audit trail. JSON exports, dashboards, and PostgreSQL projections are
read models rather than authorities.

Use expand-contract migration: schema first, idempotent dual write, bounded
backfill, shadow comparison, reversible reader cutover, and later deletion.

## Alternatives considered

- Move all authority to PostgreSQL: rejected because it weakens local-first
  operation and adds a mandatory network dependency.
- Keep all query-critical fields in JSON: rejected because it cannot provide
  predictable indexing or scalable incremental L5 projection.
- Replace the existing ledger: rejected because historical evidence and recovery
  need continuity.

## Consequences

Storage work is a first-class architectural phase with measured lock, WAL,
throughput, parity, and recovery evidence. Deployed migrations are immutable;
corrections are new forward migrations.
