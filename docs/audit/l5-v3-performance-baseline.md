# L5 v3 refactor performance baseline

Date: 2026-08-20

> Historical WP2 evidence. This report was captured before the v3 storage and
> dynamic catalog work landed. The current harness now seeds an exact v3
> definition/revision/binding/profile and a sealed in-process catalog instead
> of the retired fixed replay cohort, so it must be recaptured before using it
> as a post-refactor performance comparison.

## Scope and evidence

This is the pre-Storage-v2 functional/performance baseline required by WP2 of
the L5 v3 refactor. It measures the current v2 runtime as a comparison point;
it does **not** claim that the fixed v2 capability universe is the desired end
state, nor that an isolated no-release readiness result proves production L5
readiness.

- Measured commit: `3af4c531a76524ae06d5ef58ee3edc8d3c0e0ca8`
- Report schema: `l5_v3_baseline.v1`
- Report digest: `ecd3df1b17ae69e92628dd78cf4e2087c4e968433ead422f7dd1e5d91f9aba6b`
- Raw local report: `.bench-artifacts/l5-v3-baseline-23648e8bc5234c6ebf45db4d45a9e30c/l5-v3-baseline.json`
  (ignored by Git; its digest above is the tracked evidence anchor)
- Invocation:

```powershell
& 'C:\Users\maiph\.local\bin\rtk.exe' proxy python benchmarks\l5_v3_baseline.py `
  --tiers small,medium,large --samples 9 --warmup 2 --output-dir .bench-artifacts
```

- Contract regression: `tests/performance/test_l5_v3_baseline_contract.py` —
  `3 passed`.
- Compilation: `python -m compileall -q benchmarks/l5_v3_baseline.py` — pass.

The report recorded `isolated_state=true` and `production_mutated=false`.
Every runtime is constructed with a forced SQLite candidate source, never with
the optional environment-provided PostgreSQL source. Benchmark roots that are
equal to or beneath `EIMEMORY_ROOT` are rejected before work begins.

## Workload definition

Each tier is deterministic and has a versioned `l5_v3_workload.v1` digest. The
digest binds the scope, record payload templates, query, score policy, adapter
channels, replay selection, no-release readiness mode, cold-start fixture
contents, and legacy migration batch policy—not just record counts.

| Tier | Memory records | Capabilities | Scores/capability | Replay fixtures | Outcome traces | Knowledge links | Legacy rows |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| small | 256 | 12 | 3 | 36 | 36 | 12 | 32 |
| medium | 5,000 | 48 | 5 | 240 | 240 | 48 | 256 |
| large | 25,000 | 128 | 8 | 1,024 | 1,024 | 128 | 2,048 |

Static input records are bulk-seeded through the isolated SQLite authority so
their unmeasured Markdown/JSONL projection fan-out cannot dominate a large
fixture. The measured `append` and `atomic_mutation` operations still exercise
the complete SQLite, outbox, JSONL, and Markdown path. Outcome traces and
capability scores also use their normal runtime APIs.

`runtime_cold_startup` copies only closed SQLite authority state plus payload
segments before timing; Markdown and JSONL projections are deliberately
excluded. It is a new-runtime cold-start measurement, **not** a claim that the
operating-system page cache was cleared. `legacy_startup` and
`legacy_migration_batch` are separate: the latter measures only one bounded
`apply_storage_migrations(..., offline=False)` call on a pre-opened isolated
legacy store.

## Results

All 33 operation/tier samples had `semantic_parity.ok=true`; no result digest
varied within its repeated samples. Values below are p95 milliseconds.

| Operation | small | medium | large |
| --- | ---: | ---: | ---: |
| SQLite candidate recall | 116.37 | 148.51 | 233.52 |
| RuntimeStore append (full export path) | 12.52 | 18.55 | 27.56 |
| Atomic record mutation (full export path) | 5.81 | 16.99 | 14.28 |
| L5 readiness, no release receipt | 16.48 | 108.68 | 270.11 |
| Capability ledger, no outcome attribution | 1.76 | 10.55 | 24.89 |
| Current v2 replay-pack build | 7.88 | 49.00 | 99.37 |
| In-process adapter prefetch (Codex/Hermes/OpenClaw) | 69.64 | 96.90 | 1,348.71 |
| In-process adapter status (warm cache) | 0.01 | 0.01 | 0.01 |
| New isolated runtime startup | 7.04 | 19.49 | 25.10 |
| Legacy store startup | 15.00 | 14.91 | 16.88 |
| One bounded legacy migration batch | 0.16 | 0.15 | 0.71 |

The largest observed relative MAD was `0.15` (large migration batch); all
tiers remained below the profile's `0.25` noisy-measurement threshold. The
large adapter-prefetch p95 is the most prominent scale hotspot. It is a
baseline finding for WP5/WP11 design and query-plan work, not a release gate or
an inference that any one adapter is at fault: the operation deliberately
executes all three current adapter surfaces in-process.

Open-state SQLite/WAL footprint, without a forced checkpoint or maintenance:

| Tier | SQLite bytes | WAL bytes | SHM bytes | Closed SQLite bytes |
| --- | ---: | ---: | ---: | ---: |
| small | 6,201,344 | 4,429,032 | 32,768 | 6,287,360 |
| medium | 77,668,352 | 67,108,864 | 163,840 | 77,668,352 |
| large | 372,203,520 | 67,108,864 | 655,360 | 372,215,808 |

## Comparison profile and variance policy

The generated `l5_v3_budget_profile.v1` is embedded in the raw report. A
future candidate is comparable only when it has the same workload digest and
every operation has the same semantic digest. A semantic mismatch is a
non-passing `semantic_mismatch`, even if its timings improve.

- At least seven samples are required for a performance decision.
- Relative MAD above `0.25` is `inconclusive`, not passing.
- p95 allowance: 15% for recall/adapter paths, 20% for append, atomic, ledger,
  replay, and readiness, and 30% for startup/migration paths.
- Rule: `candidate_p95 <= baseline_p95 * (1 + relative_allowance)`.
- Hardware/OS/Python/SQLite details are report context only. They are never a
  capability, provider, machine, or pass/fail identity.

The measured context was Windows 11, CPython 3.14.3, SQLite 3.50.4, with
`EIMEMORY_PRELOAD_HOT_ROWS=128`; these facts are recorded only to aid future
noise diagnosis.

## Explicit limits and follow-up

- Current v3 contract dataclasses are not SQLite-owned yet; this is intentionally
  a v2 storage/reader baseline before WP3.
- No deployment receipt is seeded for `readiness_no_release`; its output cannot
  prove release lineage or production L5 maturity.
- Current replay packs still use the v2 fixed case implementation; their result
  is retained only for before/after comparison until WP15 removes the taxonomy.
- WP3 must preserve local SQLite correctness while adding normalized v3 tables
  and indexes. WP5/WP11 must specifically investigate the large adapter
  prefetch curve before treating it as acceptable headroom.
