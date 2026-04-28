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
