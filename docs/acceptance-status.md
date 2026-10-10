# Receipt-based acceptance status

Documentation updated on 2026-10-10. This page transcribes the operator's
earlier persisted-receipt readback for **1.14.57 / `eab88240`** and a later
nightly screenshot with subsequent detailed-record readback. The documentation publisher has not independently queried
the production database or rerun production acceptance.

**The later reported nightly failed at research model review. Formal business
recall quality has not passed; the capability-evolution loop remains incomplete.**

## Later reported nightly failure

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

## Recall evidence

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

## Other incomplete evidence

- Memory CI was skipped with `memory_eval_dataset_empty`. A separate passing
  code-capability evaluation cannot substitute for retrieval benchmark evidence.
- Dynamic capability evolution has nine waiting items, all marked
  `hypothesis_missing_or_ambiguous`. Diagnostics cannot fabricate knowledge
  links, grant behavior authority or close unresolved hypotheses.
- Research closure could not find the `codex` executable; model review was
  unavailable. An unavailable reviewer cannot approve a candidate.

## Receipt references

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

These are package changes, not production acceptance. Earlier focused local verification reported 438 passes and four
existing audit failures reproduced on the unmodified `eab88240` baseline.
Passing implementation tests does not supply missing production labels.

## Work required to close acceptance

1. Review and release the workflow repair through normal code and deployment
   gates. Validate the configured model command in the managed service environment.
2. Assemble accepted natural production cases with exact channel, owner, source,
   label trust and release authorization evidence. Do not turn smoke labels into
   accepted production gold.
3. Supply an independent, non-empty retrieval benchmark dataset. Keep memory
   benchmark verdicts separate from code/capability execution results.
4. Resolve each ambiguous or missing hypothesis with traceable independent
   evaluation and feedback. Keep unresolved binding gaps visible.
5. After those prerequisites are satisfied, run the relevant acceptance workflows
   and read back their persisted receipts. Assess ranking and noise only with
   qualifying formal evidence and unchanged thresholds.

Historical L5 readiness, a clean deployment, a 10/10 smoke result or a code pass
cannot perform these steps. No production rerun or gate relaxation was performed
for this documentation update.
