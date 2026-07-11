# Knowledge Capability Safety Gate Design

## Goal

Turn external knowledge intake into a reversible, testable capability funnel. Unsafe or low-trust external content must not become active knowledge, default recall evidence, or activatable skill behavior without explicit safety and observation evidence.

## Scope

This release closes the P0 gap around knowledge-to-capability conversion:

- screen direct `knowledge_unit` ingestion for prompt injection, secrets, missing provenance, and low source trust;
- exclude quarantined or low-trust external knowledge from default recall;
- require the same knowledge safety gate before a `skill_candidate` can pass sandbox validation;
- expose deterministic reports so the behavior is easy to replay in tests and production diagnostics.

## Architecture

Add one small shared gate module under `eimemory.knowledge.safety`. The gate reads record/content/meta/provenance payloads and returns a plain dict report with `ok`, `status`, `capability_allowed`, `recall_allowed`, `source_trust`, `trust_tier`, and `reasons`.

Callers stay simple:

- `knowledge.ingest` uses the gate before persisting units and writes unsafe units as `quarantined` with redacted content.
- `api.memory` uses the gate inside the existing online recall pollution gate.
- `skill_validation` uses the gate in sandbox checks, so low-trust or quarantined knowledge-derived candidates cannot move to canary.

## Verification

The regression suite must prove:

- direct prompt-injection knowledge ingest is quarantined and redacted;
- low-trust/missing-source knowledge is not default recalled;
- low-trust external knowledge cannot validate a skill candidate to canary;
- trusted official/API docs still flow through as normal candidates.
