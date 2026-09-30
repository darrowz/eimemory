# 1.14.22: backups, verifier bridge pool, nightly hypothesis wait (2026-09-30)

## Backups (governance `no_backups_found`)
- Cause: honrui had no backup job at all, so the governance snapshot had nothing to find.
- Fix: `deploy/eimemory_backup.py` with the daily `eimemory-backup.timer`. Each set contains:
  - SQLite online backups, each passing `integrity_check`;
  - a verified record export;
  - state copies;
  - a 0600 config archive;
  - a sha256 manifest.

  Sets are written to a hidden temp dir and renamed atomically. Retention keeps 5.
- First production set (run manually before release): `/var/lib/eimemory/backups/20260930T094424Z`, 1.1 GB, 49 s. SQLite integrity ok; 32,389 records verified. Governance snapshot health ok, warnings [].

## Verifier bridge pool
- Evidence: the Hermes host hard-codes an 8.0 s external prefetch timeout. There were 41 "prefetch timed out after 8.0s" warnings in 24 h on hermes-gateway. Over the last 45 Hermes proactive decisions, server engine elapsed was p50 7.5 s and p90 9.3 s, with 17 over 8 s. Decisions that found evidence were the slowest (6.1–9.4 s), so exactly the useful turns were dropped.
- The one-shot Luna bridge took 5.5–6.0 s per call, of which about 2.3 s was process start, imports and client setup.
- Fix: a pooled `--serve` worker (see CHANGELOG). The verifier model, prompts and identity are unchanged. It falls back to one-shot.
- The host's 8 s constant lives in Hermes (`agent/memory_manager.py`) and is not configurable from eimemory.

## Nightly `dynamic_capability_evolution`
- All 6 blocked gaps are `code.implementation:v2` with `hypothesis_missing_or_ambiguous` and zero candidates. No production path creates capability hypotheses or knowledge links yet; `create_capability_hypothesis` and `register_knowledge_capability_link` are called only from tests.
- The 1.14.15 audit kept this failing. 1.14.22 narrows that decision: the exact "zero candidates, no error, nothing applied" shape is an evidence wait with diagnostics. Every other blocked shape still fails. No threshold changed and nothing is recorded as passed.
- Open: a real hypothesis producer is still needed before this step can make progress.
