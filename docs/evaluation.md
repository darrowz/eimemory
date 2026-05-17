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
