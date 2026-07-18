# EIMemory 1.9.70 candidate closure audit

## Scope

This audit covers the complete change set from `origin/master` to the 1.9.70
release candidate. It includes release identity, RPC authentication, OpenClaw
2026.7.1 compatibility, source provenance and intake safety, replay manifests,
L5 evidence, Feishu delivery, SQLite/runtime storage, and immutable deployment
rollback.

## Independent tools

- `code-review-graph` 2.3.6 indexed 492 files into 6,174 nodes and 66,583
  edges. Its change analysis covered 121 files and identified the highest risk
  score as 0.95.
- `open-code-review` 1.7.7 reviewed 103 files with MiniMax-M3 and produced 114
  comments: 2 critical, 15 high, 53 medium, and 44 low.
- The OpenCodeReview run completed with provider errors for
  `eimemory/storage/sqlite_store.py` and
  `tests/test_l5_closure_rehearsal.py`. Those files therefore require explicit
  local focused regression evidence before release instead of being counted as
  independently reviewed.

## Confirmed findings closed

- HTTP errors can no longer enter full-text parsing, and IPv4-mapped,
  IPv4-compatible, 6to4, and Teredo IPv6 forms cannot bypass the SSRF gate.
- RPC secret provisioning validates through a no-follow descriptor, rejects
  broad modes and hard links, and creates without overwriting a racing target.
- OpenClaw configuration updates are locked, bounded, inode-checked, durable,
  and create the required plugin allow policy.
- Immutable deployment detects dangling `current`, validates the rollback
  runtime, reports rollback failures truthfully, and preserves the failed
  release for diagnosis.
- Prompt-safety commands, provider responses, prompts, and package hashing have
  hard memory bounds. Optional LLM configuration failures fall back
  deterministically; safety and release gates remain fail-closed.
- Replay manifests use schema v2, explicit scope, transactional sequences, an
  allowlisted release identity, and release-bound evidence.
- Feishu v1 state migrates explicitly, ambiguous receipts never resend, terminal
  history is bounded, stale status-only workflows escalate, and the E2E tool is
  disabled unless explicitly enabled.
- SQLite rebuild keeps the exclusive temporary file, validates projections with
  static SQL, restores the live store after failure, and preserves durable
  export/recovery behavior.

## Findings rejected after inspection

- The reported missing deployment test helpers exist in the audited test file.
- The reported empty-release behavior in `live_task_acceptance.py` was attributed
  to code not present at the referenced locations; release-bound gates already
  fail closed when identity is absent.
- Duplicated replay fields in record content and metadata are intentional:
  content is evidence payload while metadata is the query/index projection.
- Corrupt Feishu delivery state intentionally stops processing; sending without
  trustworthy idempotency state would create duplicate messages.
- URL sources without a registry-bound identity intentionally remain untrusted.

## Release rule

The candidate may be versioned only after all focused layers pass, the two
provider-failed files receive compensating tests, the refreshed graph reports no
uncovered release-critical change, and the one final full suite either passes or
every reported failure is closed by a focused regression without repeating the
full suite.

## Final suite disposition

The single final full-suite run executed 1,851 tests: 1,828 passed, 19 were
platform skips, and four failed. One failure was a stale assertion that still
expected the pre-rollback `$RELEASE_DIR` literal instead of the generalized
`$target_release`. The other three required the optional, untracked 277 MB
LongMemEval dataset. The assertion was corrected; clean-checkout tests now mark
only those three real-data cases as explicit optional-data skips, while all
synthetic converter coverage remains mandatory. The three cases were also run
against the locally available real dataset and passed 3/3. The failed set was
then re-run as focused tests and closed without repeating the full suite.

## Production-gate closure finding

The immutable-release rehearsal rejected an otherwise healthy 1.9.70
candidate because its roadmap, goal graph, and self-continuity references had
been reused from an earlier release. The evidence validator correctly reported
`release_mismatch`; the defect was in two lower-level deduplication contracts:

- learning-record idempotency did not include the verified release commit,
  version, deployment receipt, and release session;
- reflection fingerprint deduplication ignored release identity and could
  replace a new release-bound goal graph with an older record.

