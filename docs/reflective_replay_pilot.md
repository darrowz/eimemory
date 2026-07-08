# Reflective Replay Pilot

This checked-in document is the contract for reflective replay pilot runs. Live
run outputs belong under `.pilot/` and must stay out of git.

Required safeguards:

- Default model is `gpt-5.5`.
- Primary model rate-limit and cooldown errors are retried before any fallback.
- MiniMax fallback is disabled unless the operator passes
  `--allow-fallback-minimax`.
- If the primary model fails and fallback is not explicitly allowed, the case is
  marked `skipped` with `skipped_reason=primary_model_failed`.
- Skipped cases must not produce formal root-cause conclusions.
- Generated reports must include both `generated_at` and `source_snapshot_at`.
- MiniMax provider credentials must come from explicit environment variables,
  not by parsing OpenClaw's local secret store.

Run locally or on honxin:

```bash
python3 scripts/reflective_replay.py \
  --db /var/lib/eimemory/state/eimemory.sqlite \
  --output .pilot/reflective_replay_pilot.md \
  --json-output .pilot/reflective_replay_pilot.json
```

Intentional fallback run:

```bash
EIMEMORY_MINIMAX_API_KEY=... python3 scripts/reflective_replay.py \
  --db /var/lib/eimemory/state/eimemory.sqlite \
  --allow-fallback-minimax
```

Do not treat any prior report with `MiniMax-M3 > 0` and `gpt-5.5 = 0` as a
formal production RCA unless that report was explicitly marked as an allowed
fallback run.
