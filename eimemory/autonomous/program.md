# eimemory Autonomous Research — Program

This document is the single source of truth the Karpathy Loop reads at
start-up. Loop, hypothesis generator, exp log, and compounding context
all derive their inputs from here. Edit the program, not the code, when
you want the loop to chase a different metric.

## Goal

Improve eimemory by running experiments overnight. Each experiment
modifies one piece of eimemory configuration, code, or policy, then
verifies with held-out validation. The agent writes its own hypothesis
from observed `weakness` and `incident` records.

## Metric

Primary: `recall_view.hit@1` from `eimemory eval production-recall`.
Secondary: `capability_score.evidence` average over last 7 days.
An experiment is **kept** only if the primary metric improves by ≥ 1%
relative. An experiment is **discarded** otherwise. No human override.

## Time Box

Each experiment: **5 minutes** wall clock, hard limit. If exceeded, kill
and discard. The 5-minute box is what makes "50 experiments per night"
mathematically possible (50 × 5 min = 250 min ≈ 4 hours, matching the
experimental iteration budget).

## Inputs (read once at loop start)

- `/var/lib/eimemory/records.jsonl` — last 7 days of:
  - `weakness` (839 records) — failure modes
  - `incident` (250 records) — incidents
  - `capability_score` (3087 records) — real quality scores
  - `learning_playbook` (145 records) — 70/30 split into train/holdout
  - `recall_view` (1047 records) — actual recall results

## Experiment Loop (constrained agent)

1. **Hypothesize**: Embedding-cluster recent `weakness` + `incident`
   summaries. Pick the highest-frequency cluster. Generate hypothesis:
   "If we change X, then Y failure mode should reduce by Z%".
2. **Plan**: List exact files to modify. Reject if > 1 file or change
   > 50 lines.
3. **Apply**: Use state machine (`state_machine.py`) — create in
   `sandbox/`.
4. **Verify**: Run `eimemory eval production-recall` against holdout.
   Compare hit@1 to baseline.
5. **Decide**: keep (promote to canary) or discard (move to
   `rolled_back/`).
6. **Log**: Write to `exp_log/<timestamp>.md` with hypothesis, diff,
   result.
7. **Compound**: Next iteration loads last 5 kept changes as context.

## Constraints

- No model weight changes.
- No changes outside `/dev-project/eimemory` (sandbox repo root).
- No external network calls (use only local data).
- No destructive operations (`rm -rf`, `git push --force`).
- Profile-gate: every experiment must call `load_profile(...).can_run_phase2()`.
  If False, log `profile_blocked` to audit and exit. (Hard check, not
  a config — see `eimemory/governance/safety/profile.py`.)

## Stop Conditions

- 50 experiments reached
- 4 hours wall clock
- 3 consecutive failures
