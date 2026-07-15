# Autonomous Learning Evidence Integrity Design

## Goal

Prevent unavailable replay evidence and noisy terminal transcripts from being recorded as real capability regressions or learnable user corrections.

## Design

1. Capability replay uses three states: `pass`, `fail`, and `not_run`. Only executed `pass`/`fail` cases contribute to a score. If every case is `not_run`, persist diagnostics and the manifest, but do not write a capability score.
2. The autonomous learning loop runs capability acceptance first, then replays only capabilities whose complete case set has passing, bound probes. Capabilities without complete acceptance remain unassessed and keep their prior ledger state.
3. Terminal OpenClaw events receive a quality gate before correction inference and learnable outcome persistence. Mixed transcript envelopes and low-quality voice text are retained only as diagnostics, not as trusted corrections or capability evidence.
4. Reporting reuses the same noise classification so quarantined input cannot reappear as a learned item.
5. A failed candidate experiment remains candidate-level evidence. It does not write a zero global capability score; legacy zero scores created by failed gates are excluded from the effective ledger.

## Verification

- A replay pack without contract evidence produces `not_run`, no score record, and leaves the prior ledger score unchanged.
- A bound acceptance/replay run produces passing scores; unbound capabilities create neither scores nor empty manifests.
- Noisy mixed terminal input does not create a learnable bad outcome or correction.
- Clean explicit corrections still work.
- Failed candidate gates leave the last verified capability score unchanged.
- Full test suite passes; production version is bumped, deployed, and `/health` reports the new version/commit.
