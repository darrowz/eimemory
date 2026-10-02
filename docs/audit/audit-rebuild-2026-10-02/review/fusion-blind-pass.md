# Blind source pass: fusion and query identity

Baseline: **4763001d1c4f3f4af6e6dda17e008e1b4c9b5609** (1.14.31).

**Result: no actionable defect established in this bounded source pass.** The reviewer received only the two module paths and caller-navigation suggestion before reading them. No candidate finding or prior audit outcome was supplied.

## Exact scope

- `retrieval/fusion.py` full 1–191: six functions
- `retrieval/query_identity.py` full 1–12: two functions
- Caller context fully read: `retrieval/engine.py` 1352–1640 (outer fusion/pooling function plus one nested helper), and 2273–2287 (token helper); three functions, reported separately
- Call-site-only context: proactive.py 360–361; not counted as a whole-function review

Hashes, graph symbol IDs, and actual source call sites are recorded in `fusion-blind-pass-coverage.json`. A static import points to a possible dependency; the listed call sites were also read directly in the fixed source. No runtime execution is inferred from the graph.

## Findings that constrain future changes

- Weighted RRF validates supported component names, rejects duplicate component arms, bounds k/weights/result count and input IDs per component, deduplicates IDs within each arm, and uses record ID as a score tie-breaker
- Engine calls the fuser twice: once to normalize the policy (1408), then for each scope group (1479). It intentionally retains unranked group records at zero fusion score for later pooling/ambiguity handling. Removing that fallback is not a cosmetic cleanup
- The fuser truncates generic IDs to 256 characters, but this inspected production path supplies a 77-character hash token from the complete record/scope/source reference (engine 2273–2287). A raw long-ID collision concern is therefore not established for this path
- `page_pool_key` hashes complete identifier descriptors, including all four scope fields, source ID, and the selected identity type. Long identifiers are not prefix-truncated. Page, parent, document, raw-session/event, and record fallback priority is behavior-bearing
- The small query identity module retains the established SHA-256 serialization of task type, unit separator, and query. The proactive call site uses it for its effective-query digest. A format change would require an explicit migration/compatibility plan
- Existing fusion tests were inspected only as contract context. No old pass results were inherited, and no test was executed in this pass

## Limits

No full-engine review, retrieval-quality proof, real model, SQL connection, production request, or compatibility test result is claimed. Generic callers outside the inspected hashed-token path and arbitrary-input digest serialization require separate contract analysis before a change. These modules remain unchanged.
