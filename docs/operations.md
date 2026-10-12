# Agent Runtime Adapter Operations

This runbook configures independent host channels for the local-first memory
and evidence-gated learning runtime. The host names and profile values below
are maintained deployment examples; use your own configured identities.
Operational verification is separate from business recall acceptance. See
[receipt-based status](acceptance-status.md) for the latest reported run.

eimemory exposes the additive `agent.runtime.v1` contract through the existing
authenticated RPC service. OpenClaw remains the current authority source for
its own channel. Codex and Hermes are independent authoritative long-term
memory channels under `authority_mode=per_channel`:

- OpenClaw: existing base scope, unchanged.
- Codex: `embodied::channel::codex`.
- Hermes: `embodied::channel::hermes`.

Recall, writes, terminal evidence, metrics, and L5 samples remain inside the
selected channel. There is no implicit federation or cross-channel fallback.
Only the deployment receipt identity falls back to the base release so every
channel's evidence can bind to the same deployed eimemory commit.

The operator reports accepted deployment on 1.14.69 / `1729dfd2`.
Nightly `ref_0b76c0408ad1` ran 21:23:41–21:32:59 and exited 1; its only failed
step was `research_closure_review`, with
`command_completion_failed:provider_request_failed`. The underlying provider
failure is not identified as network, configuration or quota trouble.

Controlled memory checks ran 10 cases: nine passed and one known item was
missing with zero returned records. Collection created four cases and found
156 existing cases; all four new cases had empty candidates and completed
non-certifying reviews. Formal recall remains unaccepted at 0/3.
Evolution has no candidates; `knowledge_inventory_truncated` blocks knowledge
applicability, so waiting for more data alone cannot explain or resolve it.

Mainline 1.14.70 adds bounded resumable inventory scans, safe provider failure
classification and controlled retrieval-stage diagnostics. These changes do not
certify production recovery, formal recall or an autonomous evolution outcome.

## Read persisted nightly evidence

After a managed run, inspect the stored nightly, deployment-association, recall
quality and memory-evaluation receipts. Record release identity, execution
verdict, formal gate verdict, accepted-case counts and each waiting/blocking
reason. Exit 0 or `execution_ok=true` alone cannot certify recall.

