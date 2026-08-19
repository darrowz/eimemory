# ADR-0005: Knowledge-capability feedback

**Status:** accepted

**Date:** 2026-08-20

## Context

eimemory can ingest evidence-rich knowledge, but knowledge accumulation alone
does not prove an agent can perform a capability better. Contradicted, stale, or
unverified sources must not silently influence active behavior.

## Decision

Link reviewed knowledge to a capability revision through typed relationships:
`supports`, `refutes`, `informs_eval`, `informs_change`, `explains_outcome`, and
`limits_applicability`. A knowledge link can create a hypothesis, which must
produce an eval/replay or bounded candidate. Only verified results and, where
required, real outcomes feed capability state. Results also update the
knowledge-link applicability state.

## Alternatives considered

- Increase maturity when knowledge is ingested: rejected because text volume is
  not demonstrated capability.
- Feed retrieved text directly to automatic code apply: rejected because source
  provenance, contradiction, evaluation, and safety gates would be bypassed.
- Delete failed hypotheses: rejected because negative evidence is useful for
  future applicability and audit.

## Consequences

The architecture gains a measurable knowledge-to-improvement loop. Stale,
rejected, contradicted, or artifact-invalid knowledge fails closed and cannot
self-promote.

