# Recall closure checkpoint — 2026-09-09

## Subsequent production checkpoint

Later execution: deployment 2da86fdb failed its Hermes RPC replay and rolled
back successfully. Deployment 5fe5a7b3 completed successfully after fixing the
verifier to read effective process environment (11.5 seconds, not the static
5-second declaration) and including activating/deactivating writer units in
snapshot quiescence. Six focused Linux deployment regressions passed.

The post-deploy hydration check exposed a second legacy projection drift:
1473 of 1726 scoped records differed only in derived updated_at, with all
other candidate digest inputs matching verified envelopes. The explicit
timestamp repair restores SQL projection time without modifying envelopes.
Four focused storage tests passed on Windows and Linux. Preserve the old/new
time audit and synchronize PostgreSQL projection metadata; unchanged vectors
may be reused only after proving all embedding inputs match. The earlier
24/24 hydration result alone did not prove candidate projection validity.

Production 1.13.0 at d58c54af444c140e791dc0797718916edcdd4c6c is deployed
with actual PostgreSQL vector reads enabled. The original checkpoint below is
historical. The incremental worker maintains the existing derived index;
SQLite remains authoritative. No new resident model was installed.

Production hydration exposed 1065 inline records whose legacy L1 maintenance
metadata had changed without updating payload_digest. Removing exactly the
two maintenance fields reproduced every prior digest; no unexplained content
changes were accepted. An explicit, scoped, transactional CAS repair updated
only digests and preserved raw payloads, identities, labels and timestamps.
The private audit retains old/new digests and plan hash
804ae8bc4331b85c6ac3c340b0b657e39a292dc54728ec6d0beec84ec5bd64ce.
Three focused repair tests passed locally and on Linux; adjacent digest and
corruption checks passed (4 tests). Index revision catch-up and post-repair
production hydration must be checked independently of this data repair.

Formal natural coverage remains 10/15 (Codex 0/5). The refreshed dataset has
10 valid live-authority labels, replacing a pointer containing quarantined
labels without deleting its historical snapshot. Current production gate
prg_d6741afc249a91bd1b889d7cadf74e98 remains not_run because required channel
coverage is missing. L5 readiness remains incomplete. Difficult-question
production inference still timed out within the approved 10-second bound;
neither vector enablement nor this checksum repair constitutes quality closure.

An optional per-session model override is isolated on a separate branch.
No model allowlist changes or Windows-wide Codex collection were authorized
or performed. Final delivery must retain these limitations.

This checkpoint supersedes the execution status in the September 8 reports;
those historical failures remain preserved. Release 1.13.0 is not yet deployed
at this checkpoint. Production vector reads have not yet been enabled.

## Implementation and validation

- Reuse the existing OpenClaw xai/grok-4.6 model only for difficult questions.
  User approved a 10-second total recall budget; ordinary recall remains 3 seconds.
- No additional resident inference model. Two idle-expiring Node SDK clients
  measured approximately 258 MiB combined. They expire after 120 seconds idle.
- Scoped source quotations, model identity, deadlines, and original query
  evidence are checked. Generic yes/no wording alone no longer forces inference.
- Codex plugin hook ceiling is 13 seconds; transport is 11.5 seconds and receipt
  acknowledgement is separately bounded to 1 second. Production cached plugin
  and its existing 2-second override still require cutover.
- One consolidated full suite on 02b5119c: 3921 tests, 3911 passed, 6 skipped,
  4 failed. The run was resumed after active temporary SQLite fixture files
  disappeared; 2287 completed cases were not repeated. The deletion cause is
  unproven. Remaining 1634 cases used an isolated temporary directory.
- The four failures were corrected in 8aa3153d: bootstrap test doubles now
  accept the authority runtime, and recall reuses its start timestamp. All four
  targeted regressions passed. Latest-code Linux focused suite: 179 passed.
- Isolated real PostgreSQL bootstrap/update/delete/stale-CAS tests passed.
  Its disposable owned schema was removed; production data was not removed.

## Quality evidence and limitations

Known regression initially passed 8/8 before consuming frozen holdout. The one
68-case frozen evaluation at 9ae00d85 had two timeouts (hold-p18, hold-n19):
positive hit@1/hit@5 35/36, returned precision 100%, false recall zero. It FAILED.
Ordinary p95 was 2560 ms and assisted maximum 9914 ms. Preserve the original
failed report; it is not a passing holdout.

Transport repair at 402e55a5 passed 28/29 targeted cases. The remaining p18
already had an admissible correct fast-path candidate but generic Chinese
yes/no wording unnecessarily triggered inference. Generic routing correction
at f628fa42 passed all 5 affected/control cases, ordinary maximum 2012 ms and
assisted maximum 9165 ms. These are targeted repairs, NOT a new frozen holdout.
No gold IDs or question-specific templates were added to production routing.

Natural dataset remains 10/15: Hermes 5, OpenClaw 5, Codex 0 accepted labels.
Existing Codex observations are not a substitute for qualified natural labels.
No current-release production companion report or strict L5 state is claimed.

## Remaining execution

Finish the full-authority derived index, prove actual vector reads against its
generation and revision, merge and deploy the immutable release, activate
shared configuration and client budgets, refresh the official Codex plugin,
then verify actual serving identities, recall, capture, and formal readiness.
Final Feishu delivery must accurately distinguish deployed technical capability
from missing natural-production acceptance, and must be verified by readback.
