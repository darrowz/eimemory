# Business audit remediation — 1.13.4

Baseline: `e95d7ecd632e45abb5c02a2792c743f4be7b3589` (1.13.3).
This implements the supplied 2026-09-09 audit's eight P1 and nine P2 findings.
It does not assert that the reported failures occurred in production.

## Acceptance mapping

| Finding | Correctness boundary | Maintained regression coverage |
| --- | --- | --- |
| 1 | Policy rollback also terminates its watch; watch decisions, related records and ledger share a transaction; stale writes cannot reopen terminal watches | `test_promotion_audit_regressions.py`, `test_promotion_watch.py` |
| 2 | No session-recency or outcome-only policy attribution; exact event/audit and recall-time policy versions; recheck under write lock; immutable event retries | `test_promotion_audit_regressions.py`, `test_openclaw_policy_attribution.py` |
| 3 | Replay/unverified outcomes cannot promote or roll back policies or contribute historical rollback thresholds; explicit user corrections retain their safety role | `test_promotion_audit_regressions.py`, `test_audit_capability_outcome_classes.py` |
| 4 | Supersession requires identical physical scope and source | `test_audit_recall_boundaries.py` |
| 5 | Collapse only identical full envelopes across delivered sections; preserve conflicting-reference rejection | `test_audit_recall_boundaries.py` |
| 6 | Surviving writers recover peer-crash tails inside the append lock, including rotation and index/counter recovery | `test_payload_segment_live_recovery.py` |
| 7 | Rollback reconciles the visible current link after rename/fsync failure; prior code, sealed SQLite data and journal state agree | `test_installer_recovery_boundaries.py` |
| 8 | Completion acceptance must follow the latest action and task generation; overrides need a stored reason | `test_ops_audit_correctness.py` |
| 9 | Terminal tasks reject late heartbeats; explicit reopen increments generation and checks ownership | `test_ops_audit_correctness.py` |
| 10 | One lock covers task deduplication and append; compaction uses matching lock order | `test_ops_audit_correctness.py` |
| 11 | Doctor consumes actual timer/service states, report failures and issues, including disabled timers | `test_ops_audit_correctness.py` |
| 12 | Doctor compares bounded SQL rows with hydration and reports unreadable payload references | `test_ops_audit_correctness.py` |
| 13 | Unsupported owner platforms are explicitly skipped; imports no longer eagerly require `fcntl` | `test_ops_audit_correctness.py` |
| 14 | Diagnostic timer checks cannot send notifications; explicit notifier does not also send a webhook | `test_ops_audit_correctness.py` |
| 15 | Recovery restores and verifies enabled, active closure watchers; watcher failure retains the recovery journal | `test_installer_recovery_boundaries.py` |
| 16 | Episode backrefs enforce scope/source authorization regardless of relevance-admission configuration | `test_audit_recall_boundaries.py` |
| 17 | Live capability scores/counts require verified host evidence; replay/unverified/legacy counts remain distinct; historical unclassified outcome scores are excluded from the default ledger | `test_audit_capability_outcome_classes.py`, `test_capability_ledger.py` |

## Verification

The final consolidated focused batch passed **130 tests** in 22.00 seconds:

```text
tests/test_version.py
tests/test_promotion_audit_regressions.py
tests/test_promotion_watch.py
tests/test_policy_rollout.py
tests/test_audit_recall_boundaries.py
tests/test_ops_audit_correctness.py
tests/test_payload_segment_live_recovery.py
tests/test_installer_recovery_boundaries.py
tests/test_audit_capability_outcome_classes.py
tests/test_openclaw_policy_attribution.py
tests/test_capability_ledger.py
```

Adjacent focused suites were also exercised during implementation: existing
storage/installer, task/doctor/timer, adapters/archived projections, explicit
capture/recall, dynamic catalog and L5-readiness tests. Their overlapping counts
are not added to the consolidated total. `git diff --check` and installer
`bash -n` passed. Independent integration review found and verified fixes for
historical replay counters, unbound attribution, recall-time version capture,
and the version-check/write-lock race.

## Deliberate boundaries

- Crash tests use real Linux subprocess exits, files, symlinks, SQLite snapshots
  and transaction journals in temporary directories. Systemd and external
  service effects are simulated. No production fault injection was performed.
- Windows lock/import behavior is simulated here; native Windows rerun remains
  separate platform validation.
- Legacy compatibility remains explicitly selectable and diagnostic; replay
  never appears in production outcome counts. No claim is made about bypassing
  or satisfying the complete v4 L5 gate.
- Existing authoritative memory envelopes retain original links for payload
  attestation. Unauthorized episode expansion and evidence metadata are blocked;
  this does not introduce a new envelope-redaction policy.
- Routine rollout uses immutable deployment and health verification with
  optional pre-switch L5 bootstrap and full release-closure disabled, as requested.
