# eimemory Evaluation Framework

`eimemory eval run` runs repeatable recall cases from a JSON dataset. Known-item
smoke, memory benchmarks, capability executors and formal business acceptance
retain separate evidence and verdicts. See [current status](acceptance-status.md).

## Evidence and verdicts

| Result | What it establishes | What it cannot replace |
| --- | --- | --- |
| Known-item smoke | Fixed expected records can be retrieved | Natural-query and colleague-wide business acceptance |
| Memory CI / retrieval benchmark | Retrieval behavior on a non-empty labelled dataset | Release authorization and qualifying production labels |
| Trusted capability/catalog run | One immutable executor case for an exact revision/binding | A memory benchmark or another binding's evidence |
| Formal production recall gate | Qualifying trusted cases and release-bound evidence satisfy the complete contract | Missing prerequisites cannot be inferred from a process exit code |

An empty memory dataset is not a pass. The reported 1.14.57 nightly skipped it
with `memory_eval_dataset_empty`. Mainline 1.14.58 repairs persist a not-run
receipt, dispatch catalog cases through capability executors, and securely select
`EIMEMORY_MEMORY_EVAL_DATASET`, then
`<EIMEMORY_ROOT>/evaluation/memory_eval.json`, then generated replay. Dataset
generation or secure loading failures stay failures. See
[acceptance status](acceptance-status.md#mainline-workflow-repairs).

### Existing path security, new automatic entry

Version 1.14.58 added automatic discovery of the conventional memory dataset;
it did not introduce the loader's ancestor ownership or writable-directory
rules. Those checks already existed in 1.14.57 and originated in
[`3f3365e3`](https://github.com/darrowz/eimemory/commit/3f3365e3b7c1a147d2a1d9036cfe55a735bb1741).
On POSIX, ancestor owners must be root or the process's effective user, and
group/world-writable ancestors require sticky protection. Dataset files must
also satisfy the existing ownership, mode, no-symlink and open-time checks.

The new regression tests first exercised this automatic entry and exposed
host ancestor permissions that did not meet the existing contract: **old rules,
new entry, newly encountered host restriction**. Positive-path tests use the
existing `trusted_dataset_path_ancestors` fixture to model trusted host
ancestors. Dataset directories and files remain subject to the real checks;
separate rejection cases cover foreign owners and unsafe write modes. This
test isolation changes neither production validation nor gate thresholds.

For an actual deployment, select a dataset path whose entire parent chain
satisfies the contract. Changing only the file or `evaluation/` directory mode
cannot repair an untrusted ancestor. Secure loading failures remain execution
failures and do not fall back to generated replay or count as a benchmark pass.

For a separate memory CI dataset, use the existing CLI:

```bash
eimemory eval ci memory-dataset.json --output memory-ci-report.json
```

Use independently labelled retrieval cases and record the scope, dataset identity
and release. Sample fixture cases are useful smoke checks, not production gold.

## Dataset Format

```json
{
  "name": "memory-smoke",
  "scope": {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"},
  "task_type": "brain.respond",
  "profile": "balanced",
  "seed": [
    {
      "title": "Official channel",
      "text": "Feishu is the official communication channel.",
      "memory_type": "decision"
    }
  ],
  "cases": [
    {
      "id": "official-channel",
      "query": "official communication channel",
      "expect_any_title": ["Official channel"],
      "limit": 3
    }
  ]
}
```

Expected fields can be mixed:

- `expect_any_title`
- `expect_any_record_id`
- `expect_any_kind`
- `expect_any_text`

## Run

```bash
eimemory eval run dataset.json --output report.json
```

Use `--no-seed` to run against an existing production store without inserting
dataset seed records.

## Metrics

The report includes:

- `pass_rate`
- `mrr`
- `precision_at_k`
- per-case returned ids, titles, confidence, retrieval mode, and vector hits
- misses with expected versus returned records

For memory CI reports, phase-level MRR, recall, and precision are macro-averages
of the corresponding per-case metrics. Successful expected-empty cases retain
their per-case scores. Pass thresholds use the unrounded pass-count ratio;
three-decimal rounding is for report presentation only.

The LoCoMo and LongMemEval `ndcg_at_5` metric uses binary relevance over evidence
IDs. Its ideal ranking uses the requested cutoff, even when fewer results are
returned, and a repeated evidence ID earns gain only at its first occurrence.

This framework evaluates recall behavior first. Broader source-intake,
daily-brief, and skill replay suites should reuse this report shape.

## Research model review configuration (1.14.58 mainline)

Research closure uses `EIMEMORY_RESEARCH_REVIEW_LLM_COMMAND`, a JSON argv array,
or the shared `EIMEMORY_LLM_COMMAND` when the feature-specific route is unset.
The configured bridge receives `system_prompt`, `user_prompt` and `json_mode`
as JSON on stdin and returns `text`, `provider_id` and `model_id` as JSON on
stdout. No implicit `codex` executable is required by the repaired default path.

Review text must itself be a JSON object with exactly four string fields:
`verdict` (`approve`, `reject`, or `needs_followup`), `rationale`,
`required_followup`, and `risk`. Rationale and risk cannot be blank.
Duplicate JSON keys, invalid structure, command failure and missing configuration
leave the review unavailable. There is no fallback after a command fails.

The feature timeout is `EIMEMORY_RESEARCH_REVIEW_LLM_TIMEOUT_SECONDS`, falling
back to `EIMEMORY_LLM_TIMEOUT_SECONDS`. The existing
`EIMEMORY_ALLOWED_REVIEW_MODELS` policy checks the actual responding
model. The persisted receipt records the actual model/provider; unavailable
reviews are retried through the existing nightly retry queue.
Configure the route in the managed service environment before deployment and
verify its receipts. Model review cannot replace accepted production labels,
release authority or an independent retrieval dataset.

### Durable review failure diagnostics (1.14.59)

Managed deployments check the protected `governance.env` before stopping
storage writers or switching `current`. A missing or invalid durable review
route blocks the switch. A command present only in the deployment controller's
environment cannot satisfy this check. This preflight parses configuration
only; it invokes no model and cannot establish provider readiness or acceptance.

Configure `EIMEMORY_RESEARCH_REVIEW_LLM_COMMAND` (or the shared
`EIMEMORY_LLM_COMMAND`) in the managed governance file using an operator-selected
JSON argv array for a working bridge with the protocol described above.
The [governance example](../deploy/governance.env.example) documents both routes.
As the service user, configuration can be checked without rerunning nightly:

```bash
/opt/eimemory/current/.venv/bin/python -I -B \
  /opt/eimemory/current/deploy/run_with_governance_env.py \
  --env-file /etc/eimemory/governance.env --check-research-review
```

This check requires the 1.14.59 helper; the 1.14.58 live helper does not yet have
the option. Protect the file with the existing ownership/mode requirements.
No default provider is inserted and no failed review is converted into a pass.

The full review report exposes `error`, `error_counts` and
`unavailable_records`. Each unavailable record stores a fixed `review_error`
and structured `review_failure` in both content and metadata. Diagnostics
distinguish missing/invalid configuration, missing executables, permission
errors, timeouts, failed commands, invalid bridge responses, disallowed models
and invalid review text. A command bridge's validated failure category and
measured timings are retained when available. Generic exceptions remain
`research_review_execution_failed`; no provider/authentication cause is guessed.

The persisted supervisor receipt includes
`nightly_diagnostics.research_closure_review.reason_counts` and up to 20
sanitized `unavailable_records` with internal references. Reason counts cover
at most 500 rows; truncation is explicit. Raw prompts, stdout/stderr, argv and
arbitrary exception text do not enter this projection. Legacy missing reasons
remain `reason_not_reported` or `reason_not_allowlisted`.

Read these fields from storage along with the deployed version/full commit
before attributing a failed run. Retrying a review clears its current failure
metadata; it does not clear the prior supervisor receipt's diagnosis. The
existing supervisor contract still keeps the latest run rather than an
immutable history of every nightly. These diagnostics do not approve reviews,
create accepted recall cases or relax any quality gate.

## Dynamic Capability Catalog Evaluation

L5 v3 capability evaluation is separate from dataset recall benchmarks.
Installed Python entry points in
`eimemory.capability_catalog.bootstrap.v1` register trusted executors and typed
cases, after which the catalog is sealed. A dynamic case resolves one exact
capability revision and provider binding through the selected profile.

```bash
eimemory learn capability-acceptance \
  --profile l5.default --capability-scope global --json
eimemory learn l5-v3 \
  --profile l5.default --capability-scope global --persist --json
eimemory learn l5-readiness \
  --reader-mode v3 --profile l5.default --json
```

EvaluationSpec derivation is stable across repeated runs; the same immutable
case/revision pair retains one spec digest while each execution produces a
distinct run and observation. Governed probes preserve the executor verdict in
the persisted run. The Hongtu catalog returns aggregate recall checks only and
uses exact binding selectors so Hermes and OpenClaw accumulate independent
evidence. Recalled payloads are not evaluator output.

## Production recall gate

`eimemory eval production-recall DATASET.json` opens the dataset through the
trusted scheduler loader. The evaluator receives open-time
`secure_dataset_fingerprint.v1` evidence; symlinks, unsafe file modes, and
untrusted path components fail closed. Policy v2 requires 15 accepted cases and
labels, with at least five cases from each of OpenClaw, Codex, and Hermes. An
older policy-v1 report cannot qualify as the predecessor baseline. The current
production dataset is incomplete and no accepted production gate is claimed
here. The latest persisted-receipt readback reports `gate_ok=false` with
`recall_quality_evidence_incomplete` and 0/15 accepted cases. Two pending cases
remain pending. Current standards include noise ≤0.40 and precision@3 ≥0.60;
missing evidence blocks formal scoring. Fixed-item smoke results do not certify
natural queries, no-answer quality or every colleague.

Labels must satisfy the current trusted operator or evidence-bound authorized
`auto_review` path. Historical delegated labels cannot be counted as accepted
gold. Release authorization must be verified independently; neither smoke nor
successful deployment grants it.

```bash
eimemory eval production-recall production-dataset.json \
  --no-seed --persist-report --output production-recall-report.json
```

Use a qualifying securely loaded dataset and inspect the persisted gate verdict,
identity, label trust and blocking reasons after execution. See
[receipt references and remaining work](acceptance-status.md).

## LongMemEval Raw Evidence

`eimemory eval longmem` runs a LongMemEval-style retrieval benchmark without
LLM calls. It ingests each case's haystack as `raw_chunk` records, retrieves
raw evidence, and reports retrieval metrics only. It does not score generated
answers or QA accuracy.

```bash
eimemory eval longmem examples/evaluation/longmemeval_smoke.json \
  --mode raw \
  --granularity session \
  --limit 10 \
  --output tmp/longmemeval-report.json
```

Options:

- `--mode raw` searches raw evidence directly.
- `--mode hybrid` asks memory recall for raw-hybrid evidence first, then falls
  back to raw retrieval.
- `--granularity session|turn|chunk` selects which evidence id type is scored.
- `--persist-report` writes a `reflection` report with
  `source="eimemory.longmemeval"` and `meta.report_type="longmemeval_eval"`.
  Governance snapshots surface the latest report under `longmemeval`.

Dataset cases accept LongMemEval-like aliases:

- question fields: `question` or `query`
- answer fields: `answer` or `expected_answer`
- haystack fields: `haystack_sessions`, `sessions`, or `haystack`
- text fields inside sessions/turns/messages: `content`, `text`, or `message`
- evidence fields: `evidence_session_ids`, `evidence_turn_ids`, and
  `evidence_chunk_ids`

Report metrics include `retrieval_recall_at_1/5/10`, `recall_any_at_k`,
`recall_all_at_k`, `ndcg_at_5`, `mrr`, latency average/p95,
`by_question_type`, and per-sample returned evidence ids.

For `--granularity turn`, LongMemEval keeps session chunks intact and stores
per-turn text metadata. Retrieved sessions are then locally ordered by turn
text against the query before scoring, which avoids penalizing correct session
retrieval simply because the evidence turn appears late in the session.

The full benchmark helper `scripts/run_full_eval.py` accepts these environment
knobs for production probes:

- `EIMEMORY_RUN_ONLY=all|lme|locomo` to run both suites or one targeted suite.
- `EIMEMORY_WORKERS=<n>` to set worker count.
- `EIMEMORY_LME_LIMIT` and `EIMEMORY_LOCOMO_LIMIT` to cap per-worker retrieval.
- `EIMEMORY_RERANKER=auto|deterministic|llm` to control raw reranking.

Aggregated LME reports include `failure_count`, `rank_histogram`, and
`failure_examples` so low-score runs show whether misses are rank placement,
missing evidence, or retrieval failures.

## Public Benchmark Harness

`eimemory eval public-benchmark` runs public benchmark adapters with an
isolated temporary runtime. It is the preferred entry point for full
LongMemEval or LoCoMo datasets because it never writes to the production DB at
`/var/lib/eimemory/state/eimemory.sqlite`.

```bash
eimemory eval public-benchmark examples/evaluation/longmemeval_smoke.json \
  --suite longmemeval \
  --mode raw \
  --granularity session \
  --output tmp/public-longmemeval-report.json

eimemory eval public-benchmark examples/evaluation/locomo_smoke.json \
  --suite locomo \
  --mode raw \
  --granularity turn \
  --output tmp/public-locomo-report.json
```

The report's `metrics` object includes normalized `r_at_1`, `r_at_5`, `mrr`,
`ndcg_at_5`, latency, and failure samples. The adapter-specific report is
under `report`.

`eimemory eval locomo` is also available for adapter-level smoke runs. It
accepts LoCoMo-like `conversation`, `messages`, `turns`, `sessions`, or
`conversation_sessions` fields and scores `session`, `turn`, or `chunk`
evidence ids.

## Real Task Replay

`eimemory eval task-replay` runs `real_task_replay.v1` cases for OpenClaw,
UUMit, and eimemory history-derived tasks. Seeded runs use temporary state so
smoke datasets do not contaminate production memory.

```bash
eimemory eval task-replay examples/evaluation/real_task_replay_smoke.json \
  --output tmp/real-task-replay-report.json
```

The replay schema supports:

- `source_system`: `openclaw`, `uumit`, `eimemory`, or another source label.
- `query` / `input` / `prompt`: the replayed user or system request.
- `task_type` and `task_context`: routing context for recall.
- `expected_text`: terms that should be recalled.
- `negative_expected_text`: terms that must not be recalled.

To build a larger replay set from local outcome traces, corrections, and
previous replay suggestions:

```bash
eimemory learn replay-dataset --limit 100
```

To add the curated regression set from real operator feedback and incidents
(wrong version answers, missing evidence checks, long-task silence, field
mapping mistakes, and unsupported evaluation claims), pass:

```bash
eimemory learn replay-dataset --limit 100 --include-built-in-regressions
```

The generated dataset is marked `schema_version: real_task_replay.v1` and
includes source-system labels without storing secrets or private tokens.

## Actionable Memory Evaluation

`eimemory eval actionable` runs a compact smoke suite for recall + posture +
contamination checks.

```bash
eimemory eval actionable examples/evaluation/actionable_memory_smoke.json \
  --output tmp/actionable-memory-report.json
```

Cases support:

- `case_type: recall` for mixed recall checks.
- `case_type: posture` for posture-profile checks.
- `query_type` for intent-aware recall (`project`, `research`, `chat`, etc.).
- recall assertions: `expect_any_title`, `expect_any_record_id`,
  `expect_any_kind`, `expect_any_text`.
- contamination assertions: `forbid_any_title`, `forbid_any_kind`.
- posture assertions: `expect_profile_non_empty`, `expected_constraints`.

The report includes:

- `ok`
- `report_type` (always `actionable_memory_eval`)
- `sample_count`
- `pass_count`
- `pass_rate`
- `recall_topk_pass_rate`
- `posture_pass_rate`
- `contamination_rate`
- `project_query_contamination_rate`
- `samples`

Persisted reports are written as `kind="reflection"` with
`source="eimemory.actionable_memory"` and
`meta.report_type="actionable_memory_eval"`.

Governance snapshots now include:

- `actionable_memory.posture_profile_count`
- `actionable_memory.posture_coverage`
- `actionable_memory.project_query_contamination_rate`

## Living Memory Evaluation

`eimemory eval living` runs a deterministic LivingMemEval smoke suite against
`record.meta["living_memory_v1"]`. Seed records are enriched before scoring
when living metadata is absent, using the local living-memory helper if it is
available.

```bash
eimemory eval living examples/evaluation/living_memory_smoke.json \
  --output tmp/living-memory-report.json
```

Dataset cases bind to seed records by `seed_id` or `seed_index` and can assert:

- `expect_temporal`
- `expect_motive`
- `expect_affective`
- `expect_repair_needed`
- `expect_stale`
- `expect_posture`

Reports include `sample_count`, `pass_rate`, `temporal_accuracy`,
`motive_accuracy`, `affective_grounding`, `repair_recall`,
`stale_label_avoidance`, and `posture_accuracy`.

Operator-facing living-memory commands emit JSON:

```bash
eimemory living enrich --limit 100
eimemory living timeline
eimemory living posture "repair before proceeding"
```

Governance snapshots include a `living_memory` section with enriched counts,
repair-needed counts, open future-intent counts, life-phase counts, and average
ripeness.

`action_posture.recommended` uses the canonical posture values `act`, `nudge`,
`wait`, and `let_go`; explanatory fields such as friction, urgency, trust risk,
and ripeness describe why that posture was chosen.
