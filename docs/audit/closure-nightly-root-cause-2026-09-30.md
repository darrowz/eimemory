# Closure and nightly failures on honrui: root cause (2026-09-30)

Scope: honrui running 1.14.14 (`bab000fa`), deployed 2026-09-30 12:57 CST.
Evidence was gathered read-only: the deploy log, the closure capture
`closure-6par3v0y`, the nightly journal, and read-only SQLite queries.
The fixes ship in 1.14.15.

## Symptoms

1. `business_closure_outcome=failed` with `post_deploy_validation=degraded`,
   stopped at `closure_rehearsal` / `bootstrap_pending_non_recall_l5_evidence_incomplete`.
   The deploy log also listed `recall_quality_evidence_incomplete` and a
   `lineage_domain_evidence_missing` item for `memory.recall`.
2. The log said "post-deploy business closure is pending retry", but
   `eimemory-release-closure.service` was always skipped
   (`ConditionPathExists=.../release-closure-pending.json` unmet).
3. `eimemory-nightly.service` exits 1, so `eimemory-timer-monitor.service`
   alerts every 5 minutes.

## Root cause per blocker

| Blocker | Class | Evidence |
|---|---|---|
| `recall_quality_evidence_incomplete` | Honest data gap | The bootstrap reports `accepted_case_count: 0`, `required_case_count: 15`, `pending_collected: 53`, and `per_channel_accepted` is 0 for every channel. The recall gate status is `data_accumulating`, and the summary classifies it under `failure_signals.waits`. Nightly's `known_item_smoke.v1` gives `insufficient` by contract: smoke is never judged quality, so `label_trust_accepted=false`. `release_authority_verified` is always `false` in `recall_quality_contract`. This is correct fail-closed behavior. |
| `lineage_domain_evidence_missing` (memory.recall) | Diagnostic artifact (bug in presentation, not in admission) | This item comes from the pre-closure baseline lineage `rec_313bfede7594`. The installer records that lineage before closure runs, so every `gate_evidence` list is `[]`. Minutes later, closure's finalizer recorded `rec_dca3110f5090`: `compatible: true`, with `memory.recall` in mode `current` and evidence `[prbs_d76f47b5…, replay_62b3c12d7f2e]`. The final summary's `closure_blockers.items` is `[]`. Historical `release_lineage_not_compatible` failures (1.14.4, 1.14.5, 1.14.12) came from `code.evolution` → `strict_code_evolution_receipt_required`, which is policy. `memory.recall` was not involved. |
| `bootstrap_pending_non_recall_l5_evidence_incomplete` (the actual stop) | Honest data gap under the current scope contract. The reason code was opaque (diagnostic bug). | In the readiness report, `live_task_gate.current_deployment_verified_real_tasks=0` and `hard_metric_samples.verified_real_tasks=0 / verified_real_task_types=0`. `verified_real_replay.reason=current_code_replay_missing`. Accumulation needs ≥10 historical verified real tasks. In the deploy scope `hongtu/embodied/darrow`, the DB has 0 outcome traces with a real terminal verifier: 1140 are rehearsal capability probes and 320 are live-acceptance operational probes. Hermes `hermes.task_end` events live in `embodied::channel::hermes`. By design (`l5_scope_authority`), business receipts stay in the scope that produced them, and OpenClaw is absent on honrui. Every capture from 1.14.7 through 1.14.14 shows `verified_real_tasks=0`. |
| "pending retry" with nothing queued | Bug (installer message) | `release-closure-pending.json` is written only for `current_release_channel_receipt_not_found`. Channel acceptance passed (`rec_937d88ab013e`), and the source report's `pending_checkpoint` is `{"status": "no_pending"}`. Yet `_run_post_deploy_validation` printed "pending retry" unconditionally. |
| Nightly exit 1 | Not recall. Fail-closed dynamic evolution (by design). | Nightly diagnostics: `failed_steps: ["dynamic_capability_evolution"]` with `reason_counts: {hypothesis_missing_or_ambiguous: 5}`, while `evidence_waits: ["production_recall", "recall_quality_gate"]` (already non-fatal via `_non_actionable_step_wait`). The DB has 0 `capability_hypothesis` records in scope. The design keeps this blocked: see the `_run_dynamic_capability_evolution` docstring and the 1.14.8/1.14.9 notes ("retain its fail-closed success verdict"). So it is not reclassified here. |

## Fixes (1.14.15)

- `deploy/install_immutable_release.sh`: new `_release_closure_retry_state` helper.
  "pending retry" is printed only when a valid waiting checkpoint for this commit
  exists. Otherwise the installer prints `release_closure_retry=not_queued` and
  names the blocked stage and reason. Exit and degraded semantics are unchanged.
- `eimemory/governance/l5/closure_rehearsal.py`: a non-recall rejection now
  carries bounded `non_recall_evidence_deficits` codes, computed from the same
  thresholds as the gate. The verdict is unchanged.
- `eimemory/governance/release/closure_blockers.py`: new
  `closure_blockers.non_recall_evidence` section. For pre-closure baseline
  lineage, `changed_unverified` items are marked `awaiting_release_closure`,
  and the report carries `phase: pre_closure_baseline`. Explicit gate errors
  stay open.

No threshold, label, authority, or admission rule was changed. No evidence was
created.

## What still needs real data or a human decision

- Recall: accept ≥15 real production-query labels with trusted operator
  authority (`eimemory eval production-query accept …`). Until then the recall
  gate stays `data_accumulating`.
- Non-recall L5: at least 10 verified real tasks of at least 4 types in the
  deploy scope, or a verified real replay. Honrui's deploy scope has no
  producer (OpenClaw is absent, and Hermes receipts are channel-scoped).
  Someone must decide whether to run closure in the channel scope or to supply
  OpenClaw/Codex tasks. Changing the scope partition would weaken policy.
- Nightly: `dynamic_capability_evolution` needs a unique candidate
  `capability_hypothesis` for each blocked capability gap. There is no CLI
  producer, and these hypotheses are created only through the API. Until then,
  nightly exits 1 and the timer-monitor alert repeats. Deploying 1.14.15 does
  not change this.
- 1.14.15 touches `code.evolution` paths (installer, closure modules). Its own
  deploy closure is therefore expected to block on
  `strict_code_evolution_receipt_required` (policy), as 1.14.4, 1.14.5, and
  1.14.12 did, unless it is deployed through the strict code-evolution
  transaction.
