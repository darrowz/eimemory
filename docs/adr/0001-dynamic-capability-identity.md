# ADR-0001: Dynamic capability identity

**Status:** accepted

**Date:** 2026-08-20

## Context

L5 currently treats a fixed source-code taxonomy as the capability universe.
Capability identity is entangled with acceptance cases, replay packs, goal
selection, readiness, and release lineage. Adding a capability therefore
requires changing unrelated L5 code, and environment/version facts can leak into
the cognitive model.

## Decision

Use a revisioned `CapabilityDefinition` as the semantic identity and store
capabilities, revisions, relations, bindings, profiles, evals, observations,
and state as data. A capability ID is lower-case dot-separated semantic text.
Provider, implementation, package version, release identity, hostname, model,
and environment are separate entities or context.

## Alternatives considered

- Keep one expanded built-in capability list: rejected because every new
  capability would continue to require core L5 edits.
- Use a host/model name as capability identity: rejected because a provider can
  change without changing the job, and one job can have many providers.
- Let free-form LLM labels define identity: rejected because unvalidated labels
  are not stable or safe keys.

## Consequences

New capabilities require registry data and tests, not L5 source-list edits.
Incompatible revisions start with no inherited maturity; compatible inheritance
must be an explicit policy. Existing fixed lists are migration inputs until
cutover, not future sources of truth.

