"""Source-bound controlled retrieval checks, separate from production gold.

Existing authorized records provide positive known-item expectations only.
The resulting questions are controlled probes, never natural production
queries, trusted quality labels, or independently established no-answer cases.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
from math import isfinite
from time import perf_counter
from typing import Any

from eimemory.adapters.runtime.channel import runtime_channel_from_scope
from eimemory.api.memory import MemoryAPI
from eimemory.contracts.recall_boundary import authority_digest, default_recall_blocked_lane, exact_ref
from eimemory.evaluation.metrics import percentile
from eimemory.metadata import business_metadata
from eimemory.models.records import ScopeRef
from eimemory.models.source_partitions import normalize_source_ids
from eimemory.recall import is_inactive_or_superseded_record, is_outcome_pollution_record
from eimemory.retrieval.diagnostics import ADMISSION_DROPS, ENGINE_DROPS, compact_recall_diagnostics
from eimemory.retrieval.stage_diagnostics import retrieval_stage_diagnostics

CONTROLLED_MEMORY_BENCHMARK_SCHEMA = "controlled_memory_benchmark.v1"
CONTROLLED_MEMORY_DATASET_KIND = "controlled_memory_benchmark"
MAX_CONTROLLED_CASES = 50
MAX_SCAN_RECORDS = 512
_KINDS = ("memory", "knowledge_page", "knowledge_unit", "claim_card")
_ELIGIBILITY_REASONS = frozenset({"source_not_authorized", "source_record_inactive", "source_record_expired",
                                 "source_record_not_yet_valid", "source_record_invalid_temporal_binding",
                                 "source_record_operational", "source_record_missing", "source_query_material_empty",
                                 "source_not_stable_before_cutoff", "source_created_at_invalid"})
_GENERATION_REASONS = frozenset({"", "authorized_source_ids_missing", "controlled_memory_store_unavailable",
                                "controlled_memory_benchmark_empty", "controlled_memory_cutoff_invalid"})
_SELECTOR_DROPS = frozenset(ADMISSION_DROPS) | {
    "grounding", "score", "top_score_margin", "exact_dominance", "durable_event_dominance",
    "recall_budget_exhausted", "candidate_hydration_timeout", "authority_unavailable", "authority_changed",
}
_GATE_DROPS = frozenset({"inactive_or_superseded", "stale_rule", "stale_memory",
    "external_knowledge_quarantined", "external_knowledge_untrusted", "agent_outcome", "tool_call",
    "diagnostic", "deployment", "episode_evidence", "operational", "raw", "evidence_only",
    "run_log", "audit_record", "incident_report", "evolution_artifact", "task_context",
    "source:blocked", "source:not_allowed", "memory_type:not_allowed", "organ:not_allowed",
    "intent_kind:suppressed", "recall_lane:not_allowed", "task_evidence_missing", "preference_attribute_missing"})
_ENGINE_DROPS = frozenset(ENGINE_DROPS) | {
    "query_scope_limit", "raw_source_not_allowed", "raw_ref_missing", "raw_scope_not_allowed",
    "raw_not_authoritative", "provider_over_limit", "preference_candidate_limit",
    "preference_collection_timeout", "preference_collection_unavailable", "scope_not_allowed",
    "source_not_allowed", "missing_or_corrupt_record", "ref_mismatch", "inactive_record",
    "quality_rejected", "kind_not_allowed", "pre_fusion_scope_source_rejected", "anchor_reserve_swap",
    *("relevance_selector:" + reason for reason in _SELECTOR_DROPS),
}
_RETRIEVAL_STATUSES = frozenset({"unknown", "evidence_found", "no_evidence", "unavailable", "degraded",
                                "ambiguous", "available", "not_run", "not_reported"})
_FALLBACK_CODES = frozenset({"unknown", "disabled", "not_configured", "circuit_open", "index_lag_exceeded",
    "index_lag_unknown", "recall_budget_exhausted", "embedding_unavailable", "embedding_timeout",
    "postgres_unavailable", "postgres_timeout", "index_not_ready", "scope_not_indexed"})


def build_controlled_memory_benchmark(
    runtime: Any, *, scope: dict | ScopeRef, source_ids: list[str] | tuple[str, ...], limit: int = 10,
    stable_before: str = "",
) -> dict[str, Any]:
    """Read a bounded exact-scope snapshot without seeding or granting access."""
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    sources = normalize_source_ids(source_ids)
    budget = max(1, min(MAX_CONTROLLED_CASES, int(limit)))
    scan_limit = min(MAX_SCAN_RECORDS, max(64, budget * 8))
    skipped: Counter[str] = Counter()
    try:
        cutoff = _timestamp(stable_before) if stable_before else None
    except (TypeError, ValueError):
        return _dataset(scope_ref, sources or (), [], skipped, 0, False,
                        "controlled_memory_cutoff_invalid", ok=False)
    cutoff_text = cutoff.isoformat().replace("+00:00", "Z") if cutoff else ""
    if not sources:
        return _dataset(scope_ref, (), [], skipped, 0, False, "authorized_source_ids_missing", stable_before=cutoff_text)
    store = getattr(runtime, "store", None)
    if not callable(getattr(store, "list_records", None)):
        return _dataset(scope_ref, sources, [], skipped, 0, False, "controlled_memory_store_unavailable",
                        stable_before=cutoff_text)
    records = store.list_records(kinds=list(_KINDS), scope=scope_ref, status="active",
                                 source_ids=list(sources), limit=scan_limit + 1)
    cases = []
    scanned = 0
    for record in records[:scan_limit]:
        scanned += 1
        reason = _eligible_reason(record, scope_ref, sources) or _stable_source_reason(record, cutoff)
        if reason:
            skipped[reason] += 1
            continue
        # Re-read through the exact source partition before binding the case.
        current = store.get_by_exact_ref(record.record_id, scope=scope_ref, source_id=record.source_id)
        reason = ("source_record_missing" if current is None
                  else _eligible_reason(current, scope_ref, sources) or _stable_source_reason(current, cutoff))
        if reason:
            skipped[reason] += 1
            continue
        case = _case(current)
        if case is None:
            skipped["source_query_material_empty"] += 1
            continue
        cases.append(case)
        if len(cases) >= budget:
            break
    return _dataset(scope_ref, sources, cases, skipped, scanned,
                    len(records) > scan_limit, "" if cases else "controlled_memory_benchmark_empty",
                    stable_before=cutoff_text, selection_truncated=len(cases) >= budget and scanned < len(records))


def run_controlled_memory_benchmark(
    runtime: Any, dataset: dict[str, Any], *, scope: dict | ScopeRef,
    source_ids: list[str] | tuple[str, ...],
) -> dict[str, Any]:
    """Measure the configured runtime recall path and return reference-only results.

    Caller authority is supplied independently from the generated dataset. Each
    source is re-read immediately before and after recall; stale/missing sources
    cannot turn an empty result into a successful expectation.
    """
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    sources = normalize_source_ids(source_ids)
    generation = _generation_metadata(dataset.get("dataset_generation"))
    cases = dataset.get("cases") or []
    base = {
        "ok": True, "execution_ok": True, "schema_version": 2, "report_type": "memory_eval_ci",
        "name": "controlled-memory-known-item", "scope": asdict(scope_ref),
        "dataset_kind": CONTROLLED_MEMORY_DATASET_KIND, "dataset_schema": CONTROLLED_MEMORY_BENCHMARK_SCHEMA,
        "formal_business_evidence": False, "certifies_natural_queries": False,
        "certifies_no_answer": False, "memory_benchmark_accepted": False,
        "dataset_generation": generation, "seeded_record_ids": [], "incident_record_ids": [],
        "threshold": 1.0, "retrieval_case_count": len(cases),
    }
    invalid = (dataset.get("schema") != CONTROLLED_MEMORY_BENCHMARK_SCHEMA
               or dataset.get("dataset_kind") != CONTROLLED_MEMORY_DATASET_KIND
               or dataset.get("formal_business_evidence") is not False
               or generation.get("ok") is not True
               or ScopeRef.from_dict(dataset.get("scope")) != scope_ref
               or not sources or not isinstance(cases, list) or len(cases) > MAX_CONTROLLED_CASES
               or not callable(getattr(getattr(runtime, "memory", None), "recall", None)))
    if invalid or not cases:
        reason = (generation.get("reason") if generation.get("reason") == "controlled_memory_cutoff_invalid"
                  else "controlled_memory_dataset_invalid" if invalid
                  else generation.get("reason") or "controlled_memory_benchmark_empty")
        return {**base, "ok": not invalid, "execution_ok": not invalid,
                "memory_benchmark_status": "not_run", "blocked_reason": reason,
                "eval_skipped_reason": reason, "sample_count": 0, "pass_count": 0, "fail_count": 0,
                "pass_rate": None, "passed_threshold": False, "controlled_benchmark_passed_threshold": False,
                "samples": [], "failures": []}
    samples = [_run_case(runtime, case, scope_ref, sources) for case in cases]
    pass_count = sum(sample["passed"] for sample in samples)
    evaluated = sum(sample["evaluable"] for sample in samples)
    passed = pass_count == len(samples) and evaluated == len(samples)
    execution_ok = not any(sample.get("failure_reason") in {
        "controlled_retrieval_unavailable", "controlled_retrieval_incomplete"} for sample in samples)
    elapsed = [sample["latency_ms"] for sample in samples]
    return {**base, "ok": execution_ok, "execution_ok": execution_ok,
            "memory_benchmark_status": "evaluated" if evaluated else "not_run",
            "blocked_reason": "" if evaluated else "controlled_memory_cases_not_evaluable",
            "eval_skipped_reason": "" if evaluated else "controlled_memory_cases_not_evaluable",
            "sample_count": len(samples), "evaluated_case_count": evaluated,
            "not_evaluable_count": len(samples) - evaluated,
            "pass_count": pass_count, "fail_count": len(samples) - pass_count,
            "pass_rate": round(pass_count / len(samples), 3), "passed_threshold": passed,
            "controlled_benchmark_passed_threshold": passed,
            "samples": samples, "failures": [sample for sample in samples if not sample["passed"]],
            "efficiency": {"case_count": len(samples), "latency_ms_avg": round(sum(elapsed) / len(elapsed), 3),
                           "latency_ms_p95": percentile(elapsed, 95)}}


def _run_case(runtime: Any, case: Any, scope: ScopeRef, sources: tuple[str, ...]) -> dict[str, Any]:
    started = perf_counter()
    sample: dict[str, Any] = {"case_id": "", "phase": "usage", "passed": False, "evaluable": False,
                              "expected_rank": 0, "returned_record_count": 0, "returned_refs": [],
                              "recall_at_k": 0.0, "mrr": 0.0}

    def finish(reason: str = "") -> dict[str, Any]:
        sample["latency_ms"] = round((perf_counter() - started) * 1000, 3)
        if reason:
            sample["failure_reason"] = reason
        return sample

    if not isinstance(case, dict) or not isinstance(case.get("source_binding"), dict):
        return finish("controlled_case_invalid")
    binding = case["source_binding"]
    sample.update(case_id=str(case.get("case_id") or "")[:128], source_binding=_binding_metadata(binding))
    if ScopeRef.from_dict(binding.get("scope")) != scope or binding.get("source_id") not in sources:
        return finish("source_not_authorized")
    current = runtime.store.get_by_exact_ref(str(binding.get("record_id") or ""), scope=scope,
                                             source_id=binding["source_id"])
    reason = "source_record_missing" if current is None else _eligible_reason(current, scope, sources)
    if reason:
        return finish(reason)
    canonical = _case(current)
    if canonical is None or canonical != case:
        return finish("source_case_binding_changed")
    try:
        bundle = runtime.memory.recall(query=case["query"], scope=asdict(scope),
                                       task_context=dict(case["task_context"]), limit=case["limit"])
    except Exception as exc:
        sample["error_type"] = type(exc).__name__
        sample["retrieval_diagnostics"] = _controlled_retrieval_diagnostics(
            {}, task_context=case["task_context"], returned_count=0, operation_unavailable=True)
        return finish("controlled_retrieval_unavailable")
    returned = list(bundle.items)[:case["limit"]]
    sample["returned_refs"] = [_binding(item) for item in returned]
    sample["returned_record_count"] = len(returned)
    explanation = bundle.explanation or {}
    sample["retrieval_diagnostics"] = _controlled_retrieval_diagnostics(
        explanation, task_context=case["task_context"], returned_count=len(returned))
    if explanation.get("retrieval_status") in {"unavailable", "degraded"}:
        return finish("controlled_retrieval_incomplete")
    # Do not certify a result if source authority changed while retrieval ran.
    after = runtime.store.get_by_exact_ref(current.record_id, scope=scope, source_id=current.source_id)
    if after is None or _eligible_reason(after, scope, sources) or authority_digest(after) != binding["payload_digest"]:
        return finish("source_case_binding_changed")
    sample["evaluable"] = True
    if any(_eligible_reason(item, scope, sources) for item in returned):
        return finish("returned_source_not_authorized_or_current")
    ranks = [rank for rank, item in enumerate(returned, 1) if exact_ref(item) == exact_ref(current)
             and authority_digest(item) == binding["payload_digest"]]
    rank = ranks[0] if ranks else 0
    sample.update(expected_rank=rank, recall_at_k=float(bool(rank)),
                  mrr=round(1 / rank, 3) if rank else 0.0, passed=bool(rank))
    return finish("" if rank else "known_item_missing")


def _controlled_retrieval_diagnostics(explanation: Any, *, task_context: dict[str, Any],
                                      returned_count: int, operation_unavailable: bool = False) -> dict[str, Any]:
    """Project this operation's measured stages; absence stays uncertified."""
    explanation = explanation if isinstance(explanation, dict) else {}
    stage = retrieval_stage_diagnostics(explanation, trusted_retrieval=True)
    compact = compact_recall_diagnostics(explanation)
    raw_engine = explanation.get("engine_diagnostics")
    raw_engine = raw_engine if isinstance(raw_engine, dict) else {}
    selector = explanation.get("relevance_selector")
    selector = selector if isinstance(selector, dict) else {}

    def count(value: Any) -> int | None:
        return min(value, 1_000_000) if type(value) is int and value >= 0 else None

    def project(value: dict[str, Any], *, drops: frozenset[str], raw: Any = None) -> dict[str, Any]:
        raw = raw if isinstance(raw, dict) else {}
        result = {key: item for key, item in value.items() if key in (
            "input_count", "candidate_count", "candidate_limit", "selected_count", "retrieved_count",
            "search_limit", "query_scope_count", "elapsed_ms")
            and (count(raw.get(key)) is not None if key != "elapsed_ms"
                 else (type(raw.get(key)) is int and raw[key] >= 0
                       or type(raw.get(key)) is float and isfinite(raw[key]) and raw[key] >= 0))}
        if value.get("status") in _RETRIEVAL_STATUSES:
            result["status"] = value["status"]
        if value.get("mode") in {"enforced", "bypassed", "identity_lookup", "lightweight_evidence"}:
            result["mode"] = value["mode"]
        for key in ("drops", "dropped_reasons", "blocked_counts"):
            if isinstance(value.get(key), dict):
                raw_counts = raw.get(key) if isinstance(raw.get(key), dict) else {}
                result[key] = {code: amount for code, amount in value[key].items()
                               if code in drops and count(raw_counts.get(code)) is not None}
        return result

    engine = project(stage["engine"], drops=_ENGINE_DROPS, raw=raw_engine)
    if isinstance(raw_engine.get("source_names"), list):
        engine["source_names"] = [name if isinstance(name, str) and name in {"sqlite", "sqlite+postgres_vector",
            "SQLiteCandidateSource", "PostgresVectorCandidateSource"} else "other_configured_source"
            for name in raw_engine["source_names"][:8]]
    if stage["engine"].get("fallback_reason") in {"legacy_scan", "candidate_source_fallback", "empty_source_allowlist"}:
        engine["fallback_reason"] = stage["engine"]["fallback_reason"]
    if "fallback_error_codes" in stage["engine"]:
        fallback_counts: Counter[str] = Counter()
        raw_codes = raw_engine.get("fallback_error_codes")
        raw_codes = raw_codes if isinstance(raw_codes, dict) else {}
        for key, value in list(raw_codes.items())[:8]:
            amount = count(value)
            if amount is not None:
                fallback_counts[key if key in _FALLBACK_CODES else "unknown"] += amount
        engine["fallback_error_codes"] = {key: min(value, 1_000_000) for key, value in fallback_counts.items()}
    raw_pipeline = explanation.get("pipeline")
    raw_phases = raw_pipeline.get("phases") if isinstance(raw_pipeline, dict) else []
    raw_phases = raw_phases if isinstance(raw_phases, list) else []
    phases = [{"name": phase["name"], **project(phase, drops=_GATE_DROPS, raw=raw_phase)}
              for phase, raw_phase in zip(stage["pipeline"], raw_phases[:8])
              if phase.get("name") in {"prepare", "retrieve", "graph_expand", "score_filter", "package"}]
    retrieval_status = stage["retrieval_status"] if stage["retrieval_status"] in _RETRIEVAL_STATUSES else "unknown"
    complete = selector.get("collection_complete") if type(selector.get("collection_complete")) is bool else None
    source_searches = count(raw_engine.get("source_searches"))
    source_budget_exhausted = count(raw_engine.get("source_budget_exhausted"))
    fallback = raw_engine.get("fallback") if type(raw_engine.get("fallback")) is bool else None
    unavailable = operation_unavailable or retrieval_status in {"unavailable", "degraded"} or complete is False
    unavailable = unavailable or bool(source_budget_exhausted)
    availability = ("unavailable" if operation_unavailable or retrieval_status == "unavailable"
                    else "degraded" if unavailable or fallback is True
                    else "available" if complete is True and source_searches is not None and source_searches > 0
                         and source_budget_exhausted == 0 and fallback is False else "unknown")
    result = {"schema": stage["schema"], "retrieval_status": "unavailable" if operation_unavailable else retrieval_status,
              "measurement_origin": "controlled_runtime_exception" if operation_unavailable else "controlled_runtime_bundle",
              "engine": engine, "pipeline": phases,
              "selector": project(stage["selector"], drops=_SELECTOR_DROPS, raw=selector),
              "online_gate": project(stage["online_gate"], drops=_GATE_DROPS, raw=explanation.get("online_recall_gate")),
              "verifier_boundary": stage["verifier_boundary"],
              "source_availability": {"status": availability, "collection_complete": complete,
                  "source_searches": source_searches, "source_budget_exhausted": source_budget_exhausted, "fallback": fallback},
              "source_partition": {"source_ids": list(task_context["source_ids"]),
                  "target_source_id": task_context["target_source_id"], "exact_scope_only": True}}
    if isinstance(compact.get("caller_assistance"), dict):
        # The shared compact projection already uses enumerated reasons and
        # numeric bounds. No free-form names or verification session IDs.
        raw_assistance = selector.get("caller_assistance")
        raw_assistance = raw_assistance if isinstance(raw_assistance, dict) else {}
        result["assistance"] = {key: value for key, value in compact["caller_assistance"].items()
            if key not in {"proofs", "local_evidence"}
            and (count(raw_assistance.get(key)) is not None if type(value) is int
                 else type(raw_assistance.get(key)) in (int, float)
                      and isfinite(raw_assistance[key]) and raw_assistance[key] >= 0 if type(value) is float
                 else True)}
    else:
        result["assistance"] = {"status": "not_reported"}
    input_count = count(selector.get("input_count"))
    selected_count = count(selector.get("selected_count"))
    candidate_count = count(raw_engine.get("candidate_count"))
    if returned_count:
        cause = "returned_results"
    elif unavailable:
        cause = "retrieval_unavailable_or_incomplete"
    elif input_count is not None and input_count > 0 and selected_count == 0 and any(
            result["selector"].get("dropped_reasons", {}).values()):
        cause = "selector_rejected_all"
    elif candidate_count is not None and candidate_count > 0:
        # Engine drops span several searches and gate counts also include
        # auxiliary rules/reflections. Neither binds all losses to this target.
        # Retain the observations without assigning the zero-result cause.
        cause = "empty_origin_unknown"
    elif candidate_count == 0 and availability == "available":
        cause = "candidate_collection_empty"
    else:
        cause = "empty_origin_unknown"
    result["empty_result_cause"] = cause
    return result


