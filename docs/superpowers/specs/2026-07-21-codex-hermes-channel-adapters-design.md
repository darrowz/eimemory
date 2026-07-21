# Codex and Hermes Channel Adapter Design

## Goal

Add first-class Codex and Hermes adapters without weakening the existing
OpenClaw integration. OpenClaw, Codex, and Hermes each own an independent
memory namespace. A memory accepted by the common eimemory quality and safety
gates is authoritative long-term memory inside its originating channel; no
channel waits for another channel to approve it.

## Authority and Isolation

The authority model is `per_channel`.

- OpenClaw remains the default production entry point and the compatibility
  reference for terminal evidence.
- Codex and Hermes write directly into their own channel scopes after the same
  capture-quality and safety checks used by eimemory.
- Recall is channel-local by default. There is no automatic cross-channel
  search, copying, merge, or conflict resolution in this release.
- A record carries `runtime_channel`, `authority_mode=per_channel`, and
  `authoritative=true` only when it was persisted with active status.
- Existing OpenClaw scopes and records are not migrated. Codex and Hermes use
  deterministic workspace suffixes, so the current SQLite schema and indexes
  remain valid.

For a base workspace `embodied`, the new scopes are:

```text
OpenClaw: embodied
Codex:    embodied::channel::codex
Hermes:   embodied::channel::hermes
```

## Shared Runtime Contract

The authenticated EIBrain loopback RPC remains the only data-plane service.
It gains an additive `agent.runtime.v1` contract with these methods:

- `adapter.prefetch`: channel-local recall plus bounded prompt context.
- `adapter.sync_turn`: idempotent, quality-gated turn memory ingestion.
- `adapter.remember`: explicit durable memory ingestion.
- `adapter.record_terminal`: event, outcome, and outcome-trace persistence.
- `adapter.status`: contract, channel scope, release, and health identity.

The server derives the channel scope. Host plugins cannot accidentally omit
the channel boundary. Request bodies remain subject to the existing one
megabyte limit and bearer-token authentication.

## Codex Adapter

The repository ships a standard Codex plugin under
`integrations/codex/eimemory/`.

- `SessionStart` checks adapter availability without loading a transcript.
- `UserPromptSubmit` calls `adapter.prefetch` and returns bounded additional
  context.
- `PostToolUse` records a bounded tool receipt for explicit verification.
- `Stop` records a terminal task. A claimed success without explicit
  verification remains unverified and cannot count as a real-task L5 sample.
- The bundled MCP server exposes recall, remember, terminal verification, and
  status tools for explicit agent use.

Hook and MCP failures are fail-open for Codex: the current turn continues with
no injected context, while a bounded local failure ledger records the bypass.

## Hermes Adapter

The repository ships a standalone Hermes memory-provider plugin under
`integrations/hermes/eimemory/`. It implements Hermes' `MemoryProvider` ABC
without modifying Hermes core files.

- `prefetch()` performs channel-local recall.
- `sync_turn()` submits an idempotent, quality-gated turn memory.
- `on_memory_write()` mirrors explicit memory writes.
- `on_pre_compress()` persists a bounded pre-compression memory when supplied.
- `on_session_end()` records lifecycle completion without inventing task
  verification.
- Provider tools expose recall, remember, verified terminal outcome, and
  status.

Hermes supports one external memory provider at a time. Selecting
`memory.provider: eimemory` therefore replaces another external provider, but
the optional built-in write mirror stays configurable.

## Terminal Evidence and L5

The outcome evidence validator is generalized from an OpenClaw-only method
allowlist to a runtime-terminal contract. Accepted real-task methods are:

```text
openclaw.agent_end
openclaw.task_end
codex.stop
hermes.task_end
```

Every accepted real-task outcome must have a specific task type, one matching
terminal event, an explicit boolean success result, an explicit verifier, a
matching persisted event outcome, a current release identity, and any claimed
tool receipt must pass signature verification. Session-only events remain
lifecycle evidence and never inflate L5.

L5 readiness is evaluated per channel scope. Counts from one channel cannot
satisfy another channel's gate.

## Performance and Failure Safety

- Reuse the existing long-running RPC service and SQLite WAL store.
- Use a lightweight HTTP client with short configurable deadlines.
- Bound injected context, turn text, tool outputs, failure ledgers, and cached
  prefetch values.
- Never read an entire Codex or Hermes transcript.
- Deduplicate by channel, session, turn/event id, and operation.
- Open a circuit after repeated transport failures and probe health before
  re-entry.
- A dead or slow eimemory service never blocks the host agent from continuing.

## Verification Contract

The release is not complete until automated and live checks prove:

1. The same memory text written in Codex and Hermes produces different scopes.
2. Each channel recalls only its own record.
3. Accepted records are active and marked authoritative in their channel.
4. Duplicate turn and terminal events are idempotent.
5. Unverified success is excluded from real-task evidence.
6. Explicitly verified Codex and Hermes tasks produce valid release-bound
   outcome traces in their own scopes.
7. RPC outage and timeout paths bypass without blocking either host.
8. Codex plugin validation and Hermes provider contract tests pass.
9. Existing OpenClaw adapter and L5 evidence tests remain green.
10. The immutable production release, health identity, deployment receipt,
    service state, and closure checks agree on one commit and version.

## Release Scope

This is a backward-compatible feature release delivered as the next patch
version because no existing public RPC method or OpenClaw scope changes. The
version advances only after the adapter layers, adjacent OpenClaw regression
layer, packaging validation, and live production acceptance pass.
