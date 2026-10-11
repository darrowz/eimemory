# Receipt-based acceptance status

Documentation updated on 2026-10-11. This page transcribes the operator's
latest **1.14.66 / `2587331f`** nightly and diagnostic readbacks, alongside
earlier release evidence, including the failed 1.14.64 deployment.
Earlier session-deduplication audits remain historical; their count-only
records cannot inherit the exact provenance supplied for the new sample.
The documentation publisher has not
independently queried the production database or rerun production acceptance.

**The operator reports successful 1.14.66 deployment and nightly execution.
Formal recall quality remains insufficient, with 0/3 accepted cases, an empty
memory benchmark and no evolution candidates. Team recall remains unverified.**

## Latest 1.14.66 nightly and traceability readback

Bound commit: `2587331f26062ceba5a35b71852b43c94c76991a`.
Nightly `ref_12f098002823` ran 22:05:39–22:14:44 (reported times; receipt
timezone not separately supplied), bound to deployment `rec_f7c085d6a768`.
Exit 0, `Result=success`, `execution_ok=true`, no failed steps. Produced 6,045,
promoted 0, rolled back 55; rollback is a task count, not a release rollback.

Formal `recall_quality_accepted=false`, evidence `insufficient`. Ten known-item
smoke samples executed with no missing/invalid metrics, but trusted labels and
release authority did not pass validation. The readback supplied no new smoke
accuracy values. Accepted cases are 0/3. All 343 reviews completed: 0 passes,
327 not-evaluable closures, 5 closures without a label, 11 rejections, 0 pending
or open reviews. Reasons include `pending_capture_decision_missing=186`,
`no_candidate_refs=135`, `pending_capture_boundary_invalid=10` and
`semantic_off_topic=1`; reasons may overlap. Memory source `replay_dataset`
has zero retrieval cases, `memory_eval_dataset_empty`, and no run. Nine
evolution results have zero hypotheses, `hypothesis_missing_or_ambiguous`.

One real new dedupe decision, `pd:1cdce141f8f3ec2889ed263a46d223e5`, binds to
1.14.66 and the full commit above. It removed one candidate and recorded one
witness, with no truncation or missing match. Candidate
`mem_15ff033f0c1661a1e27406c7d99fa122` / `hermes` links to prior decision
`pd:c5c91b7c5ce3c4027e615c3aa71f2619`, citation `pm:7b21cefa55ab74866747`,
`not_used`, ever injected and terminal, basis `ever_injected`. This confirms
the diagnostic on that new sample, not the old four count-only decisions.

The nightly report has `evidence=[]`, `links=[]`, empty `detail`, and no
`review_results`, `review_receipt_id` or `pending_record_id`. Original nightly
logs also retain only counters. Its unique off-topic count therefore cannot
identify a review receipt, pending case, semantic observation or decision.
This is a **report traceability gap**, not proof of an algorithm regression.
Full-store observations `rec_66d6e1e8b9fc` (1.14.27) and `rec_cc1802dbad8a`
(1.14.58) are distinct historical observations; neither is certified as this
run's source. Mainline 1.14.67 adds bounded native receipt pointers for new
summaries without reconstructing this history or changing verdicts or gates.
All production readbacks in this section were read-only, without reruns.

## Earlier failed October 11 deployment attempt: 1.14.64

Source: the operator's final read-only receipt and task-state readback for full
commit `f61487e2d1d85ca291a240086fc70aeb921669a3`.

- The current release link switched to the target. The source integrity check
  passed, with zero bytecode files. These checks do not establish RPC readiness.
- Final receipt completed at **20:16:31**, as reported. Installer and binding
  refresh exit codes are **0**; the service is reported **`active`**.
- Health acceptance failed: **`health_exit_code=1`**, **`error=TimeoutError`**,
  final **`ok=false`**. Earlier observed health requests timed out at 10 and
  30 seconds. Timeout does not establish a crashed process or a specific cause.
