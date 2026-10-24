# Explicit offline reranker comparison

`RelevanceAdmission.select` preserves the authorized fused order. Enabling its
configuration or supplying a scorer does not call a model; its report includes
`reranker_called: false`. Online recall does not import or call the shadow
evaluation entry point.

`eimemory.retrieval.reranker_shadow.evaluate_reranker_shadow` is a separate,
callable evaluation utility. It is disabled by default and does not read online
environment flags or construct a client. Running it requires an explicit
`ShadowConfig(enabled=True)`, an injected scorer, and an authority validator.
The scorer may be an offline fixture or an explicitly configured local
`TEIReranker`; the utility itself never selects a service or starts one.

## Bounds and evidence

The candidate cap is configurable from 20 to 32, with 24 as the default. A case
may contain fewer authorized candidates. The input scan, case count, projected
text length, per-case budget and total batch budget are bounded independently.
Candidates retain their original fused order until the separate comparison is
computed. The caller's list and native records are never rewritten.

Each `ShadowCase` carries exact `CandidateRef` authorization from the controlled
retrieval: record ID, four-part scope and source ID. A matching record ID in a
different permission or source partition is insufficient. Inactive records and
records rejected by the fresh validator are excluded before text projection.
The bounded batch is checked again immediately before projection, and its
identity, active status, fresh authorization and complete record digest are
checked after scoring. A changed snapshot invalidates the comparison.

For native data, the validator must use `get_by_exact_ref(record_id,
scope=record.scope, source_id=record.source_id)` and compare the active native
record's digest with the supplied record. Derive the allowed reference set from
the trusted retrieval authorization boundary, not from arbitrary candidate
payload claims. The synthetic example below uses a fixed local fixture scope
and source; it is not a production authorization recipe.

The scorer receives a timeout and the smaller of the batch, case and caller
deadlines. The supplied scorer must honor that budget; the existing TEI client
implements transport timeouts and remaining-budget checks. This synchronous
offline utility cannot forcibly interrupt an arbitrary uncooperative Python
callback. It discards late results, reports the actual elapsed time, and never
connects that failure to online delivery.

## Executable synthetic example

This example runs without a model or network. Its score function deliberately
reverses the fixture candidates to demonstrate the report contract. Its numbers
do not establish model quality or a production recall improvement.

```python
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.retrieval.contracts import CandidateRef, ExactScope
from eimemory.retrieval.reranker_shadow import (
    ShadowCase, ShadowConfig, evaluate_reranker_shadow,
)

scope = ScopeRef(tenant_id="offline-fixture", user_id="fixture-owner")
records = tuple(RecordEnvelope.create(
    kind="memory", title=f"Fixture {index}", scope=scope,
    source_id="fixture", content={"text": f"Synthetic fact {index}"},
) for index in range(24))
refs = tuple(CandidateRef(r.record_id, ExactScope.from_scope(scope), "fixture")
             for r in records)

class FixtureScorer:
    def score(self, query, texts, *, timeout_seconds, deadline_at):
        return list(range(len(texts)))

case = ShadowCase(
    case_id="synthetic-001", query="Synthetic question", candidates=records,
    authorized_refs=frozenset(refs), relevant_refs=frozenset({refs[-1]}),
)
report = evaluate_reranker_shadow(
    [case], scorer=FixtureScorer(), config=ShadowConfig(enabled=True),
    validate=lambda r: r.scope == scope and r.source_id == "fixture"
                       and r.status == "active" and r in records,
)
assert report["model_calls"] == 1
assert report["affects_delivery"] is False
assert report["shadow"]["precision_at_1"] == 1.0
```

## Reading the report

`relevant_refs=None` means unlabelled. An explicitly empty set means a labelled
negative query. Labels are evaluated after scoring and never sent to the scorer
or used to select its inputs. A label outside the supplied authorization set is
rejected as an invalid evaluation case.

- Candidate Recall@K uses the actual bounded authorized input set and positive
  labels. A positive reference excluded by the candidate cap remains a miss.
- P@1, P@5, Recall@5, MRR and NDCG use the first five ranked results. P@5 has a
  fixed denominator of five; MRR is truncated at five.
- False-positive rate is the fraction of labelled negative queries that return
  a candidate. The comparison changes order only, so it cannot claim to repair
  negative-query admission merely by moving candidates around.
- Aggregate ranking quality uses successfully completed, labelled pairs.
  Attempted cases, unlabelled cases, unavailable cases and input-budget losses
  remain explicit; a failed comparison is never counted as a successful one.
- P50/P95 use the nearest-rank estimator over the elapsed times of **all**
  attempted cases, including failures. The sample count is reported; a tiny
  synthetic sample is not an operational P95 estimate.

Reports include opaque hashes of exact references, bounded counters, configured
scorer identity when available, and quality/latency values. They exclude query
and evidence text, scope/source strings, credentials and exception bodies.
Real acceptance still needs a pinned real model, independently labelled
development and untouched holdout cases, negative and forbidden-reference
checks, and a representative latency sample. This utility does not provide that
evidence by itself.

## Lightweight page-candidate boundary

Lightweight admission retains up to three native sibling candidates for each
`page_pool_key` by default (`EIMEMORY_LIGHTWEIGHT_PAGE_CANDIDATES`, range 1–8).
Each candidate keeps its own indexed fragment, vector score, source and scope.
The answer requirements and evidence band are checked before the final page
representative is selected. Counts describe losses at authorization, bounded
pooling, fragment validation, answer requirements, the evidence band, page
selection and final validation; counts alone do not establish Recall@K.

This repair concerns native sibling records sharing a page identity. It does
not change PostgreSQL's existing one-fragment-per-parent candidate contract.
An unscored span from the same parent must never borrow another span's vector
score. Supporting multiple indexed spans for one parent requires a separate,
explicitly bound candidate-hint and backend change.
