# Task 7 Proactive Recall Closure Report

## Outcome

Implemented one deterministic, LLM-free `ProactiveRecallService` shared by
OpenClaw, Codex, and Hermes. SQLite remains authoritative. The implementation
adds proactive decision/delivery/use feedback without creating a second memory
authority or a second telemetry fact store.

## Closed contracts

- Persists a bounded latest-four-turn ledger per channel, exact scope, exact
  source allowlist, and session. Summaries are bounded and redacted; only
  deterministic entity terms are appended to the recall query.
- Uses `GovernedRecallEngine` with `exact_scope_only=True` before candidate
  search. Every cached hit is rechecked for exact scope, source, status, and
  channel-derived authority.
- Documents and tests the inclusive 0.70 confidence boundary, max-three result
  cap, context character cap, deterministic control cohort, same-session
  dedupe, mandatory safety-policy exemption, and poisoned-cache isolation.
- Emits escaped JSON records inside an untrusted-data envelope with opaque
  20-hex citations. Similar answer text never counts as use.
- Persists `volunteered -> injected -> used|not_used|rejected` plus the
  control-only terminal `suppressed` state through the
  existing `feedback/memory_usage_telemetry.v2` path. Decision transition,
  feedback record, and export outbox are one `BEGIN IMMEDIATE` transaction.
- Persists task effect separately from use telemetry. Paired effect is exposed
  only when the same exact source/release/policy pair has explicit verified
  boolean task outcomes in both control and treatment; otherwise it is
  `not_available`. Treatment citation use remains a separate diagnostic.
- Binds decisions, cache keys, citations, and feedback to the complete current
  release identity. A missing release fails open with no injected context and
  bounded diagnostics. Same-commit redeployments with a new receipt create a
  distinct decision identity.
- Caps turns, decisions/items, in-memory caches, Hermes pending state, bypass
  diagnostics, worker concurrency, and timeout. Runtime shutdown first stops
  admission, drains all already-running recall workers, then closes SQLite on
  the shutdown thread.
- Codex uses official `UserPromptSubmit`/`Stop` shapes and closes an exact turn
  from a new process without reading transcripts.
- Hermes uses the provider prefetch plus official `pre_llm_call` and
  `post_llm_call` hooks. Its key includes channel, exact scope, source allowlist,
  session, and query; session switch/reset clears pending context.
- OpenClaw keeps the existing authoritative `before_prompt_build` path. The JS
  bridge records `injected` only after it actually builds non-empty
  `prependContext`; agent/task terminal hooks close explicit use feedback.

## TDD evidence

The implementation was driven through focused RED -> GREEN tests for:

- four-turn eviction and restart persistence;
- 0.69 reject / 0.70 include;
- max-three/context escaping and same-session dedupe;
- cache/channel/scope/source/release isolation, including same-commit new
  deployment receipt;
- exact-scope candidate search and poisoned-cache rejection;
- all state transitions, protected metadata, atomic rollback/retry, and exact
  namespace validation;
- deterministic paired control/treatment metrics and mandatory safety context;
- true timeout, worker bound/shutdown, and bounded diagnostics;
- Codex/Hermes/OpenClaw host event shapes and cross-process closure;
- persistent decision keyset cap with no orphan items.

Independent review then supplied 12 executable counterexamples: persisted
decision conflicts, render-cap citation drift, OpenClaw actual-injection drift,
tautological paired metrics, OpenClaw completed-cache drift, incomplete
transition namespaces, worker/store close races, combined mandatory/voluntary
overflow, omitted rule candidates, Hermes terminal hooks without host IDs,
Hermes completed-cache drift, and session dedupe beyond 512 item rows. The first
formal run was `9 passed, 3 failed`; after correction the same named matrix is
`12 passed`. The shutdown case also exposed and eliminated a Windows SQLite
cross-thread close access violation.

Post-commit review found one additional Hermes counterexample: a completed
background prefetch with no consumer could leave a ghost pending decision and
be mistaken for the next different query. A dedicated RED test now proves that
unconsumed background decisions close as `not_used`, while the no-ID unique
pending fallback is permitted only for an empty query and never crosses query
identity.

The same review cycle then closed further evidence-integrity cases: exact-turn
replay now returns a persisted immutable context/item snapshot even after the
turn ledger changes; verified task outcomes are first-write CAS values with
identical retry and conflict rejection; candidate caches bind the effective
four-turn recall query and task type; repeated paired samples are matched
deterministically instead of overwritten; Codex Stop and Hermes post-LLM
payloads cannot forge verified task evidence; and OpenClaw preserves policy
suggestions plus mandatory fallback context when optional proactive persistence
fails. Legacy OpenClaw recall-view rows are selection audits (`injected=false`);
actual delivery remains the citation-bound proactive acknowledgement.

Final submitted-state review found and closed two additional boundary gaps.
The shared adapter RPC now rejects every caller-supplied non-empty
`terminal_outcome`, so possession of the common bearer plus a forged channel
cannot mint verified paired/L5 evidence; trusted OpenClaw outcome attestation
continues to use its in-process host hook. Hermes now terminalizes the exact
persisted decision on capacity eviction, session switch/reset, reinitialize,
foreground/background abandonment, and shutdown. Cleanup snapshots pending
identities while locked and performs bounded RPC calls after releasing the
lock.

The last transport-failure counterexample is closed without introducing a
second local authority. Hermes retains failed exact terminal calls in a
bounded in-memory retry queue, drains it before later hooks, and fails closed
for new proactive prefetch while responsibility remains. A lost provider
process is recovered from the existing authoritative SQLite decision ledger:
the next service call reconciles decisions older than the bounded 15-minute
lease through the same not-used/control-suppressed transition and feedback
contract. Tests cover first-fail/next-hook success, used-citation retry,
continuous failure/capacity gating, and process-loss recovery for explicit and
wildcard source scopes.

Fresh verification after counterexample closure:

- Named independent-review counterexamples: `12 passed`
- Task 7 service + Codex + Hermes + RPC + OpenClaw JS selection: `84 passed`
- OpenClaw full adapter regression: `64 passed`
- Full Codex + Hermes + runtime RPC channel regression: `63 passed`
- Final Hermes + runtime RPC boundary regression: `45 passed`
- Final proactive + Hermes + runtime RPC focused regression: `85 passed`
- Task 4-6 recall engine/fusion/Postgres focused regression: `179 passed`
- `python -m compileall -q eimemory integrations/hermes/eimemory`
- `git diff --check`

The final fresh counts after review are recorded in the commit handoff.

## Scope guard

No LLM dependency, version bump, push, deployment, second authority, or broad
new model-callable tool surface was added. Postgres remains optional and SQLite
remains the default/authoritative store.
