"""Read-only candidate diagnostics for one independently labeled recall target.

The existing native observer binds one target to one invocation. Multiple
labels, incomplete windows, and unobserved ranks remain unknown. No target is
inserted into retrieval and no query is repeated to manufacture a candidate pool.
"""
from __future__ import annotations

from eimemory.models.records import ScopeRef
from eimemory.recall.query_clean import clean_user_query
from eimemory.retrieval.contracts import CandidateRef, ExactScope
from eimemory.retrieval.target_trace import safe_target_trace_report, target_trace_scope


SCHEMA = "single_invocation_candidate_metrics.v1"


def _unknown(k, reason, *, applicable=True):
    return {"schema": SCHEMA, "k": k, "status": "unknown" if applicable else "not_applicable",
            "reason": reason, "candidate_recall_at_k": None,
            "first_loss": None, "first_loss_scope": "first_observed_adjacent_transition",
            "diagnostic_only": True, "does_not_certify_answer": True}


def score_candidate_trace(report, *, k):
    trace = safe_target_trace_report(report)
    result = {**_unknown(k, "native_trace_not_completed"), "target_trace": trace}
    if trace["status"] != "completed":
        return result
    rows = trace["stages"]
    candidate = next(row for row in rows if row["stage"] == "provider_bounded")
    count = candidate["observed_count"]
    presence = candidate["target_presence"]
    if candidate["window_complete"] is not True or type(count) is not int:
        result["reason"] = "candidate_window_incomplete"
    elif presence == "absent_in_observed_window":
        result.update(status="observed", reason="", candidate_recall_at_k=0.0)
    elif presence == "present" and count <= k:
        # A target in a fully observed pool no larger than K is provably in K.
        # Native tracing does not expose a cross-provider rank for larger pools.
        result.update(status="observed", reason="", candidate_recall_at_k=1.0)
    else:
        result["reason"] = "candidate_rank_not_observed"
    for before, after in zip(rows, rows[1:]):
        if (before["target_presence"] == "present"
                and after["target_presence"] == "absent_in_observed_window"
                and after["window_complete"] is True):
            result["first_loss"] = {"from_stage": before["stage"], "to_stage": after["stage"],
                                    "drop_reasons": list(after["drop_reasons"])}
            break
    return result


def recall_with_candidate_trace(runtime, *, case, context, k):
    """Observe the original recall call without altering its retrieval inputs."""
    kwargs = {"query": case["query"], "scope": case["scope"], "limit": 5, "task_context": context}
    refs = [ref for group in case["expected_groups"] for ref in group]
    if len(refs) != 1:
        report = _unknown(k, "multiple_expected_refs_not_traced" if refs else "negative_case_has_no_target",
                          applicable=bool(refs))
        return runtime.memory.recall(**kwargs), report
    scope = ScopeRef.from_dict(case["scope"])
    with target_trace_scope(runtime.memory.recall_engine,
            CandidateRef(refs[0], ExactScope.from_scope(scope), case["source_id"]),
            query=clean_user_query(case["query"].strip()), source_ids=context["source_ids"]) as trace:
        bundle = runtime.memory.recall(**kwargs)
    return bundle, score_candidate_trace(trace.report(), k=k)


def summarize_candidate_diagnostics(samples):
    positive = [sample for sample in samples if sample["metrics"]["hit_at_1"] is not None]
    observations = [sample.get("candidate_diagnostics") or {} for sample in positive]
    known = [item for item in observations if item.get("status") == "observed"
             and type(item.get("candidate_recall_at_k")) in (int, float)
             and item["candidate_recall_at_k"] in (0, 1)]
    ks = {item.get("k") for item in observations if type(item.get("k")) is int}
    losses = {}
    for item in observations:
        transition = item.get("first_loss")
        if isinstance(transition, dict):
            stage = transition["to_stage"]
            losses[stage] = losses.get(stage, 0) + 1
    return {"schema": SCHEMA, "k": next(iter(ks)) if len(ks) == 1 else None,
            "positive_count": len(positive), "known_positive_count": len(known),
            "unknown_positive_count": len(positive) - len(known),
            "coverage": len(known) / len(positive) if positive else None,
            "candidate_recall_at_k": (sum(item["candidate_recall_at_k"] for item in known) / len(known)
                                      if known and len(ks) == 1 else None),
            "first_loss_counts": dict(sorted(losses.items())),
            "denominator": "known_single_target_positive_invocations",
            "unknown_is_not_zero": True, "diagnostic_only": True}