Known-item smoke is diagnostic. `recall_quality_evidence_incomplete`,
`memory_eval_dataset_empty`, `hypothesis_missing_or_ambiguous` and an unavailable
model reviewer leave their corresponding evidence gaps open. Mainline workflow
repairs began in 1.14.58; mainline 1.14.59 also persists safe research-review
diagnostics in supervisor receipts. The earlier 1.14.58 / `534bb2f0` run failed at
`research_closure_review`, with `research_review_llm_unconfigured` in detailed
records. The earlier 1.14.64 / `f61487e2` attempt switched the release link but
failed deployment health acceptance: `ok=false`, `health_exit_code=1`,
`TimeoutError`. Its closure was blocked at `deployment_receipt` with
`health_fetch_failed`, exit 1. The handoff denied nightly. At that failed attempt, service
health and the specific timeout cause were unverified.
The historical 1.14.63 / `4fbcbbc9` deployment receipt passed,
and the October 11 nightly exited 0 with `execution_ok=true` and no failed steps.
Smoke passed 10/10, diagnostic error rate was 0.00, and hit@1 / hit@5 were 1.00.
The formal gate remains false with `recall_quality_evidence_incomplete`:
accepted cases remain 0/15; all 321 reviews produced zero passes and zero
pending conclusions. The memory benchmark has no retrieval cases; all nine
evolution candidate counts are zero, with no applicable knowledge link for the
gap revision. Formal recall and evolution remain unaccepted. See [evaluation](evaluation.md) and
[remaining acceptance work](acceptance-status.md#work-required-to-close-acceptance).

Before diagnosing a colleague's empty recall, verify their actual Feishu
session identity, host adapter, effective client configuration, RPC destination
and authorized scope/source policy. Prior controlled checks with incomplete
identity verification do not establish a current fault. Service smoke does not
substitute for this path. Use the [team recall checklist](team-recall-acceptance.md)
for participant-level natural questions, no-answer near neighbors, permission
boundaries and source readback.

Mainline 1.14.64 introduces policy v5 with a three-case / three-label floor in
the dataset's existing exact scope. This explicitly requested count change
retains label authority, release authorization and quality/isolation checks.
The deployed 1.14.63 receipt above keeps its historical 0/15 result. Use
[existing authorized memories](team-recall-acceptance.md#using-existing-memory-for-four-checks)
for controlled targets and contrasts without relabelling them as natural gold.

Mainline 1.14.65 reports bounded health snapshots: `store.migration_status`
distinguishes `available`, `timeout`, `unavailable` and liveness-only
`not_checked`; `retrieval.health_status` describes the optional candidate
snapshot. Storage probe timeout/error refuses readiness. An optional retrieval
timeout reports the source unavailable, keeping local storage readiness separate. Repeated
requests reuse one in-flight read per fragment; an expired healthy value is
not substituted for a blocked refresh. `/livez` skips both readers and cannot
replace authenticated deployment health acceptance. A timed-out worker is not
cancelled; it may finish and supply a later fresh snapshot. Production recovery
still needs a new release-bound deployment receipt.

From 1.14.60, research review automatically discovers the service user's Hermes
installation when no explicit feature/shared command is selected. It follows
current Hermes model/provider configuration through an isolated SDK completion.
Ordinary installations need no extra review route. Put any nonstandard launcher,
source-root or profile-home override in protected `governance.env`, then use
the configuration-only preflight. See [automatic review](evaluation.md#automatic-hermes-review).

From 1.14.61, the semantic judge uses the same supported installation discovery,
retaining tool-free/read-only execution. Each scanned automatic label review
persists pass or fail with reasons; unavailable transport or insufficient signals
concludes fail without creating a relevance label. Later evidence can trigger
another review. Inspect persisted conclusion IDs and nightly diagnostic counts;
old receipts remain historical. Version 1.14.62's reason-code repair is confirmed
in that release's durable summary: invalid capture boundary 10, missing matching
decision 186, semantic items not delivered 6. These review codes do not identify
the smoke retrieval failure. Mainline 1.14.63 fixes digest deduplication that
merged different dates or content sharing a summary prefix. Native local checks
retain the target without changing query, expectation or gate. It also preserves expected/returned record
IDs and explicit error-rate counts in newly persisted quality reports, while
excluding query and memory bodies. The later 1.14.63 readback confirms aggregate
smoke recovery; it does not provide a historical stage trace or certify formal
business recall.

## Controlled memory checks from authorized records

In 1.14.69, an unconfigured nightly memory evaluation can use existing records
only after replay generation explicitly succeeds and produces no retrieval cases.
Configured inputs keep precedence; loading errors, replay errors and unknown
success states cannot activate fallback. The channel, exact logical scope and
source allowlist come from caller authority and private release bindings, never
from a database scan or the presence of a Hermes installation.

The resulting `memory_eval_ci` receipt reports
`dataset_source=controlled_records` and `dataset_kind=controlled_memory_benchmark`.
Generation, source references, actual returned references, ranks and latency
describe a controlled known-item check through the current runtime recall path.
Source revalidation rejects changes, expiration or lost authorization around
retrieval. No query or memory body is copied into the report.
Generation retains `stable_before`, cutoff skip counts and separate
`scan_truncated` / `selection_truncated` flags. Nightly excludes sources created
after its existing start cutoff. Invalid cutoff blocks generation; invalid
source creation timestamps exclude those records. The cutoff avoids same-run
new sources and does not independently certify that older sources are indexed.

Inspect `controlled_benchmark_passed_threshold` separately from
`memory_benchmark_accepted`, which remains false for this path. Generated
known-item questions cannot supply trusted natural-production labels or
independent correct-no-answer evidence. The default case budget is 10;
`EIMEMORY_CONTROLLED_MEMORY_BENCHMARK_CASES` is bounded to 1–50. Empty eligible
material and unresolved authority retain explicit not-run/blocked reasons.
See [the evaluation contract](evaluation.md#controlled-baseline-from-existing-memory-11469).

From 1.14.70, native controlled samples also retain `retrieval_diagnostics`
with schema `retrieval_stage_diagnostics.v1`. `measurement_origin` distinguishes
the runtime bundle from a runtime exception. Measured engine, pipeline, selector
and online-gate counts and allowlisted drop reasons expose stage boundaries;
at most eight source names and eight fixed pipeline phases are retained. Unknown
provider names become `other_configured_source`; absent measurements stay unknown.
`source_availability` separates available,
degraded, unavailable and unknown sources using reported collection completeness,
source searches, budget exhaustion and fallback. `source_partition` keeps the
exact configured case authorization in the native report; the diagnostic adds
no access.

`empty_result_cause` identifies an available empty candidate collection or
rejection of all current selector inputs only when the required measurements
support that conclusion. Mixed-stage drop counts and partial evidence retain
`empty_origin_unknown`, alongside their actual reported drops. Unavailable or
incomplete retrieval remains separate. These fields do not prove that the
expected target was removed at a particular stage or that ranking is defective. Query,
memory text, raw source error labels, provider paths and local proof contents
remain excluded. The earlier 1.14.69 empty known-item miss cannot inherit these
new measurements from a later lookup.

## Knowledge applicability for capability gaps

Since 1.14.69, the hypothesis producer can review missing knowledge applicability
for the live profile's exact capability revision and provider bindings. It uses
the installed Hermes tool-free transport and current profile configuration;
neither model name nor deployment path is embedded in the review.

Eligible knowledge must be active in the exact scope, paper-backed and tied to
a durable intake approval and the existing registry trust threshold. Canonical
text and source artifacts must verify. Existing capability context declarations,
temporal validity and environment constraints cannot be ignored to create a link.
The review binds an input digest and exact revision and must quote real text from
the bounded canonical window. Its `knowledge.gap_applicability_review.v1` receipt
keeps quote offsets, lengths and hashes, source/review/revision digests and
applicability; it does not copy quoted text into the receipt.

Inspect the producer's `knowledge_link_reviews` for `eligible_count`,
`reviewed_count`, `completed_count`, `deferred_count`, `applicable_count`, `review_receipt_ids`, `links` and
`reason_counts`. No transport, unapproved or untrusted sources, missing canonical
anchors, mismatched contexts and ambiguous applicable evidence retain explicit
non-linking outcomes. A typed applicable link is evidence, not write authority.
Each invocation reviews at most four previously unreviewed inputs and reuses
completed receipts. It registers a unique link only after every eligible input
has a terminal result; remaining inputs cannot be discarded to claim uniqueness.

The live source, intake approval, registry trust, revision, bindings and canonical
digest are rechecked before accepting a review and when consuming its link.
Append-only review revocation prevents unchanged inputs from reviving the receipt.
Changed inputs require fresh valid evidence. Without registered writable candidate
bounds and a matching trusted machine policy, hypotheses remain evaluation-only.

Version 1.14.70 advances the full inventory in bounded persistent pages and
retains progress across invocations. Partial scans cannot certify that the first
eligible source is unique; review and link creation must account for the complete
corpus and current input changes. It keeps the existing trust, exact revision,
canonical evidence and revocation checks. The reported 1.14.69
`knowledge_inventory_truncated` is a workflow blocker, not evidence that more
incoming conversations alone will complete applicability.

The `knowledge.gap_inventory_scan.v1` scan uses stable storage-key pages,
an exact-scope change watermark and a persistent dependency manifest. A target
invocation reads at most 200 compact inventory rows, hydrates at most 200 dirty
entries and makes at most four model reviews. It continues across invocations
rather than raising the first-page limit or restarting every night. Late inserts,
including lower-key records, updates, deletes, scope moves, source/approval
changes and review revocations invalidate affected evidence and require review.
Target/revision/binding/environment or substantive trust changes select a new
target scan; bookkeeping timestamps do not supply new authority.

A unique link requires complete base enumeration, reconciliation through the
current change watermark and current terminal outcomes for every eligible entry,
with no unknown or pending evidence. Unknown source artifacts remain unresolved,
not permanently excluded as ineligible. Temporal transitions schedule rechecks;
canonical inputs are verified again when the selected link is consumed. This
scan creates neither trusted recall labels nor permission to apply a patch.

The native producer exposes each scan under
`knowledge_link_reviews[].inventory`:

| Field | Meaning |
| --- | --- |
| `schema`, `scan_key`, `cursor_digest` | Scan contract and digest-bound target/progress identity; no raw cursor or scope identity is copied |
| `inventory_complete`, `changes_complete`, `complete` | Base enumeration, change reconciliation and current certification; a complete base alone cannot establish uniqueness |
| `page_record_count`, `revalidated_record_count`, `manifest_record_count` | This invocation's compact page and dirty-entry work, plus accumulated inventory coverage |
| `pending_count`, `unknown_count` | Remaining dirty or unresolved evidence; either prevents certification |
| `input_watermark`, `current_watermark` | Applied and current exact-scope change versions; a mismatch invalidates completed evidence |
| `next_transition`, `certificate_ref` | Scheduled temporal revalidation and the native complete-inventory certificate, when actually issued |

Missing legacy fields remain unknown. Enumeration/change/review pending reasons
describe different blockers. Consumption rechecks the complete certificate and
watermark as well as the chosen source, so a new second applicable source cannot
silently preserve the earlier claim of uniqueness.

## Safe provider failure diagnostics

Since 1.14.70, Hermes completion timing retains an allowlisted
`provider_failure.v1` diagnostic for provider-request failures. `failure_class`
distinguishes authentication, permission, rate limiting, rejected requests,
server errors, timeouts, connection errors and unknown failures. The SDK's typed
attributes supply `http_status`, allowlisted `sdk_error_type` and `retry_after_ms`
when available. The bridge records `retryable`, `bridge_attempt_count`,
`retry_stop` and `outcome` from classification and actual control flow. The
review-failure projection preserves this object under
`unavailable_records[].completion_timing.provider_failure`; native current
failures keep it under `review_failure.completion_timing.provider_failure`.
Validated recovered completions retain `review_completion_timing.provider_failure` and
native `reviewed_records[].completion_timing.provider_failure`, with
`outcome=recovered`. This does not establish the review verdict. Missing
details remain unknown. Raw exception text, provider response bodies, endpoint
URLs, headers and credentials are not copied into receipts.

When this request retries, `provider_failure.first_failure` retains one bounded,
single-layer object for its first actual SDK failure. If both calls fail, the
main error/HTTP status and SDK type describe the final failure; the first object
preserves the earlier failure separately. A validated recovery still retains
that first failure. Neither object is populated from historical `business_meta`,
and missing fields are not reconstructed.

Transient retry can make at most one additional bridge call under the original
deadline, with the same client, provider, model and wire payload. It requires
a verified SDK retry policy of zero; existing or unknown SDK retries receive
no multiplying outer retry. Non-retryable failures, insufficient deadline,
oversized retry delay and changed provider identity stop explicitly. A recovered
transport still must pass response/model and review-schema validation; retry
cannot turn an unavailable or invalid review into an approval.
Client cleanup failure records `client_cleanup_failed` and keeps `outcome=failed`.
Receiving an SDK response therefore does not establish valid review completion.

`bridge_attempt_count` counts bridge calls, not hidden HTTP attempts. The
reported 1.14.69 `command_completion_failed:provider_request_failed` has no
measured underlying category in the supplied readback. These new fields cannot
retroactively identify that failure as network, configuration or quota trouble.

## Hermes v2 known-gap maintenance

The 1.14.69 official Hermes v2 route constructs
`dynamic_capability_patch_context.v1` for incident class
`capability.gap_maintenance`. It binds the hypothesis, applicable link, exact
revision/binding, complete independent evaluation suite and trusted repository
coordinates. A passed result dict or model-generated command is insufficient.

Writable bounds require the intersection of the registered immutable contract,
protected `capability.gap-maintenance.v1` test plan and a validated v2 machine
policy. The policy pins the exact provider implementation, repository/base/tree,
allowed files, limits and permitted effects. Unsupported targets and absent or
stale policy remain blocked. Known profile gaps and user-reported problems are
maintenance provenance: `qualifies_for_product_completion=false`; they do not
certify autonomous discovery or autonomous L5.

The supported bound resolver reports `status=bounded_write_candidate` with
`max_changes`, `allowed_file_paths`, byte/line limits, test-plan and registry
contract digests, provider binding, implementation digest and machine authority
digest. Capacity comes from the immutable code-implementation v2 resource and
official provider binding limits; actual paths also require machine-policy and
protected-plan permission. Existing zero-write hypotheses keep their original
bounds. A gap review cannot rewrite those bounds or create a machine policy.

The proposal goes through the official provider bridge and attestation checks.
The existing `promotion_manager` remains the single transaction/effect owner:
isolated candidate verification includes protected focused, regression and full
suite phases before any permitted forward effects. The model cannot replace the
test plan, broaden paths or grant commit, push or deployment authority.

The proposal retains a digest-bound durable context. Hypothesis/link revocation,
changed evidence, missing independent artifacts and changed policy are rechecked
at forward-effect and recovery boundaries. Loss of authority prevents further
forward effects while allowing the existing rollback path. Existing active or
quarantined transactions and consumed policy grants cannot be bypassed by
constructing another context. These code contracts establish no new production
transaction, deployment, business-quality verdict or successful evolution run.

## Nightly production-case collection diagnostics

Since 1.14.68, durable supervisor receipts retain
`nightly_diagnostics.production_recall_collection` with schema
`production_collection_diagnostics.v1`. These fields describe this run's native
collection result and its exact review associations; they do not certify quality.

| Field | Meaning |
| --- | --- |
| `status`, `enabled` | Reported collector outcome and policy enablement; absent legacy reports use `not_reported` and unknown values |
| `decision_count`, `new_count`, `existing_count`, `empty_result_count` | Native batch counters; already captured decisions remain separate from newly created cases |
| `new_nonempty_count`, `new_empty_count` | Candidate availability among this batch's newly created cases only |
| `skipped_reason_counts`, `skipped_reasons_truncated` | Bounded allowlisted skip counts; missing input is unknown, not an empty map or zero |
| `new_pending_record_ids`, `new_pending_record_ids_truncated` | At most 50 native new-case IDs, with new cases having non-empty candidates prioritized across the full collection batch |
| `new_case_reviews[]` | A new pending ID joined to this run's review results by that exact ID; no full-store or time-window inference |
| `review_results_status`, `review_results_truncated`, `review_dry_run` | Whether review results were provided, whether their bounded window was truncated, and whether review was dry-run |
| `scope_resolution.rewritten`, `scope_resolution.reason` | Allowlisted scope-resolution outcome without copying user or tenant identities |

The collector supplies bounded `new_case_refs` alongside the selected IDs,
including `capture_ref` and final persisted `candidate_count`. The diagnostic
uses them to populate `new_case_reviews`. Each entry retains these fields and
native `review_receipt_id` / `accepted_record_id` references when provided.
Reported review states are `pass`, `fail`, `already_accepted`, `not_reported`
or `ambiguous`; missing or invalid producer fields remain unknown or redacted.
Multiple matching review rows are ambiguous and cannot supply a chosen verdict.
Reused acceptance is distinguished from a newly created acceptance receipt.

Allowlisted `reasons`, `recall_status`, `review_complete`, `signal_counts` and
semantic `status` / `verdict` help locate the first actual blocker. Semantic
verdicts include `relevant`, `off_topic`, `mixed` and `unknown`. Signal counts
separate candidates, delivery, semantic relevance, verified proof and host
`used/rejected` feedback. `signal_checks_complete` distinguishes a complete
signal check from early capture rejection or an off-topic conclusion: candidate
counts remain available if checked, while unchecked later signals stay `null`
rather than appearing as initialized zeros. A missing signal stays `null`; it
is not a measured zero. Semantic failure/unavailability cannot become approval.
Follow `capture_ref` through the authorized decision read path for item state,
injection and render-format diagnostics; those fields are not inferred here.

The projection records at most 50 new cases, scans at most 1,500 review results
and retains at most 100 reasons per joined result and 100 skip categories.
Upstream ID/result truncation remains explicit; missing legacy collections or
review lists cannot be backfilled from other records. Query text, memory bodies,
identity values and credentials are excluded. These limits bound diagnostics,
not collection or review eligibility.

The operator's 1.14.67 `ref_5df3562505ca` and original logs lack the collection
section. Its `recall_semantic_relevance.new_count=9` counts semantic observations,
so it cannot substitute for this batch's new-case count. Repeated reviews of
existing captures likewise cannot establish new case arrival. The diagnostic
supports a trace from one real new case to its recorded verdict; it does not
authorize a label, prove successful use, certify correct no-answer or establish
that accepted cases have advanced from zero to one. No production rerun is
reported or required by this documentation update.

## Nightly review receipt references

Since 1.14.67, `recall_label_auto_review.not_passed_review_references` in the
durable nightly diagnostics has schema `production_review_reason_refs.v1`.
`reason_refs[reason].refs` retains up to five exact `review_receipt_id` /
`pending_record_id` pairs emitted by this run's automatic reviewer. Each reason
has its own window, so common failures cannot crowd out a rare `semantic_off_topic`.
Read the native receipt, validate its owning scope and reason, then follow its
pending case's `capture_ref`. This is a diagnostic pointer, not new label authority.

`observed_result_count` counts scanned failing results for that reason, deduped
within each result. `reference_count` counts unique recorded ID pairs;
`missing_reference_count` covers missing or invalid native IDs.
`references_truncated` discloses the five-pair cap. The projection scans at most
1,500 results and 100 reasons per result, with `results_scanned_count`,
`results_truncated` and `result_reasons_truncated`. Unknown reason text is
redacted. Aggregate reason counters keep their existing meaning and may overlap.
Absent legacy result lists report `status=not_reported` with unknown scan counts;
missing pointers are not reconstructed from time windows or matching reason codes.

The operator's 1.14.66 report `ref_12f098002823` and its original nightly logs
have only reason counters, so that report's single `semantic_off_topic` source
remains unknown. Two older off-topic observations cannot identify it. New
references establish only what they actually retain; unchanged gates still
require trusted labels, release authority and independent quality evidence.

## Session deduplication provenance

Since 1.14.66, new proactive decisions retain
`retrieval_diagnostics.session_dedupe` (`proactive_session_dedupe.v1`). Use the
evaluation case's `capture_ref` to read its exact authorized decision, then
inspect this object alongside `post_selection.session_deduped_count`:

| Field | Meaning |
| --- | --- |
| `observed_at` | UTC timestamp at the ledger read; not an injection timestamp |
| `input_count`, `removed_count` | Unique voluntary candidates entering and removed by session deduplication |
| `recorded_count`, `truncated`, `missing_match_count` | Recorded witnesses, whether the 64-witness cap omitted candidates, and any missing witness within that window |
| `matches[].record_id`, `source_id` | Exact removed candidate identity |
| `prior_decision_id`, `prior_citation` | One qualifying historical decision/item per removed candidate |
| `prior_state`, `prior_ever_injected`, `prior_terminal` | Historical item/decision state observed in the same SQLite snapshot as the dedupe set |
| `prior_decision_created_at`, `prior_item_updated_at` | Historical creation and last-update timestamps; last update may be a terminal transition |
| `basis` | `ever_injected`, `host_used`, `host_rejected`, `injected` or `in_flight` |

The witness is the newest qualifying historical item by decision creation time,
decision ID, item order and citation. It uses the same channel, exact scope,
source-set key, session and record/source identity as the original predicate.
It does not list every historical occurrence. The diagnostic cap does not
limit deduplication; an omitted witness cannot be called individually verified.
The snapshot records eligibility at the read even if another process closes an
in-flight decision immediately afterwards. Historical ledger retention may
later remove a referenced decision; its observed fields remain in this audit.

`ever_injected=true` with `prior_state=not_used` means the model was given the
item, without recorded actual use. `in_flight` describes an open reservation,
not confirmed delivery. A closed item that was never injected and has no
host `used/rejected` feedback stays eligible. Mandatory policy context bypasses
session deduplication. Query, title and memory body are excluded from the audit.

For an empty current decision, consult this audit to distinguish session
suppression from no matching evidence. Historical witnesses are never inserted
into current `candidate_refs`; an automatic `no_candidate_refs` review closes
as not evaluable and does not certify correct no-answer. Old count-only decisions
cannot gain exact candidate/history links retroactively. Keep the old four
audits separate from any newly observed cases; see the
[operator readback](acceptance-status.md#session-deduplication-readback).

## RPC security

Production loads `/etc/eimemory/rpc.env`, which contains the strong server
secret `EIMEMORY_RPC_AUTH_TOKEN` plus, when enabled, only non-secret paths and
profile markers for attestation. Clients receive the runtime secret through
their own secret manager as `EIMEMORY_RPC_TOKEN`; never print, commit, or copy
the token into plugin files.

The production service binds RPC to the honxin Tailscale address on port 8091
and exposes a separate loopback health probe. Check it without revealing auth:

```bash
systemctl --user status eimemory-rpc.service --no-pager
curl -fsS http://127.0.0.1:8091/health
```

Client profile environment:

```text
EIMEMORY_RPC_URL=http://honxin:8091/
EIMEMORY_RPC_TOKEN=<secret supplied out of band>
EIMEMORY_TENANT_ID=default
EIMEMORY_AGENT_ID=hongtu
EIMEMORY_WORKSPACE_ID=embodied
EIMEMORY_USER_ID=<stable user identity>
EIMEMORY_ADAPTER_TIMEOUT_SECONDS=<bounded host-specific timeout>
```

The maintained production settings use a 5-second Hermes adapter budget and a
3.5-second managed OpenClaw hook budget. Do not restore the former sub-second
default: production recall commonly needs several seconds, and too-small host
deadlines create false bypasses rather than useful safety.

### Optional fail-closed task attestation

Codex/Hermes task receipts require an operator-separated producer boundary;
same-UID file permissions alone do not make a producer model-unforgeable. The
RPC service reads producer credentials only from a private JSON file shaped as
`{"codex":"<token>","hermes":"<token>"}`. Its environment contains only the
file path and explicit profile marker:

```text
EIMEMORY_ATTESTATION_HOST_PROFILE=operator-separated-v1
EIMEMORY_ATTESTATION_TOKENS_FILE=/etc/eimemory/attestation-producers.json
```

The producer tokens must be strong, distinct from each other and from
`EIMEMORY_RPC_AUTH_TOKEN`, and the file must be a regular non-symlink private
file (mode `0600` on POSIX). Codex and Hermes host integrations receive only
their channel-specific private token-file path. Do not put a producer token or
token-file path in the ordinary model/tool environment. For Codex, enforce the
corresponding `shell_environment_policy` stripping `KEY`, `SECRET`, and
`TOKEN` variables from model-launched commands.

The local receipt handoff file contains receipt IDs only and is not a trust
boundary. The runtime database joins and atomically claims the exact pending
receipt set. Check `eimemory_status.attestation_available` and
`attestation_reason`; when the profile is absent or malformed, receipt issuance
is unavailable and those tasks cannot count toward L5, while memory/recall
remain fail-open.

## Codex

Install the matching eimemory Python package in the environment where the
`eimemory` executable is visible. Register the repository marketplace:

```powershell
codex plugin marketplace add E:\eimemory\integrations\codex
codex plugin marketplace list
```

Restart the desktop app, install **eimemory** from the `eimemory-adapters`
marketplace, review/trust its hooks, and start a new Codex task. The plugin
provides `SessionStart`, `UserPromptSubmit`, `PostToolUse`, and `Stop` hooks plus
`eimemory_recall`, `eimemory_remember`, `eimemory_verify_outcome`, and
`eimemory_status` MCP tools.

Use `eimemory_status` to verify `channel=codex`, `authority_mode=per_channel`,
the `embodied::channel::codex` scope, and current release identity. To disable,
turn the plugin off in the Plugins Directory. To remove its local marketplace:

```powershell
codex plugin marketplace remove eimemory-adapters
```

The plugin never reads `transcript_path`. It sends bounded prompt context and
redacted/digested tool summaries only. `Stop` always continues Codex and never
invents verification.

## Hermes Agent

Copy the standalone provider into the active Hermes home and ensure the Hermes
Python environment can import the matching eimemory package:

```bash
mkdir -p "$HERMES_HOME/plugins/eimemory"
cp -R integrations/hermes/eimemory/. "$HERMES_HOME/plugins/eimemory/"
mkdir -p "$HERMES_HOME/plugins/eimemory_hook"
cp -R integrations/hermes/eimemory_hook/. "$HERMES_HOME/plugins/eimemory_hook/"
```

Select the provider in `$HERMES_HOME/config.yaml`:

```yaml
memory:
  provider: eimemory
plugins:
  enabled:
    - eimemory-hook
```

Or enable the host callbacks explicitly:

```bash
hermes plugins enable eimemory-hook
```

Start a new session, then call `eimemory_status` and verify `channel=hermes`,
`authority_mode=per_channel`, `embodied::channel::hermes`, and current release
identity. A production immutable deployment also provisions the separated
Hermes producer token, restarts `hermes-gateway.service`, and runs
`deploy/verify_hermes_integration.py`. That replay must prove the official
loader/provider binding, all three callbacks, authoritative remember/recall,
an actual pytest tool result, terminal receipt consumption, and completed-turn
sync. Disable the integration with:

```bash
hermes memory off
hermes plugins disable eimemory-hook
```

Hermes uses one bounded background writer and one latest-wins prefetch worker.
It ignores full history arguments, mirrors only accepted bounded turns and
built-in memory writes, and skips all writes in cron, flush, and subagent
contexts.

## Fail-open and evidence rules

Both adapters are fail-open. Timeout, authentication failure, network failure,
or an open circuit returns a stable bypass result and never stops the host:

```json
{"ok": false, "bypassed": true, "error": "adapter_unavailable", "result": null}
```

The bounded failure ledger contains timestamps, method names, and error classes
only; it never contains the RPC token or full host transcripts. Fix the service
and use `eimemory_status` to confirm recovery.

Codex `Stop` and Hermes `task_end` can enter the verified-real-task pipeline
only when the terminal transaction consumes the exact protected set of
eligible, channel-specific v2 receipts joined from the runtime database. The
verification text is diagnostic only; the service may replace it with a
receipt label, but prose is never the trust gate. Unverified successes and all
`session_end` events remain diagnostic/lifecycle evidence and cannot raise L5.

## Live closure checklist

1. `/health` version, 40-character commit, import root, and release path agree.
2. The installed `eimemory.capability_catalog.bootstrap.v1` entry point resolves
   from the intended trusted distribution and the catalog seals successfully.
3. The selected profile resolves each active provider binding to its own catalog
   case; no ambiguous or cross-provider case is accepted.
4. `eimemory_status` succeeds independently in Codex and Hermes.
5. A unique memory written in one channel is recalled there and absent from the
   other two channels.
6. A verified task appears only in that channel's current-deployment metrics.
7. An unverified success and a session end do not change verified-task counts.
8. Capability acceptance produces distinct runs and observations for Hermes and
   OpenClaw; each binding reaches its profile threshold independently.
9. `learn l5-readiness --reader-mode v3 --profile l5.default` reports all four
   axes separately; do not infer readiness from `/health`.
10. Stop the RPC briefly and confirm both hosts continue with fail-open bypass;
   restore it and confirm status recovery.
11. Re-run readiness and confirm the existing dynamic closure remains green.
