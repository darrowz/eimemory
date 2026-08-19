# ADR-0004: Adapter capability negotiation

**Status:** accepted

**Date:** 2026-08-20

## Context

Codex, Hermes, OpenClaw, and eibrain expose different lifecycle and model tool
surfaces. Forcing equal public APIs creates dead wrappers and hides actual host
behavior; deriving capabilities from host names produces false claims.

## Decision

Add an internal, additive adapter protocol for capability advertisements,
normalized outcomes, and binding health. Advertisements declare supported
capability revisions, operations, limits, side-effect class, evidence sources,
and contract digest. Existing public adapter surfaces remain unchanged unless a
separate compatibility contract proves a change.

OpenClaw keeps lifecycle behavior in hooks and its limited bridge status surface;
Codex and Hermes retain their shared model-facing operations; eibrain remains
bounded RPC/SDK based.

## Alternatives considered

- Expose every adapter capability as a model tool: rejected because it expands
  the attack surface and resurrects unused wrapper layers.
- Require tool-surface parity: rejected because hosts have different execution
  semantics.
- Infer capabilities from adapter name: rejected because it is unverifiable.

## Consequences

L5 can report adapter readiness honestly without requiring identical model
interfaces. Unsupported host events remain unsupported/unclassified rather than
being guessed into a capability.

