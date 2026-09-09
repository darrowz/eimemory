# Recall critical path and complete acceptance

Accepted direction: the operator requested all repairs and final commit/deployment/acceptance after reviewing the repeated-failure diagnosis on 2026-09-09. This document makes that direction executable; it is not a request to lower any acceptance gate.

## Design

The three-second governed recall must first obtain the evidence required by its admission policy. In mandatory fragment mode, separate inexpensive exact-identity lookup and authoritative hydration from full SQLite hybrid candidate scoring. The full hybrid scorer remains available for modes that use it; do not spend its global FTS/sort budget ahead of mandatory PostgreSQL fragment retrieval. Preserve exact identity exceptions, scope/source/kind checks, current authority digests, final validation, and semantic/lexical fragment fusion.

Propagate the existing absolute collection deadline into SQLite read execution under the store lock. Cancel only request-owned reads, restore connection state, distinguish budget interruption from genuine SQL errors, and return an incomplete-search diagnostic rather than false absence or an RPC exception. No deadline-bearing background thread may use the authoritative SQLite connection.

Use one dependency-free release-impact classifier for both lineage domain classification and the installer's automatic closure decision. Shared model records affect their real consumers; unknown production paths still conservatively affect all domains. Documentation/tests/version metadata alone may remain lightweight. Report technical completion separately from skipped, accumulating, failed, or complete business closure. A successfully recorded incompatible lineage is not evidence of complete acceptance.

## Constraints

- Keep the default total recall budget at 3 seconds and the existing authority/admission thresholds.
- Keep production authentication, tenant/source partitions, payload digests, snapshot/rollback safeguards, and PostgreSQL dependency installation intact.
- Never label deliberate maintenance calls as natural traffic or invent host/external delivery receipts.
- Do not equate historical task evidence with an authoritative latest task state. The repository currently has no such task ledger; its source must be identified before claiming current-state completeness.
- Do not alter production during implementation or instantiate Runtime on live storage for a replay.
- Commit and deploy only reviewed, tested code; preserve every failed acceptance batch.

## Alternatives and choice

Increasing deadlines would conceal the three-second contract problem; merely adding another local micro-optimization would leave mandatory evidence behind full hybrid search. Choose policy-aware critical-path separation plus statement cancellation, then verify real data. For release impact, choose a shared classifier instead of expanding a second independent shell whitelist.

## Acceptance

Focused red/green tests cover mandatory fragment retrieval despite expensive local hybrid search, exact identities, normal non-fragment behavior, invalid authorities, SQL interruption and restoration, and release-impact/closure status truthfulness. Real-data validation uses independent fresh processes, changing first-query order and a bounded concurrent-request scenario; distinguish process cold starts from unproven OS-cache coldness. After the installer actually exits, run native production regression and the formal closure path, reporting complete versus external evidence deficits separately. Current-state task verification is blocked only on its authority source, not silently replaced by a historical relevance test.
