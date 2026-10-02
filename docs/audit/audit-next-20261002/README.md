# Audit-next first verified batch, 2026-10-02

Baseline: a12be3507bc7b2461377679acdff094b1228476c (1.14.32).

## Changes
- Persist below-threshold promotion observations inside the existing SQLite transaction.
- Preserve already-recognized high-risk categories through candidate aggregation.
- Only ignore a single top-level, single-target string version declaration during impact classification; share the normalizer with lineage.
- Include removed source paths as well as destinations when classifying renames.

## Verification scope
Independent combined run: 227 passed in 14.01 seconds; 89 new regression cases and 138 existing adjacent cases. Tests use real modules with synthetic values, temporary SQLite databases and temporary Git repositories. This is not full-suite, production, deployment, promotion-policy or recall-quality acceptance.

Four separately tested existing failures also reproduce on the unmodified baseline: an existing promotion fixture fails the health_identity_unbound prerequisite; two capability rollback tests fail; an active-surface test reports active_surface_scan_unavailable. These are not changed to obtain a passing report.

A separate exploratory medium/unknown risk-ordering issue is not fixed in this first batch. No unreviewed follow-up changes are included. Compatibility aliases remain; sharing the version normalizer removes a duplicate maintenance point, not a project-wide size reduction claim.

The successful bounded combined log and case list are included. Only synthetic test evidence is published; no production data, credentials or private execution paths are included.
