# eimemory Evaluation Framework

`eimemory eval run` runs deterministic memory recall evaluations from a JSON
dataset. It is the shared base for production smoke checks, regression tests,
and future `eiskills` replay/utility scoring.

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

This framework evaluates recall behavior first. Broader source-intake,
daily-brief, and skill replay suites should reuse this report shape.

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

The top-level report includes normalized `r_at_1`, `r_at_5`, `mrr`,
`ndcg_at_5`, latency, and failure samples, plus the adapter-specific report
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
