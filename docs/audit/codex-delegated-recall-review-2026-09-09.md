# Codex capture review contract

This is a local implementation awaiting independent main-thread acceptance and
separate deployment release. It does not authorize a production deployment.

## Authority

`operator` and `release_operator` are existing CLI packet authority roles. The
implementation verifies protected file custody and exact scope; it does not
verify a human identity signature. They remain unchanged. A delegated automatic
review uses `labeler=delegated_ai`, orchestrator/reviewer `codex`, the actual
configured `model_id`, and a distinct service attestation. It never impersonates
an operator or asserts that the service HMAC is the user's signature.

The protected delegation packet uses `production_recall_review_delegation.v1`,
exact scope, `channel=codex`, `source_id=codex`, local OS user as `delegator`,
`delegate=codex`, a timezone-aware expiry, and an actual user-message locator:
`authorization_ref={kind: user_instruction, session_id, message_digest}`.
A Hermes locator can additionally contain `source_message_id` and
`source_store=hermes_user_history`. The issuer verifies this locator; the secure
JSON loader checks file custody. A forwarded Codex task is not the original grant.

`actions=[review_pending]` retains classification-only behavior. The explicit
`actions=[review_pending, accept_positive_labels]` grants bounded AI relevance
review. The actual user consent independently verified by the main thread is
Hermes session `20260907_144314_8210cf15`, message `63467`, role `user`, digest
`7fa60510d31e446626a2e8af3b7595e864794818c8e43bd63f1d2e839ddd1d3a`.
This authorizes the task, not the correctness of any particular label.

## New positive path and normal trigger

The existing L1 worker invokes `collect_and_review_configured` after queue drain
when `EIMEMORY_CODEX_REVIEW_DELEGATION` identifies a valid protected packet.
No new timer is required. Existing installations without the variable retain
their current workflow. The existing collector CLI also accepts
`--channel codex --review-delegation-json PATH`.

Collection and review queries bind exact tenant, agent, workspace, user, channel
and source before reading decision provenance. Other-source pending records are
outside the default delegation and remain untouched; they cannot block an authorized
Codex batch. An explicit `legacy_review_source_ids=[default]` extension permits
review of existing `source_id=default` pending records only within the same exact
Codex scope. It does not collect new legacy observations, migrate their sources,
run a semantic model for them, or authorize positive labels. Their real scoped
provenance is checked and insufficient/maintenance/quarantine outcomes are retained
as signed review receipts. Other legacy source names are rejected before writes.
Unknown provenance, maintenance, quarantine, missing host input,
failed retrieval, missing/invalid candidates and cross-boundary links cannot
reach positive review. Empty observations remain pending evidence.

A fresh eligible capture reaches one bounded semantic model call using the
existing configured caller model and at most nine seconds. The model must assess
the original host question against current candidate content, respecting entity,
time, requested attribute and polarity, and return sufficient verbatim support.
Related vocabulary alone is explicitly insufficient. Grade 3 labels require
an exact quote and explanation. An empty selection rejects the proposed positive
label without inventing negative gold; it needs no positive query features.
Model failure, identity mismatch or invalid output defers. At most five model
calls occur per worker invocation; deferred model work retries after five minutes.

The model runs outside the store write lock. Immediately before promotion the
service revalidates the grant and entire evidence snapshot. In one existing
transaction/outbox mutation it writes signed `prdl_` AI label evidence, a new
`prqa_` accepted case and the signed review receipt. Signed authority binds the
actual model, user grant, original input digest, candidate digest, query features,
quote digest and semantic reason. No operator label signature is fabricated.

Accepted-case validation, dataset manifests, freezing and live hydration support
this distinct identity through the delegated signature contract. They recheck
natural capture, original input, current candidate and query-feature digests.
Changing evidence invalidates old authority instead of silently relabelling it.
Unchanged reviews reuse receipts and make no extra model call. Previous receipts,
pending records and quarantine history remain intact.

## Verification and limits

Focused tests exercise genuinely new acceptance through dataset hydration,
worker collection and idempotency; semantic rejection; missing evidence and
cross-boundary denial; tampering; evidence changes during review; and retry after
a temporary failure. A separate live-model contract probe uses explicitly
synthetic fixtures for a direct answer and an unsupported retention-period
question. It creates no natural capture or production gold. The main-thread
handoff includes the exact model responses and test node IDs for review.

The old five Codex accepted cases are generated direct-RPC probes, established
by source-script/query-digest matches. Their nine candidate references currently
have valid boundaries; this does not restore natural provenance. Keep their
quarantine and historical audit. The current repair-task natural capture exhausted
admission with no returned candidates; it does not supply a positive gold label.

An exact original-input cold/hot replay exposed redundant SDK prewarming. A
single caller now starts one SDK process and expands to two only under concurrent
load. Request IDs, deadlines and discarded timed-out workers remain in place.
Before this change, observed total times were 8.13/6.75 seconds; afterward they
were 7.65/6.70 seconds. These are individual observations, not a latency guarantee
or an attribution of all timing differences to the code. The same model and
3-second ordinary / 10-second assisted budgets remain. Gateway response waits
were 5.30–5.90 seconds. The historical timeout lacked a session/stage diagnostic,
so its precise upstream cause is not retroactively proven. The handoff retains
that limitation; successful no-evidence replays do not prove all timeouts fixed.

Natural coverage, independent semantic acceptance, deployment readback and strict
release activation remain separate gates. None is claimed complete by these
local contract tests.
