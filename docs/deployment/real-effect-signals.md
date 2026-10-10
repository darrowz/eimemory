# Real-effect signals and test-rule cleanup (1.14.53)

This release collects evidence for future improvement. It does not change recall
ranking, train a policy, apply autonomous data changes, or certify L5. Deploy the
RPC server, memory provider and Hermes hook together. The hook implementation
digest changes: obtain fresh provider binding/catalog activation and release
evidence rather than reusing the 1.14.52 qualification.

## Signal contract

`adapter.proactive_signal` requires the separate Hermes host producer credential
already used for host attestation. An ordinary adapter bearer cannot write it;
another channel's producer cannot write Hermes observations. The original
decision must match channel, all four scope fields, source ids, session and
decision turn. Acceptance-generated decisions are rejected. Its persisted
release, policy, pair/cohort and actually delivered memory references are captured
by the server, not supplied by the caller.

| Phase | Labels | Interpretation |
| --- | --- | --- |
| `turn_completed` | `tool_chain`: succeeded/failed/unknown/not_run | Explicit tool exit code, boolean success/error fields, or execution exception. Tool success does not imply task success. Free-form tool prose is unknown. |
| `turn_completed` | `task_success`: succeeded/failed/unknown | Optional explicit host `task_success` boolean; default unknown. This remains an observation, not a signed verified outcome. |
| `turn_completed` | optional finite `latency_ms` | Pre-call to completed-call elapsed time; omitted if the pre-call was missing. |
| `next_user` | `correction`, `reask`: suspected/none/unknown | Correction prefix or same normalized question within 300 seconds, attributed to the previous completed decision in the same session. Heuristics are not verified dissatisfaction. |
| `explicit_rating` | `rating`: positive/negative | Explicit host feedback for a named completed turn; silence is not a positive vote. |

The supported Hermes hook contract has no native thumbs-up/down event. Hosts may
call `provider.on_user_feedback(session_id=..., turn_id=..., rating=..., event_id=...)`
or forward `feedback_rating`, `feedback_turn_id`, `feedback_event_id` through the
next `pre_llm_call`. `post_llm_call` accepts optional host `task_success`. Without
these host fields ratings are absent and task success is unknown; do not present
zero observed ratings as zero negative feedback. No new model-facing tool may
self-report verified success.

Telemetry stores fixed labels, latency and routing/evidence identities in
`proactive_effect_signals`. It rejects user text, tool payloads and arbitrary
summaries. Correction detection never traverses conversation history. Repeat
matching uses an ephemeral keyed fingerprint kept only in bounded process state;
no query text or query fingerprint is saved in the signal ledger. Ordinary memory
capture/turn-context features retain their existing separate contracts.

When Hermes supplies its profile home, the provider persists unsent label payloads with mode 0600 in
`<hermes_home>/logs/eimemory-effect-signals-<session-digest>.json`, with atomic
replacement and fsync. One worker sends signals outside the host callback path;
the default observation client has a one-second timeout. Up to four pending
entries retry on the next callback or shutdown; at most 64 entries are retained.
Same event/same payload is idempotent; same event/conflicting payload is rejected.
Queue overflow is counted, never silently reported as complete delivery.

Inspect `eimemory_status.adapter_local.effect_signals`: `sent`, `pending`,
`dropped`, `error`, `worker_running`, `durable_queue_configured`, `unbound_tool_callbacks`,
`unbound_completed_turns`, and `native_reaction_hook=false`. Missing host
credentials or `durable_queue_configured=false` must be fixed before claiming production coverage. Missing host turn
ids use process-local turn identities for observations only; they cannot produce
verified host receipts. After restart the durable unsent queue is recovered;
previous-turn heuristic matching and rating targets are process-local, so delayed
feedback across restarts is unavailable in this release.

## Quarantine old test rules

Run with the production runtime configuration that selects the intended storage
root, and explicitly provide the exact tenant/agent/workspace/user and source partition. Take the normal
storage snapshot first. Example commands (preview is the default):

```bash
owner=(--tenant-id default --agent-id AGENT --workspace-id WORKSPACE --user-id USER --source-id default)
eimemory storage quarantine-test-rules "${owner[@]}"
eimemory storage quarantine-test-rules "${owner[@]}" --manifest /path/reviewed-test-rules.json
eimemory storage quarantine-test-rules "${owner[@]}" --manifest /path/reviewed-test-rules.json --apply
eimemory storage quarantine-test-rules "${owner[@]}" --revert
eimemory storage quarantine-test-rules "${owner[@]}" --revert --apply
```

Explicit `acceptance_generated=true` or `test_generated=true` in rule metadata,
content or provenance proves a test rule. New marked rules are archived on write
and cannot be promoted. Historical marked active rules can be quarantined.
Deployment/test producers must mark their fixtures at creation, for example
`runtime.evolution.store_rule(..., acceptance_generated=True)`; marked promotion
candidates are blocked before creating live rules. Unmarked new fixture
ids cannot be identified safely from duplicate text alone.
Unmarked rules appear in `unclassified` with id/version digest. Similarity or
duplicate text never proves test provenance. Confirm old deployment-test rules
from their production creation/promotion evidence, then copy only those selected
id/digest pairs into the empty `manifest_template.rules` list. The tool rejects
wrong-scope, missing, duplicate or stale manifest entries and incomplete scans.
An empty reviewed manifest selects no rules; omitting the manifest selects only
rules with explicit test provenance.

Applying writes `deprecated` rule envelopes, retains the original snapshot and
commits archived audit receipts plus export outbox in one transaction. Real rules
remain active. An ordinary rewrite or promotion cannot reactivate the same
quarantined id. Restore refuses any envelope changed after quarantine and keeps
explicitly marked test rules inactive; unmarked reviewed rules restore their
original envelope and status. Use a fresh preview/version manifest to select a
subset for restore, or omit it to restore all quarantined rules in that exact
scope/source. This tool covers `kind=rule` records, not `intent_patterns`.

Production's reported 49 test rules are an investigation target, not a built-in
count or a deletion selector. This workspace has no production database; no claim
of cleaning those 49 rules or backfilling the 515 old decisions is made. Verify
new real turns produce label rows, negative tools/corrections remain negative or
suspected, and quarantined rules stay out of the active rule search before
proceeding to the 1.14.54 daily-report phase.