def _eligible_reason(record: Any, scope: ScopeRef, sources: tuple[str, ...]) -> str:
    if record.scope != scope or record.source_id not in sources:
        return "source_not_authorized"
    if record.kind not in _KINDS or record.status != "active" or is_inactive_or_superseded_record(record):
        return "source_record_inactive"
    if MemoryAPI._is_temporally_stale_memory(record):
        return "source_record_expired"
    meta = business_metadata(record.meta)
    living = meta.get("living_memory_v1")
    temporal = living.get("temporal", {}) if isinstance(living, dict) else {}
    for value in (meta.get("valid_from"), meta.get("effective_from"), temporal.get("valid_from")):
        if not value:
            continue
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return "source_record_invalid_temporal_binding"
        if (parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed) > datetime.now(timezone.utc):
            return "source_record_not_yet_valid"
    if (is_outcome_pollution_record(record)
            or default_recall_blocked_lane(business_metadata(record.meta), record.content, record.provenance)):
        return "source_record_operational"
    return ""


def _binding_metadata(binding: Any) -> dict[str, Any]:
    if not isinstance(binding, dict):
        return {}
    scope = binding.get("scope") if isinstance(binding.get("scope"), dict) else {}
    return {"record_id": str(binding.get("record_id") or "")[:128],
            "source_id": str(binding.get("source_id") or "")[:128],
            "scope": {key: str(scope.get(key) or "")[:128]
                      for key in ("tenant_id", "agent_id", "workspace_id", "user_id")},
            "payload_digest": str(binding.get("payload_digest") or "")[:64]}