Both deduplication layers now preserve retries within the same exact release
while forcing new immutable evidence across releases. A two-release regression
proves fresh roadmap, goal-graph, and self-continuity record IDs, exact binding
to the second release, and stable IDs on a repeated second-release run. The
focused storage and L5/release suites passed 121 tests after the repair. A
code-review-graph incremental scan covered the changed functions and reported
no affected cross-module flow; its remaining test-gap labels are static-link
limitations because the release-level regression exercises both shared
deduplication paths.

The first production retry then exposed a bootstrap exception to that rule:
the deployment receipt is itself the anchor used to derive the release
identity. Scoping receipt idempotency by the receipt-derived identity caused a
new receipt on every verification call, so live acceptance passed all 10 cases
but correctly failed its exact receipt comparison. Deployment receipts now use
their existing deployment tuple (scope, commit, version, release path, rollback
commit, current link, and health endpoint) as the non-circular idempotency
domain; all downstream learning evidence remains release-bound. A regression
first reproduced the receipt churn after it became the current release, then
proved repeated verification returns one stable receipt. The expanded focused
deployment, live-acceptance, storage, L5, release, and version suites passed 158
tests.

The next production rehearsal reached a release-bound L5 assessment with no
missing evidence and replay 12/12, then stopped because readiness was exactly
`data_accumulating`: ten current-release operational probes passed, while ten
verified real tasks across five task types had not yet accumulated. The outer
release gate already allowed this sole exception, but the inner rehearsal had
duplicated an older `readiness_score == 1.0` rule. Both layers now use one
strict readiness predicate: a trusted complete L5 assessment and clean replay
are mandatory; full L5 additionally requires the real-task threshold, while
`data_accumulating` additionally requires ten current-release operational
probes and a positive real-task or task-type deficit. No other partial state is
accepted, and the release summary reports this rehearsal gate truthfully.

The expanded deployment test layer also exposed a Windows-only thread race in
OpenClaw configuration lock-file initialization. A process-local reentrant lock
now serializes threads before the existing cross-process file lock, and unlock
is attempted only after successful acquisition. The 24-worker concurrency test
passed ten consecutive runs; the combined readiness, rehearsal, release,
governance environment, deployment, and version layers passed 117 tests with
19 documented platform skips.

A subsequent GPT-5.6 safety run executed all six cases but failed one
indirect-injection response. Five focused reproductions exposed a deterministic
language-coverage gap rather than unsafe behavior: four variants used
"ignore/untrusted" and passed, while one safely said that the document
instruction was invalid and that secrets would not be read, displayed, or
sent. Indirect neutralization now recognizes "invalid" only when the response
also names an external/document/retrieval context and an instruction/content/
request, and still rejects a counterexample that calls the instruction invalid
but then says it will execute the external document instruction. The prompt
safety and L5/release regression layer passed 72 tests after this repair.

## Legacy JSONL compaction closure

The production storage inspection found one 4,256,275,933-byte legacy JSONL
archive even though new rotations were configured for 64 MiB. The rotation
limit prevented future growth but did not repair historical files, so the
single-file growth requirement was not yet closed.

The storage maintenance path now streams oversized archives into bounded
generation files without reading the source as a whole. It fsyncs and rereads
the generated bytes, requires equal byte counts and SHA-256 streams, atomically
switches an ordered segment manifest, and then retries deletion of superseded
sources. A pending-manifest protocol recovers rotation crashes without losing
the old active file or exposing duplicates. New appends reject any single row
larger than the configured hard segment limit. Unreferenced generation files
left by a crash before manifest activation are cleaned with strict filename,
type, count, and root-bound checks.

OpenCodeReview session `a58a2ce8-504f-422f-b0f4-fbcde7086125` used MiniMax-M3
to scan the complete storage module and returned five comments. The cleanup
retry overwrite, read-only iterator side effect, and loose digest accumulator
contract were repaired. The expected-digest comment was rejected because all
durable callers pair it with an operation ID and the field is a precondition,
not an independent persistence protocol. The UUID fallback suggestion was
also rejected: bypassing a missing/corrupt manifest would still omit generation
segments and could produce an incomplete ordering; corrupt manifests instead
fail closed and SQLite remains the canonical rebuild source.

Focused regressions cover legacy migration, digest and byte equality, strict
size bounds, order preservation, post-migration rotation, pending-rotation
recovery, retryable cleanup failures, preservation of older cleanup work,
orphan generation cleanup, non-mutating reads, missing-snapshot failure, and
nightly maintenance integration.

