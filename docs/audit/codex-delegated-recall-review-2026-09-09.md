# Codex capture review contract

The local user may explicitly delegate pending-capture classification to Codex.
The CLI uses the existing secure JSON file loader, verifies the local OS principal
against the delegated exact scope, and accepts only the `review_pending` action.
The delegation identifies the actual user instruction by session and message
digest. Its issuer must verify that reference against the host's user message;
file custody is the authority model, not a cryptographic user signature.

`eval production-query collect --channel codex --review-delegation-json PATH`
collects naturally marked decisions and reviews both new and existing pending
observations in the delegated scope. `review-pending` uses the same arguments
without collecting. Neither command creates a resident worker or changes roles.

The protected JSON packet has schema `production_recall_review_delegation.v1`,
the exact `scope`, `channel=codex`, `source_id=codex`, the local user as `delegator`,
`delegate=codex`, `actions=["review_pending"]`, a timezone-aware `expires_at`, and
`authorization_ref={kind: "user_instruction", session_id, message_digest}`.
Delegation must be present and valid before the collector writes anything.

Each result is a separate `production_recall_delegated_review.v1` service-attested
record, committed with its audit export in the existing store transaction.
Its signing key attests the automatic service outcome, never an operator's
judgment. Exact repeated evidence and delegation reuse the same result; changed
evidence appends another result. Pending records and quarantine history remain
unchanged, including failed and empty retrieval observations.

Dispositions are `maintenance`, `rejected`, `evidence_insufficient`,
`pending_independent_review`, and `accepted`. The last disposition only recognizes
an already existing, independently valid operator accepted case. This contract
does not generate positive labels or create natural gold. Unknown Codex capture
provenance is rejected by the existing positive-label entry as well as skipped by
the collector. A failed or empty retrieval cannot establish a negative gold label.

The September 9 audit traced five old Codex accepted cases through all nine labels,
their pending records and raw decisions. Each original query digest matches a
fixed query in the historical direct-RPC probe script; those cases cannot be
natural gold. All current candidate records have valid active scope/source
boundaries. The old quarantine event did not record the particular failed reader
branch, so current hydration is not proof of the historical failure cause.

The actual repair-task host capture has matching original input, source, scope,
and release, but exhausted admission time with no injection. The old long query
was replayed unchanged: one timeout and one no-evidence response at the existing
model/budget. This is not a stable latency or semantic-quality pass. Gateway
timeout diagnostics now distinguish connection and response wait without logging
private prompts or credentials. Formal natural coverage and strict activation
remain separate, independently verified gates.