def _generation_metadata(generation: Any) -> dict[str, Any]:
    if not isinstance(generation, dict):
        return {}
    reason = generation.get("reason")
    reasons = generation.get("skipped_reason_counts")
    refs = generation.get("source_refs")
    try:
        stable_before = _timestamp(generation["stable_before"]).isoformat().replace("+00:00", "Z") if generation.get("stable_before") else ""
    except (TypeError, ValueError):
        stable_before = ""
    return {"ok": generation.get("ok") is True,
            "reason": reason if isinstance(reason, str) and reason in _GENERATION_REASONS
                      else "controlled_memory_dataset_invalid",
            "stable_before": stable_before,
            **{key: generation[key] for key in ("case_count", "scanned_count", "scan_truncated",
                                               "selection_truncated", "same_run_skipped_count")
               if key in generation and isinstance(generation[key], (int, bool))},
            "skipped_reason_counts": {key: value for key, value in (reasons or {}).items()
                                      if key in _ELIGIBILITY_REASONS and isinstance(value, int)}
                                     if isinstance(reasons, dict) else {},
            "source_refs": [_binding_metadata(ref) for ref in refs[:MAX_CONTROLLED_CASES]]
                           if isinstance(refs, list) else []}


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timestamp_invalid")
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    return (parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed).astimezone(timezone.utc)