An independent follow-up review then reproduced five remaining blockers: an
activated generation could be deleted after both manifests disappeared;
historical unterminated rows still had unbounded read paths; cleanup paths were
not restricted to files owned by the log; immediate cleanup could invalidate a
concurrent reader snapshot; and auxiliary JSONL streams were not maintained.
All five now have explicit regressions. Manifests use a backup-first durable
write and restore the primary from the backup, while a generation transaction
distinguishes staging, prepared, and activated data. If both manifests are
gone, activated data fails closed and is never treated as an orphan. Cleanup
paths are name-bound to the current log, default cleanup has a one-hour reader
grace, and immediate cleanup requires an explicit operator override. Strict and
best-effort readers bound every row before decoding. Events, event outcomes,
intent patterns, memory edges, and policy rollout ledgers now share the same
maintenance contract. The independent recheck passed all five reproductions
and found no new P0/P1 loss window.

The final focused layers passed 93 tests: 61 storage/segmented-consumer, 28
migration/eiskills, and four nightly/version checks. A final full-file
OpenCodeReview retry reached its 20-minute provider deadline without a usable
response; it is recorded as an external timeout, not as a zero-finding pass.
The earlier completed MiniMax-M3 review, the independent five-fault recheck,
and the refreshed code-review-graph therefore provide the independent evidence
for this delta. The graph change analysis found no
affected cross-module flow; its static test-gap labels do not resolve the
function-local imports used by these explicit regressions.

## Production RPC and SQLite bounded-sort closure

The post-migration authenticated RPC acceptance exposed a separate performance
failure that the compact health endpoint did not exercise.  `GET /` built a
daily brief, and `list_records()` selected `payload_json` in the same statement
that sorted every matching row.  The production Hongtu scope contained 35,414
rows with 1,283,611,915 payload bytes and individual payloads up to 1,506,021
bytes.  SQLite therefore placed large payloads in the temporary ORDER BY
B-tree: the authenticated probe timed out after five seconds while the service
grew to about 1.4 GiB RSS and continued at one full CPU core for minutes.

The RPC root is now a compact authenticated contract and identity endpoint;
only `/daily-brief` and `/diagnostics` build the diagnostic brief.  SQLite adds
an ordered scope index and uses a single-statement CTE that sorts only
`storage_key`, `updated_at`, and `record_id`, applies LIMIT/OFFSET, and joins the
bounded keys back to `payload_json`.  Keeping this as one statement is required:
an initial two-query implementation reduced memory but OpenCodeReview correctly
identified that WAL writers could commit between the key and payload queries,
causing a short page or a payload that no longer matched the original filters.

OpenCodeReview MiniMax-M3 session `89c8bcca-fec8-42f7-97f8-83e9cbba96e5`
reviewed all four initial delta files in 14 minutes 8 seconds and returned three
comments: the two-snapshot race, a missing deterministic interleaving
regression, and an untested 500-variable batching boundary.  The CTE and its
interleaving regression close the first two; the CTE eliminates `IN` batching,
so the third boundary no longer exists.  A read-only production-shaped CTE
probe returned 500 records / 11,197,572 payload bytes in 1.437 seconds with
68,876 KiB peak RSS and no swap.  Its query plan showed that the unbounded
temporary sort contains only CTE key columns; the payload-side sort is bounded
by the maximum query limit.

The refreshed code-review-graph analysis reported four changed implementation
and test files, eight changed functions/classes, zero affected cross-module
flows, and risk 0.60.  Its five test-gap labels are static mapping misses: the
delta contains explicit RPC-root, bounded-CTE, single-snapshot interleaving,
scope-index, stable pagination, auth, and daily-brief regressions.  The final
focused layers passed 68 storage/RPC-contract tests plus 22 adjacent RPC,
daily-brief, and platform tests.  The full suite was intentionally not repeated
because one fresh full run already existed for this release candidate and the
operator explicitly requested no further full reruns.

Final MiniMax-M3 re-review session
`0a512824-d752-4cde-bd9e-a9e65ace69f1` inspected the repaired four-file delta
in 2 minutes 13 seconds and returned zero comments.  One intermediate
`file_read` tool call used an inverted line range and failed locally, but the
review continued to a successful terminal result with 52 recorded tool calls;
the tool error is not represented as a code pass or finding.
