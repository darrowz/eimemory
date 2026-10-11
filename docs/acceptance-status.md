# Receipt-based acceptance status

Documentation updated on 2026-10-11. This page transcribes the operator's
latest persisted-receipt readback for **1.14.61 / `7a625fe1`**, alongside
earlier release evidence. The documentation publisher has not
independently queried the production database or rerun production acceptance.

**Latest nightly execution and current service checks passed.
Formal business recall quality and capability evolution remain unaccepted.**

## Latest October 11 nightly: 1.14.61

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
The policy-v2 production dataset requires 15 accepted cases, including at least
five from each of OpenClaw, Codex and Hermes. Acceptance must retain trustworthy
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
The October 11 readback above now reports execution on 1.14.61 and zero pending
reviews, with all 304 reviews non-passing. This does not certify business quality.

**1.14.62** expands the versioned safe reason catalog to include capture,
original-query, feature-quality, candidate, semantic and native acceptance
failures previously generalized by an incomplete list. Unknown arbitrary text
remains redacted and oversized reason maps disclose truncation. Closed/reopened
supervisor receipt tests verify the exact native reasons and unchanged label
eligibility. This diagnostic patch cannot recover raw reasons absent from an
older summary, and has no supplied production readback yet.

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

The latest reported `7a625fe1` nightly completed with zero pending reviews,
but none passed. The remaining work is evidence-specific:

1. Diagnose invalid capture boundaries and missing authoritative decision matches
   from real stored evidence. Check current capture retention and actual delivery,
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

Historical L5 readiness, a clean deployment, a 10/10 smoke result or a code pass
cannot perform these steps. The operator's additional nightly is recorded above;
this documentation update performed no production run or gate relaxation.
