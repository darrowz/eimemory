# ADR-0002: Multi-axis L5 assessment

**Status:** accepted

**Date:** 2026-08-20

## Context

The existing L5 readiness model combines cognitive maturity, replay coverage,
adapter behavior, release identity, and deployment health into one fixed
readiness path. A healthy service can then appear to advance cognition, and a
new version/machine can incorrectly reset or gate portable evidence.

## Decision

Represent L5 with four independent axes:

1. loop maturity;
2. capability readiness by capability revision and provider binding;
3. adapter readiness;
4. deployment assurance.

The API stores and returns all axes. Any aggregate is profile-specific and
cannot hide individual missing, blocked, stale, or regressed states.

## Alternatives considered

- One global score: rejected because it hides the reason a system is not ready.
- Separate L5 instance per machine/version: rejected because it turns
  operational context into cognitive identity.
- Drop deployment evidence: rejected because deploy-dependent claims still need
  strict commit/receipt/session binding.

## Consequences

Deployment assurance remains strict while portable cognitive evidence can survive
an explicitly compatible implementation transition. L5 claims remain
evidence-bound and are never inferred from health alone.

