# Semantic admission and closure

SQLite remains authoritative. PostgreSQL contains disposable candidate vectors.
The independent reranker is not a second embedding implementation, and neither
service may authorize a record or fabricate a missing answer.

## Optional service

Run `deploy/provision_reranker.py` explicitly on honxin after code validation.
It pins BAAI/bge-reranker-base to a full upstream revision, uses the pinned TEI
CPU image, listens only on 127.0.0.1:8089, and starts at one CPU / 2 GiB with no
additional swap. It refuses existing resources. Docker logging is disabled
because TEI startup arguments may contain the API key. Never print env files or
raw container inspect/log output. Client/server env files are mode 0600.

`/etc/eimemory/reranker.env` is optional in the RPC unit. Initial ENABLED=0 and
CALIBRATION=unvalidated are deliberate: provisioning is not promotion. Enable
only in the validation process first. Compare candidate budgets 8/12/20 and
bounded original spans on the independent development set, then freeze model,
revision, projection length, score threshold and candidate budget before holdout.
The threshold is a **raw logit**, not cosine similarity or probability. Keep the
calibration artifact digest in EIMEMORY_RERANKER_CALIBRATION. Do not fit the eight
historical cases. No external inference endpoint or redirect is accepted.

The client has one in-flight slot, a bounded response, no automatic retries or
proxy use, and fails closed on missing/duplicate/non-finite scores. Default full
engine scoring timeout is 2 seconds; the proactive path has a 3-second budget
when admission is enabled. Configure host adapter transport timeouts with room
for the full path (at least 3.2 seconds); an old 0.8-second host deadline must not
be mistaken for model failure. Runtime p95 must still be <= 3 seconds. Explicit
title/ID lookup is reported as identity lookup, not natural-answer acceptance.

The final gate does not pad. It verifies current authority before inference and
again afterward. Per-turn proactive caching is disabled for this mode so updated
records and formerly empty results do not inherit stale admission. Auxiliary
rules cannot reintroduce candidates rejected from ordinary recall items.

## Index maintenance

Memory-only `vector-index sync` uses an immutable **text projection snapshot** for
bootstrap, persisted under `state/vector-projections/`. This is not a SQLite
vector scheme. The snapshot contains no embeddings and is not an authority.
Changing live memory cannot restart its embedding pass. Once committed, the
coalesced SQLite change journal identifies modified/deleted/renamed keys; exact
compatible vectors are reused and only changed content is embedded. PostgreSQL
applies each prefix atomically with watermark/revision compare-and-swap. An index
behind authority remains unavailable until caught up; per-hit authority checks,
query revision fences and cache bindings are not relaxed. An index predating the
journal floor requires a snapshot once. Do not run two maintenance workers.

Snapshot/model/projection mismatches cannot resume incompatible work. Successful
bootstrap removes only its own derived snapshot file. Journal entries coalesce
by storage key (not by number of writes), retaining tombstones. Observe backlog,
failures and applied revision; a completed initial build is not sustained liveness.

## Evidence and privacy

`eval production-query capture-status` reports per-channel collection barriers,
without original text. Empty genuine proactive decisions remain collectible.
`accept-negative` accepts a private packet containing query, labeler and reason,
bound to an actual pending capture. `negative-eval` accepts original queries and
negative label IDs. These labels do not increase positive ranking coverage.

Optional EIMEMORY_CAPTURE_ORIGINAL_QUERY=1 stores the original and effective query
with bounded online context in a private SQLite table. Default is off. Verify
authority database permissions before enabling. It retains at most 10,000 entries
for 30 days and removes entries whose authoritative decisions have expired; no
raw input is exported to records or reports. Inputs remain scope/source/channel
and digest-bound. Existing privacy guarantees for ordinary audit rows remain.
`original-eval` can reconstruct captured engine inputs, but does not claim to
reconstruct external OpenClaw bundle processing or replace observed host delivery.

## Fixed quality gate

Run `python -m eimemory.evaluation.semantic_recall --root ROOT --dataset PRIVATE_JSON
--output REPORT_JSON`. This tool never seeds the corpus. Its packet schema is
semantic_recall_cases.v1; each case includes case_id, original query, split
(development/holdout/regression), intent_group, exact scope, source_id,
expected_groups (equivalent record IDs per fact), and optional forbidden_refs and
online task_context. Empty expected_groups means a reviewed no-answer case.
One intent group cannot span development and holdout. Freeze the secure packet
before inference; report its digest, not its text.

Release requires at least 60 cases, independent holdout hit@1/hit@5 >= .90,
returned precision >= .90, false recall <= .05, no forbidden hits or unavailable
queries, and full recall p95 <= 3000 ms. All historical regression cases must
pass strictly (first correct, no unrelated tails, all required fact groups).
Missing positive or negative coverage is unknown, not 100%. Existing production
quality, noninferiority, stability and memory gates still apply independently.

## Release sequence and evidence boundaries

Complete code and coherent commits, minimal checks only, then one full Linux
suite. Repair failures with targeted regressions only. Run real-corpus acceptance,
resource contention and fault recovery. Merge master and deploy the exact full
commit from /dev-project/eimemory only after pre-deployment gates pass. Verify
GitHub, repo, /opt/eimemory/current and /health identities agree. Produce the
same-candidate production report/strict state, replay/live/closure/readiness and
host-render checks. A natural sample deficit cannot be fixed with generated
queries or importing memories; it remains a real usage dependency. Observe the
candidate without marking it complete merely because a timer elapsed.

Rollback must disable the optional inference/index paths together and restore a
verified conservative version; retain SQLite records and diagnostic artifacts.
Do not delete authority or return weak hash-only noise to hide unavailable
inference. Deliver final metrics and exact release status to the verified Feishu
private chat and verify API delivery; a notification is not deployment success.