def _stable_source_reason(record: Any, cutoff: datetime | None) -> str:
    if cutoff is None:
        return ""
    try:
        created_at = _timestamp(record.time.created_at)
    except (TypeError, ValueError):
        return "source_created_at_invalid"
    return "source_not_stable_before_cutoff" if created_at > cutoff else ""


def _binding(record: Any) -> dict[str, Any]:
    return {"record_id": record.record_id, "source_id": record.source_id,
            "scope": asdict(record.scope), "payload_digest": authority_digest(record)}


def _case(record: Any) -> dict[str, Any] | None:
    material = str(record.title or "").strip() or str(record.summary or record.content.get("text") or "").strip()
    if not material:
        return None
    query = material[:300]
    binding = _binding(record)
    key = sha256(json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]
    channel = runtime_channel_from_scope(record.scope)
    context = {"source_ids": [record.source_id], "target_source_id": record.source_id, "exact_scope_only": True}
    if channel:
        context["runtime_channel"] = channel
    return {"case_id": f"controlled-memory-{key}", "phase": "usage", "execution_type": "retrieval",
            "query": query, "query_origin": "controlled_source_prefix", "source": CONTROLLED_MEMORY_DATASET_KIND,
            "scope": asdict(record.scope), "limit": 5, "source_binding": binding,
            "expect_any_record_id": [record.record_id], "expected_empty": False,
            "formal_business_evidence": False,
            "task_context": context}


def _dataset(scope: ScopeRef, sources: tuple[str, ...], cases: list[dict[str, Any]], skipped: Counter[str],
             scanned: int, truncated: bool, reason: str, *, stable_before: str = "",
             selection_truncated: bool = False, ok: bool = True) -> dict[str, Any]:
    return {"schema": CONTROLLED_MEMORY_BENCHMARK_SCHEMA, "name": "controlled-memory-known-item",
            "dataset_kind": CONTROLLED_MEMORY_DATASET_KIND, "formal_business_evidence": False,
            "certifies_natural_queries": False, "certifies_no_answer": False,
            "scope": asdict(scope), "threshold": 1.0, "seed": [], "cases": cases,
            "dataset_generation": {"ok": ok, "reason": reason, "case_count": len(cases),
                                   "scanned_count": scanned, "scan_truncated": truncated,
                                   "selection_truncated": selection_truncated, "stable_before": stable_before,
                                   "same_run_skipped_count": skipped.get("source_not_stable_before_cutoff", 0),
                                   "authorized_source_ids": list(sources),
                                   "skipped_reason_counts": dict(sorted(skipped.items())),
                                   "source_refs": [case["source_binding"] for case in cases]}}