- Closure summary: `blocked_stage=deployment_receipt`,
  `blocked_reason=health_fetch_failed`, `execution.closure_exit_status=1`,
  `business_closure_outcome=failed`. Hard errors contain `health_fetch_failed`
  at both `$.blocked_reason` and `$.deployment_receipt.error`; these are two
  references to the reported condition, not two independently identified causes.
- Last reported log stage: `binding_refresh=verified`, `scope_count=2`.
  **2026-10-11 20:16:32 +08:00** is the log file modification time, not an
  independent timestamp on that line. The final receipt time's timezone field
  was not separately supplied.
- Final handoff has `deploy_exit=1` and `deployment_ok=false`, so **nightly
  did not start**. The previously pending receipts have now finalized.
- No new nightly or quality report was supplied. The database's latest nightly
  remains `ref_840ade0e6fbf` from 1.14.63; it cannot accept this candidate.
- The operator only read receipts and task state; no rerun, restart or new
  health request was performed for the final readback.

Mainline 1.14.65 bounds waits for migration and optional candidate-health
snapshots, uses one in-flight reader per fragment, isolates caches per runtime,
and refuses readiness for unavailable migration status. Authenticated liveness
does not call these readers. Local SQLite contention reproduces the old request
timeout; the production request's underlying cause is still unconfirmed.
No 1.14.65 deployment or production recovery is reported.

## Session deduplication readback

Source: the operator's read-only trace of these four fixed previous decisions.
No rerun, production modification, query or memory body was supplied.

| Decision | Deduped candidates |
| --- | ---: |
| `pd:5c4e2e49b153777cd5e108f5e7307d80` | 1 |
| `pd:9b2bf1efc83d7fbef7ef80ef6d0181bc` | 1 |
| `pd:1310a6e43da8e3ab266a2ede496d6bbe` | 1 |
| `pd:0a393689845a99d5f485064e17b1bd75` | 2 |

Each decision's channel, authorized scope, source, session and preceding time
window contains historical items with `ever_injected=1`, closed as `not_used`.
That state means previously injected without recorded actual use. The ledger
also admits host `used/rejected` items and still-open volunteered reservations;
closed never-injected items without such host feedback do not suppress recall.
No defect in these rules has been established.

