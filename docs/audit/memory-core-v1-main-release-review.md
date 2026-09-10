# Memory core 1.13.10 — main-thread pre-release review

Authorized scope: memory-core repair, paired default-Hermes host handoff, controlled release, scoped existing-history repair and live original-query acceptance. No global marker reset, scope/permission relaxation, raw-history deletion, alternate-profile mutation, or formal-gate label fabrication.

## Reproducible host dependency

Host commit `b4cabb0ad1123a81956f8e5ec092cb4b4f0a8a90`, based on `693641aa8b4359c602283bdbbc14041e03bc47bc`, is packaged under `integrations/hermes/host-patches/`. It opts in to bounded immutable completed-turn snapshots; legacy providers retain their contract. The host repository has only its upstream remote; no upstream push or new fork is attempted. Main will fast-forward the clean default host checkout through the effect-owner deployment unit. The companion patch is distributed in this repository for reproducibility.

## Independently exercised evidence

- Candidate core plus actual candidate host: **252 passed in 16.32s**, nine focused files. Log `main-final-core-bound-tests.log` in the private working evidence directory. Both host module origins were printed and verified before collection.
- Host provider/snapshot tests: **79 passed**, two files, 4.8s. Not added to overlapping integration counts.
- Exact scoped parent records, fresh disposable RuntimeStore: phone extraction and project-support derivation positive; original raw content unchanged; repeated extraction and support insertion idempotent. This exercises real source metadata, not only sanitized fixture projections.
- Historical supporting tool/assistant bodies were read back by their exact persistent IDs and session and compared with the scoped export. No body replacement or synthetic project label.
- Independent sensitive-support probe: `derived=false`, `synthetic_sensitive_line_persisted=false`.
- Version synchronized in main package and both plugin manifests. No whole-repository test run.

Retained failed independent checks: an initial combined test run imported the installed old host at collection and yielded 4 race failures / 248 passes. Explicit candidate-host PYTHONPATH and pre-collection module-origin verification corrected the test environment, without changing assertions. An initial standalone sensitive probe omitted the tests import path; corrected environment produced the result above. Earlier exact-parent replay exposed non-persisted L1 completion and duplicate retries; the scoped L1 repair has separate red/green evidence in `l1-capture-idempotency.md`.

## Production acceptance boundary

These are pre-release checks, not live acceptance. The authorized deployment must use the existing immutable installer and storage transaction unchanged. Production history repair is limited to two previously verified same-user/same-channel parents; it must revalidate source content and historical persisted messages, use existing atomic write/outbox paths, retain raw source, and reread exact written targets. Index maintenance uses the normal bounded service. Live probes cover supply/correction, phone identity, explicit deployment history with citations, project-scoped latest *known* history (not live Kanban truth), and absent contract-amount rejection, including first and repeated requests. Keep every failed response. The AI semantic review is not user-approved formal gate labels, L5 or a global quality percentage.

On a production deployment failure, stop further releases and audit installer rollback/current identity/transaction markers. Do not change gates to make the release pass. On post-release acceptance failure, report the actual online state and failing criterion, not completion.
