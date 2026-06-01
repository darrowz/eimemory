# Outcome Evolution Design

## Goal

Outcome Evolution closes the loop between OpenClaw task execution and eimemory governance. Each task can persist a sanitized `outcome_trace.v1` reflection, nightly can summarize failure patterns, and governance can turn repeated low-risk patterns into replayable candidates before any policy reaches an operator-facing path.

## Closed Loop

1. **Capture** records a task outcome as a `reflection` with `report_type=outcome_trace`, `schema_version=outcome_trace.v1`, `primary_label`, `diagnosis_signals`, and `risk_level`.
2. **Sanitize** strips raw media, credentials, authorization material, camera URLs with embedded secrets, cookies, raw long transcripts, and unbounded screenshots before storage.
3. **Diagnose** assigns one stable primary label and optional signals such as `operator_gap`, `missing_visual_evidence`, `world_state_mismatch`, and `verifier_missing`.
4. **Summarize** nightly reports `outcome_evolution` with trace totals, bad outcome rate, top labels/signals, and visual/operator/world-state gap counts.
5. **Replay** converts bad outcomes into replay cases with positive expectations, forbidden regressions, source trace ids, and risk metadata.
6. **Search** groups repeated labels/signals into candidate memory, rule, verifier, or operator-prompt improvements.
7. **Shadow** evaluates candidates against replay datasets without changing active behavior.
8. **Promote or block** applies only candidates that pass risk gates. Blocked or high-risk candidates remain audit records.
9. **Rollback** monitors attributed policies and rolls back low-risk changes when later traces show repeated bad outcomes tied to the policy.

## Trace Boundary

The durable trace is evidence, not a raw execution dump. It may include:

- Compact task identifiers, action labels, and timestamps.
- Sanitized `visual_evidence` summaries, hashes, dimensions, or redacted detector observations.
- Sanitized `operator_gap` facts, such as missing confirmation, ambiguous intent, or absent approval.
- Sanitized `world_state` observations, such as expected state, observed state, and mismatch reason.
- Policy attribution ids, replay hints, and evaluator/verifier status.

It must not include:

- Raw screenshots, base64 images, videos, OCR dumps, or camera frames.
- Cookies, tokens, authorization headers, passwords, private keys, or credential-bearing URLs.
- Raw long transcripts when a bounded summary is enough.
- Sensitive user content unrelated to the failure diagnosis.
- Direct device-control payloads that would let a later job replay a real-world action.

## Risk Gates

Risk level controls what the loop may do automatically:

- **L0 observation**: summaries, replay cases, and harmless recall hints may be generated automatically.
- **L1 software-only**: candidates may be shadowed and promoted after deterministic replay passes with no forbidden hits.
- **L2 workflow-changing**: candidates require stronger replay evidence, source trace diversity, and a stable rollback key.
- **L3 account, privacy, or irreversible impact**: candidates are report-only and require explicit operator approval.
- **L4 physical, financial, medical, legal, or credentialed action**: no automatic promotion; only sanitized evidence and blocked recommendations are allowed.

Promotion requires:

- Replays pass threshold for the target scope and task type.
- No regression on known success traces.
- Risk level is within the automatic budget.
- Candidate has clear policy attribution and rollback metadata.
- Sensitive-data validation passes after serialization.

Rollback triggers when later bad traces repeatedly attribute failure to the promoted policy or when replay confidence falls below the gate.

## Nightly Surface

Nightly exposes a compact `outcome_evolution` summary:

- `outcome_trace_count`
- `bad_outcome_count`
- `bad_outcome_rate`
- `top_primary_labels`
- `top_signals`
- `operator_gap_count`
- `visual_gap_count`
- `world_state_mismatch_count`

Missing optional trace fields are treated as empty values. A partial trace can increase `outcome_trace_count` without breaking the nightly run or inventing a diagnosis.

## Rollout

1. **Observe**: persist sanitized traces and nightly summaries only.
2. **Replay buildout**: generate replay cases from bad outcomes but keep all candidates report-only.
3. **Shadow**: run candidate policies against replay datasets and record pass/fail reports without applying changes.
4. **Limited promotion**: allow L0/L1 software-only policy changes within a small nightly budget and with rollback metadata.
5. **Expanded governance**: consider L2 changes only after repeated shadow success and operator-reviewed reports.
6. **Steady state**: keep L3/L4 report-only unless a separate explicit approval channel is added.

The rollout is deliberately reversible: every promoted candidate must cite source traces, replay evidence, and a rollback key before it can affect active behavior.