These old audits retain counts only, so the **five removed candidates across
four decisions cannot be matched individually** to that historical delivery.
Same-session delivery is not a 4/4 exact verification. Mainline 1.14.66 adds
[candidate/history witnesses](operations.md#session-deduplication-provenance)
for new decisions without reconstructing the missing historical IDs or changing
deduplication, ranking or quality gates. No production execution of this new
diagnostic path has been reported.

The operator found genuine terminal receipt
`pret_107d2400930d19d8fc15c6a33d9b43f4`: `disposition=not_evaluable`,
`close_reason=no_recall`, `recall_status=no_recall`,
`answer_quality=not_evaluable`, `does_not_certify_answer=true`,
`review_complete=true`, `reasons=["no_candidate_refs"]`.
This corrects the earlier characterization of that case as pending: its review
is complete, closed without a label; the original collection record remains.
The collector uses only the current decision's final persisted candidates,
without copying historical delivery into a positive label. No independently
justified no-answer case was verified end to end by this readback.

The audit does not establish a recall algorithm fault, current service health,
a newer nightly, formal business acceptance or team-wide recall accuracy.

## Earlier completed October 11 deployment and nightly: 1.14.63

Source: the operator's readback of persisted receipts bound to full commit
`4fbcbbc99e228c753c2e0e255b7bad264a98c01a`. These historical results remain bound
to that runtime identity; they do not describe the subsequent candidate switch.

- Deployment completed October 11, 2026 at 12:09:26. Association receipt:
  `rec_c37c15ee113e`; `ok=true`, install, binding refresh and health checks exited 0.
- At that readback, the release path matched the receipt; RPC process, storage
  and readiness were reported healthy.
- Nightly ran 12:09:26–12:17:36, exited **0**, with `execution_ok=true` and
  **no failed steps**. Produced **5,981**, promoted **10**, rolled back **54**.
- Nightly receipt: `ref_840ade0e6fbf`; quality report: `ref_6ae80df64e32`.
- Smoke **10/10**, failed **0**, error rate **0.00**, hit@1 and hit@5 **1.00**.
- Formal gate remains **`gate_ok=false`**, with
  **`recall_quality_evidence_incomplete`**; accepted cases remain **0/15**.
- Label review processed **321**, passed **0**, pending **0**. Detailed current
  reason counts were not supplied in this initial readback; older counts are not
  assigned to this run.
- Memory benchmark has **0 retrieval cases**, `memory_eval_dataset_empty`, and
  did not run. Current dataset-source field was not supplied in the initial readback.
- Dynamic evolution has **nine results**, each with candidate hypothesis count
  **0**. The producer lacks an applicable knowledge association:
  `no_applicable_knowledge_link_for_gap_revision`.
- Times are the operator's reported times. The receipt's timezone field was
  not supplied.
- The operator only read existing results, without a rerun or production change.

The preceding smoke error-rate violation did not recur, and the gate reason is
now evidence insufficiency. This confirms the reported smoke recovery on
1.14.63, without establishing natural-query or colleague-wide quality. No
per-case identity comparison or historical candidate/stage trace was supplied;
smoke recovery alone cannot prove where the older target was lost or that an
identical case set was rerun. Formal labels, a retrieval benchmark and eligible
capability knowledge links remain necessary under the existing gates.

## Team recall in actual Feishu conversations

The operator additionally clarified that **鸿泰、鸿欣、小马哥 have no completed
actual-conversation recall acceptance**. Earlier controlled checks returned
empty results, but the session identity and environment were not fully
verified. Those observations do not establish that any participant currently
has a recall fault. Shared-service checks and the historical 10/10 smoke do not
establish accurate recall for those participants either.

Current status: **1.14.66 deployment and nightly execution are reported successful;
formal cases remain 0/3, and natural-question recall under each colleague's actual identity
remains unverified**. Verify the
actual host, effective scope and environment first, then cover known memories
with natural questions, no-answer near neighbors, cross-member permission
boundaries and authorized source readback. Keep participant-level persisted
results separate from aggregate smoke and each release's exact formal policy.
See the [team recall acceptance guide](team-recall-acceptance.md). No team
acceptance was executed or certified by this documentation update.

The historical 1.14.63 run uses `production_recall_gate_policy.v4`, which
requires at least 15 cases and 15 labels overall and has no fixed per-client
quota. Its coverage is limited to observed dataset channels, not all installed
or enabled hosts. The previous documentation's three-client five-case rule was
incorrect for this runtime. A qualifying Hermes-only dataset can satisfy the
coverage prerequisite; this does not establish that all team use is Hermes-only
or that every participant was tested. Keep actual team usage, dataset acceptance
and cross-client product coverage separate. The documentation correction itself
did not change that run's policy.

## Mainline 1.14.64: explicitly requested three-case floor

The operator subsequently requested **3 cases / 3 labels**. Mainline 1.14.64
implements `production_recall_gate_policy.v5` with that floor in each dataset's
existing exact scope. Label trust, release authorization, source/scope boundaries,
ranking, resources, isolation and noninferiority remain required. Coverage stays
limited to observed dataset hosts; it grants no cross-member access.

The historical completed nightly on **1.14.63 / `4fbcbbc9`** reports **0/15**
accepted cases under its historical v4 rule. Do not relabel that result as 0/3
or use it to accept the failed 1.14.64 deployment attempt. Historical policy
payloads and digests keep their original floors; old receipts cannot become
accepted by changing their policy tag. No 1.14.64 nightly result is supplied.

Existing authorized memories can provide controlled positive targets, source
readback and contrast probes; they do not automatically become trusted labels
or natural-production cases. See [the four-check procedure](team-recall-acceptance.md#using-existing-memory-for-four-checks).

## Earlier October 11 deployment and nightly: 1.14.62

Source: the operator's persisted-receipt readback for full commit
`82cba75afcea7e50a8fee758ee24b03f5b286717`. These results belong to this deployed
commit, not a later documentation or diagnostic patch.

- Deployment completed October 11, 2026 at 10:36:45 (reported time; receipt
  timezone unspecified). Association receipt: `rec_710320cdd83e`.
- Deployment `ok=true`; install, binding refresh and health checks all exited 0.
  The live release path matches the receipt; RPC process, storage and readiness
  are reported healthy.
- Nightly ran 10:36:46–10:45:00, exited **1**, and produced 5,897, promoted 10,
  rolled back 53. Persisted reference: `ref_e8739d53bb13`.
- Only failed step: **`recall_quality_gate`**.
- Quality report `ref_a2dee7107fde`: smoke **9/10**, blocked with
  **`recall_quality_gate_failed`**, error rate **0.10 > 0.05** (required ≤0.05).
- Follow-up diagnostic readback: numerator **1**, denominator **10**;
  `known_item_miss_with_return_count=1`, `no_answer_return_count=0`.
  The failed case used topk 5, returned two items, was non-empty, matched no
  expected item, and had smoke `noise_rate=1.0`. Expected/actual grouping was
  the operator's presentation of fields, not an original nested report object.
  Case ID: `generated-digest_bc2b3a1308f3d795`; returned IDs:
  `ktop_f52653534923ea69`, `digest_76552a58fd3bba5d`.
- Formal accepted cases **0/15**; automatic review processed **312**, passed **0**.
  A pending-count field, model identity and new authorization evidence were not
  supplied for this run; they must not be copied from historical runs.
- Memory benchmark: **0 retrieval cases**, `memory_eval_dataset_empty`;
  current dataset source was not supplied.
- Dynamic evolution: **nine results**, all candidate hypothesis counts **0**.
  No current producer-status/skip details were supplied.
- The operator reports no repeated deployment or rerun.

Unlike the earlier evidence-only block, this run has an explicit measured
violation that fails the gate. The metric combines known-item misses with a
return and erroneous returns on no-answer cases; this run's violation is the
first kind. It does not establish no-answer or colleague-wide natural-query
quality. Missing formal evidence remains a separate blocker. Retrieval root
cause in the actual run is not yet confirmed; changing the metric, expected
item or threshold cannot establish a repair.

Additional operator readback compares the target (digest, October 10, 256
distinct source IDs), the returned digest (October 11, the same 256-source set),
and the returned topic (one source ID, different set, no digest date). The two
digests have identical first 220 summary characters, both at record top level
and in `content.summary`; the topic's prefix differs from both. This confirms
the collision's input shape. It does not prove that the historical execution
lost the target at deduplication without that run's candidate/stage trace.

The classification repair is confirmed by that 1.14.62 persisted nightly summary:

| Directly preserved code | Reported occurrences |
| --- | ---: |
| `pending_capture_boundary_invalid` | 10 |
| `pending_capture_decision_missing` | 186 |
| `semantic_judgment_not_delivered` | 6 |

The third count is **6 in this run**, versus **5 in the earlier 1.14.61 run**.
These codes no longer collapse to `reason_not_allowlisted`. Counts can overlap;
they do not identify the cause of the known-item miss.

## Earlier October 11 nightly: 1.14.61

Source: the operator's supplied readback, identifying **1.14.61 / `7a625fe1`**.
The repository resolves that abbreviation to
`7a625fe1a280862b5ef4c1680d5162efab848bcb`; a full commit field from the receipt
and a new deployment receipt were not supplied.

- October 11, 2026, 03:30:00–03:37:46, as reported by the operator. The client
  timezone is Asia/Shanghai; the receipt's own timezone field was not supplied.
- Exit code `0`, `execution_ok=true`, no failed steps.
- Produced 5,776; promoted 10; rolled back 52.
- Persisted nightly reference: `ref_a650963881f2`.
- Current RPC process, storage and readiness are reported healthy.
- Automatic label review enabled: 304 reviewed, 0 passed, 304 not passed,
  **0 pending**. Formal accepted cases remain **0/15**.

| Review reason | Reported occurrences |
| --- | ---: |
| `reason_not_allowlisted` | 201 |
| `no_candidate_refs` | 98 |
| `independent_signal_agreement_missing` | 9 |
| `no_candidate_delivered` | 5 |
| `semantic_off_topic` | 1 |

One case can have multiple distinct reasons. These counts cannot be added to
obtain a case total. A follow-up operator readback checked all 304 per-case
receipts and deduplicated each receipt's raw reasons. It supplied these codes:

| Underlying code | Reported occurrences |
| --- | ---: |
| `pending_capture_boundary_invalid` | 10 |
| `pending_capture_decision_missing` | 186 |
| `semantic_judgment_not_delivered` | 5 |

The operator confirmed the third count as **5**: **10 + 186 + 5 = 201**,
matching the nightly aggregate and involving 201 distinct receipts in this run.
No additional reason code is missing from this reconciliation.

The first is a capture-envelope/boundary rejection. The second means no
authoritative decision row matched the capture's exact owner/channel/source;
the code alone cannot distinguish pruning from a mismatch. The third means no
candidate was actually injected, so no semantic relevance is certified. These
are eligibility/evidence failures and do not establish a recall algorithm defect.
The collector already uses bounded retention pins for capture decisions;
missing historical evidence cannot be recreated or accepted by a diagnostic fix.

- Memory benchmark: `not_run`; source `replay_dataset`, retrieval cases **0**,
  reason `memory_eval_dataset_empty`. No accepted memory evaluation is reported.
- Hypothesis producer: `no_eligible_evidence`; one skipped gap with
  `no_applicable_knowledge_link_for_gap_revision`.
- Dynamic evolution: nine results, all with `candidate_hypothesis_count=0` and
  `hypothesis_missing_or_ambiguous`.
- The operator reports only reading existing results, with no rerun or modification.

Pending reviews are no longer the primary explanation. Current blockers are
non-passing reviews, an empty retrieval dataset and missing applicable hypothesis
evidence. No new smoke/ranking metrics, model identity or release-authorization
evidence were supplied. Older evidence gaps must not be silently filled from
this execution success.

## Earlier 1.14.60 deployment and nightly

Source: the operator's supplied readback. The deployed full commit is
`40285e36ab2e43d22fe20ee658af95eab4b5bcad`; the following results belong to that
exact release, rather than a later documentation commit.

- Deployment receipt: `ok=true`; installation, binding refresh and health checks
  succeeded. The live release path matches the receipt; RPC process, storage
  and readiness are reported healthy. A deployment receipt reference was not supplied.
- One additional nightly: 19:04:03–19:11:56, timezone unspecified.
- Exit code: `0`; `execution_ok=true`; no failed steps.
- Persisted nightly reference: `ref_3287829a4ae2`, bound to the full commit above.
- Produced: 5,683; promoted: 10; rolled back: 51.
- `research_closure_review` is no longer reported as failed. Actual review counts,
  verdicts and provider/model identity were not supplied in this readback.
- Formal recall cases: **0/15 accepted**, with **five pending review**. Evidence
  remains `insufficient`; label trust and release authorization are unverified.
- Additional automatic-label-review readback: `status=completed`, `enabled=true`,
  `pending_count=5`. Reason counts are five each for
  `independent_signal_agreement_missing`, `semantic_judgment_unknown` and
  `tool_free_transport_unavailable`. These counts were aggregated from persisted
  per-case reviews; they were not fields saved directly in the nightly summary.
- Memory benchmark: `not_run`, not accepted. Its specific skip/blocking reason
  was not supplied, so the earlier empty-dataset diagnosis is not assigned to this run.
  Associated receipt: `ref_d5b6b1ded019`; dataset source and retrieval-case count
  were not supplied in readable form.
- Dynamic evolution: **nine waiting items**, all with `hypothesis_missing_or_ambiguous`.
  `result_count=9`, `results_truncated=false`. The supplied reason-count fragment
  omits its numeric value; candidate counts and hypothesis-producer skip reasons
  were absent from the available persisted summary and journal.
- The operator reports one extra nightly, with no repeated deployment or restart.

Execution success closes the reported execution failure for this run. It does
not supply trusted production labels, a memory benchmark or accepted hypotheses.
No new smoke ranking or latency measurements were supplied for this release.

## Earlier 1.14.58 nightly failure

Source: the operator's screenshot and subsequent record readback report. The
operator identified the deployment as **1.14.58**, full commit
`534bb2f02562ed6cc45a7e3b8d87d59ff54a2aa1`. The production database was not
independently queried by this documentation publisher.

- Nightly started once automatically after a successful deployment receipt;
  the operator did not start it again.
- Run window: 15:21:17–15:27:25, timezone unspecified.
- Exit code: `1`; `execution_ok=false`.
- Persisted reference: `ref_4cc83efb2f5d`.
- Only reported failed step: `research_closure_review`.
- Produced: 5,620; promoted: 10; rolled back: 50.
- Formal cases: 0/15, with two pending review. Recall evidence remains
  `insufficient`; label trust and release authorization are unverified.
- Memory benchmark did not run; `memory_benchmark_accepted=false`.
- Ten evolution items wait with `hypothesis_missing_or_ambiguous`.

The operator confirmed that this durable nightly receipt did not save
`research_closure_review.unavailable_records`. Readback of 15 research records
in the execution window reported `review_error=research_review_llm_unconfigured`,
including `replay_0c66c27caffd` and `replay_2fa4d834f02a`. These are separately
read diagnostic fields, not a reconstructed unavailable-record list.

This establishes a missing research-specific/shared model command in that run.
It is separate from the earlier missing `codex` executable and dataset-path
permission restrictions. Configuring a working managed review bridge remains
necessary; package diagnostics and preflight checks do not supply a model route.

## Earlier 1.14.57 execution

- Run window: 10:54:37–11:01:18, as supplied by the operator; timezone unspecified.
- systemd exit code: `0`.
- Scheduler: `execution_ok=true`, with no failed execution steps.
- Produced: 5,645; promoted: 10; rolled back: 50.

Counts and exit status describe execution, not business acceptance.

## Earlier 1.14.57 recall evidence

| Evidence | Reported result | Acceptance interpretation |
| --- | --- | --- |
| Known-item smoke | 10/10; hit@1, hit@5, MRR all 1.00; P95 340.8 ms | Fixed known items are retrievable; natural-query and colleague-wide recall remain unproven |
| Formal gate | `gate_ok=false`; `recall_quality_evidence_incomplete` | Formal acceptance blocked by incomplete evidence |
| Accepted production cases | 0/15; another 2 pending review | Pending or generated cases do not count as accepted labels |
| Label trust / release authority | Smoke label trust not accepted; release authorization unverified | Neither success nor authorization may be inferred |
| Smoke noise | 0.80, compared with required ≤0.40 | Smoke observation only; no formal business score assigned in this run |
| Smoke precision@3 | 0.333, compared with required ≥0.60 | Smoke observation only; no formal business score assigned in this run |

The formal gate first stopped for missing evidence. It did not use the two
smoke ranking observations as a completed formal business evaluation.
The deployed 1.14.63 policy-v4 production dataset requires at least 15 cases and 15
accepted labels overall; observed dataset channels define the coverage scope,
with no fixed per-channel quota. This describes the current code contract,
without assigning an unreported policy field to the historical receipt.
The subsequently requested v5 minimum is three cases / three labels in mainline
1.14.64; it does not change this older receipt.
Acceptance must retain trustworthy
labels, exact owner/source/channel boundaries, release identity and the required
release authorization evidence. The complete contract is in the
[evaluation guide](evaluation.md#production-recall-gate).

## Other incomplete evidence in 1.14.57

- Memory CI was skipped with `memory_eval_dataset_empty`. A separate passing
  code-capability evaluation cannot substitute for retrieval benchmark evidence.
- Dynamic capability evolution has nine waiting items, all marked
  `hypothesis_missing_or_ambiguous`. Diagnostics cannot fabricate knowledge
  links, grant behavior authority or close unresolved hypotheses.
- Research closure could not find the `codex` executable; model review was
  unavailable. An unavailable reviewer cannot approve a candidate.

## Earlier 1.14.57 receipt references

| Receipt | Reference |
| --- | --- |
| Nightly | `ref_1993a5692b9a` |
| Deployment association | `rec_300a9cce017c` |
| Recall quality | `ref_41e1ea639d00` |
| Memory evaluation | `ref_9b97981f26e0` |

These are runtime references, not public links or credentials. The operator
reported reading them back from persistent storage. This page includes no raw
queries, memory payloads or private labels.

## Mainline workflow repairs

**1.14.61** records completed pass/fail conclusions for every scanned automatic
label review, retaining reasons and exact evidence identity. Missing semantic
review, insufficient agreement or unavailable approval results in **fail**,
without a relevance label or benchmark certification. Captures stay active and
later qualifying evidence can trigger a new review. Semantic and research
review share supported Hermes runtime discovery while the semantic judge retains
tool-free/read-only execution. New nightly diagnostics retain bounded conclusion
and dataset reasons plus reported hypothesis counts; absent values remain unknown.
The earlier October 11 readback reports execution on 1.14.61 and zero pending
reviews, with all 304 reviews non-passing. This does not certify business quality.

**1.14.62** expands the versioned safe reason catalog to include capture,
original-query, feature-quality, candidate, semantic and native acceptance
failures previously generalized by an incomplete list. Unknown arbitrary text
remains redacted and oversized reason maps disclose truncation. Closed/reopened
supervisor receipt tests verify the exact native reasons and unchanged label
eligibility. This diagnostic patch cannot recover raw reasons absent from an
older summary. The earlier 1.14.62 production readback confirms the classification
repair; deployment passed and the nightly quality gate failed.

**1.14.63** fixes digest/synthesis identity collisions by using full content,
date, supporting evidence and source identity instead of a truncated summary.
An isolated native smoke with two differently dated digests sharing the same
256-source set reproduces the known-item miss using the unchanged mainline
dedupe method; the repaired method retains the target with the same query,
expected ID and cutoff. Exact duplicates still collapse; source quotas and
default digest suppression remain unchanged.
It also preserves expected record IDs alongside returned IDs and explicit
`false_recall_count` / `false_recall_denominator` in diagnostic quality receipts.
Closed/reopened storage tests distinguish a non-empty known-item miss, a
no-answer return and an empty miss, retain raw-body redaction, and require the
same ≤0.05 gate and nightly failure behavior. The latest operator readback now
confirms deployed 1.14.63 execution and smoke 10/10, error rate 0.00. It does not
establish the historical loss stage, an identical case set or formal business acceptance.

The workflow repairs introduced in **1.14.58** are separate from the earlier
1.14.57 result. The operator identified the later failed run as commit `534bb2f0`:

- Research closure uses a configured model command, validates structured results
  and records the actual model/provider. Missing, failed or disallowed execution
  remains unavailable; unrelated replay records do not starve pending reviews.
- Catalog cases use their immutable capability executors. Memory benchmarks
  require retrieval cases; empty inputs persist a not-run receipt rather than
  becoming a pass. Dataset-generation failures remain failures.
- A dedicated memory dataset can be securely loaded from
  `EIMEMORY_MEMORY_EVAL_DATASET` or `<EIMEMORY_ROOT>/evaluation/memory_eval.json`
  before generated replay is considered. The automatic conventional-path entry
  is new; the ancestor ownership and protected write-mode checks already existed
  in 1.14.57, originating in `3f3365e3`. New tests exposed a host-path restriction
  when first exercising this entry. See [path security and test isolation](evaluation.md#existing-path-security-new-automatic-entry).
- Available exact binding cases can run diagnostics while missing bindings keep
  their gaps. Projection and catalog selection share a precise time cutoff.

Mainline **1.14.59** additionally preserves bounded research-review failure
reasons and validated bridge categories in durable nightly supervisor receipts.
The full report's `error` reaches the step summary, and managed deployment
checks durable route configuration before stopping writers or switching current.
Unavailable records keep
structured current failure metadata. Retries clear that current metadata while
leaving the previous supervisor receipt unchanged until another run writes it.
See [durable diagnostics](evaluation.md#durable-review-failure-diagnostics-11459).

Mainline **1.14.60** adds automatic Hermes runtime discovery when no explicit
review command is selected. Reviews follow the installation's current provider,
model, credentials and SDK transport without a fixed host path or model label.
Managed preflight accepts this route only after runtime/configuration checks;
actual model response validation and all formal quality requirements remain.
See [automatic Hermes review](evaluation.md#automatic-hermes-review).

A subsequent operator screenshot reports **1.14.60 / `57013648`** with 228 local
regression passes and two failures in `test_governance_env.py`, before deployment
or nightly execution. Those tests expected an unavailable reviewer but did not
isolate the OS account home where Hermes was installed. The follow-up test-only
repair retains their rejection assertions, isolates account lookup and system
PATH in the test subprocess, and separately verifies service-runtime discovery
and current-profile validation. It does not change production routing or gates.

The earlier operator readback reports deployment and nightly execution success for
**1.14.60 / `40285e36`**, as detailed above. Formal business acceptance remains
open. Earlier focused local verification reported 438 passes and four
existing audit failures reproduced on the unmodified `eab88240` baseline.
Passing implementation tests does not supply missing production labels.

## Work required to close acceptance

The latest reported `4fbcbbc9` nightly and smoke passed, while no automatic
review passed and formal cases remain 0/15. The remaining work is evidence-specific:

1. Inspect the current non-passing review reason counts. Earlier receipts showed
   invalid capture boundaries, missing authoritative decision matches and
   undelivered semantic inputs; current per-reason counts are not yet supplied.
   Diagnose those paths only where supported by current stored evidence.
   Check capture retention and actual delivery,
   without reconstructing historical evidence or treating offered items as used.
   The new run does not establish that the earlier semantic transport failure
   persists. Preserve non-passing outcomes until independent signals agree.
2. Assemble accepted natural production cases with exact channel, owner, source,
   label trust and release authorization evidence. Do not turn smoke labels into
   accepted production gold.
3. Supply an independent, non-empty retrieval benchmark dataset. Keep memory
   benchmark verdicts separate from code/capability execution results.
4. Supply applicable, traceable knowledge links for the exact gap revision,
   supported by independent evaluation and feedback. Nine reported candidate
   counts are genuinely zero; diagnostic output cannot create valid hypotheses.
5. After those prerequisites are satisfied, run the relevant acceptance workflows
   and read back their persisted receipts. Assess ranking and noise only with
   qualifying formal evidence and unchanged thresholds.

The earlier smoke violation remains historical evidence, and the ≤0.05 limit
still applies. Its exact loss stage would require historical candidate traces
or an identical-case comparison; the new aggregate smoke success does not
supply that causal evidence. This separate diagnostic question does not change
the current formal gate's evidence requirements.

Historical L5 readiness, a clean deployment, a 10/10 smoke result or a code pass
cannot perform these steps. The operator's latest readback is recorded above;
this documentation update performed no production run or gate relaxation.
